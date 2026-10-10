"""PortAudio 设备表刷新协调器（sounddevice 回退平台，#126）。

背景：
PortAudio 在 ``Pa_Initialize`` 时快照一份设备表和各 host API 的默认设备，
进程内此后不再自动更新（macOS 上尤其如此：新插拔的设备不出现，默认设备
切换也不被察觉）。因此"重新打开流"拿到的仍是**开流那一刻**的旧默认设备
——这就是 #126 里"换默认输出设备后播放不跟随、重载音频也没用、必须重启
应用"的根因。

修复思路：
检测到系统默认输出设备变化时（``SoundDeviceEngine`` 轮询注入的 provider，
见 ``set_default_output_device_provider``），先让进程内**所有**持有
PortAudio 流的组件（主引擎、按键音/节拍器的 :class:`SdSampleStreamPool`）
关掉各自的流，再 ``sd._terminate()`` + ``sd._initialize()`` 强制 PortAudio
重建设备表，最后各组件在新的默认设备上重建流。

注意：
``Pa_Terminate`` 时若有流仍然打开，其原生结构会被释放而回调链尚在，存在
崩溃风险——"先关流、后 terminate"是硬性顺序，由本模块的监听器机制保证；
极小概率落在刷新窗口内的新开流由各组件检查 :func:`portaudio_refresh_in_progress`
自行丢弃（键音/节拍音是毫秒级短音效，丢一发无感）。
"""

from __future__ import annotations

import threading
from typing import Callable

import sounddevice as sd

# RLock：串行化刷新本身；监听器回调（各组件关流）内部允许再进本锁
# （如注销自己），不会自锁。
_lock = threading.RLock()
_listeners: list[Callable[[], None]] = []
# 刷新窗口标志：普通 bool，读侧无锁（GIL 下原子），供各组件在开流前
# 检查以避开 terminate/init 的危险窗口。
_refreshing = False


def register_refresh_listener(listener: Callable[[], None]) -> None:
    """登记一个"刷新前关流"回调（零参，关闭调用方持有的全部 PortAudio 流）。

    回调在刷新线程内执行，必须快、不得再触发新的刷新。重复登记忽略。
    """
    with _lock:
        if listener not in _listeners:
            _listeners.append(listener)


def unregister_refresh_listener(listener: Callable[[], None]) -> None:
    """注销监听器（组件永久关闭时调用；临时关流由刷新流程自动触发）。"""
    with _lock:
        try:
            _listeners.remove(listener)
        except ValueError:
            pass


def portaudio_refresh_in_progress() -> bool:
    """是否正处于 PortAudio 刷新窗口（窗口内不要新开流）。"""
    return _refreshing


def refresh_portaudio_devices() -> bool:
    """强制 PortAudio 重建设备表：通知所有监听组件关流 → terminate → initialize。

    这是让"不带 device 参数的新流"重新解析到**当前**系统默认设备的唯一
    手段（macOS 上 PortAudio 不会自行刷新）。sounddevice 未公开这对接口，
    ``sd._terminate/_initialize`` 是其私有的 ``Pa_Terminate/Pa_Initialize``
    直通——PortAudio 的初始化计数按配对调用维护，这里成对使用保持平衡。

    Returns:
        True 表示 terminate/initialize 均已执行（无论监听器是否成功关流）；
        False 表示重新初始化失败（音频功能大概率已不可用，调用方按设备
        异常兜底处理即可）。
    """
    global _refreshing
    with _lock:
        _refreshing = True
        try:
            # 1) 各组件关流（失败的监听器跳过，不阻断后续 terminate——
            #    不 terminate 就无法更新设备表，两害取其轻）
            for listener in list(_listeners):
                try:
                    listener()
                except Exception:
                    pass
            # 2) 重建 PortAudio 设备表。terminate 报错（理论上仅在底层状态
            #    异常时出现）不阻断 initialize——必须把初始化计数配平，
            #    否则进程内 PortAudio 彻底报废。
            try:
                sd._terminate()
            except Exception:
                pass
            try:
                sd._initialize()
            except Exception:
                return False
            return True
        finally:
            _refreshing = False
