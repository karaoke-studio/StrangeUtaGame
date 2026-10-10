"""低延迟按键音播放器 — 基于 BASS Sample API。

BASS_SampleLoad 把 WAV 加载为"样本"，最多可同时持有 _MAX_CONCURRENT 个播放通道，
超出时自动复用最旧的通道（BASS_SAMPLE_OVER_POS）。每次 play_* 调用仅需
BASS_SampleGetChannel + BASS_ChannelPlay，无文件 IO，无内存分配，延迟极低。
"""

from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Optional

from . import bass_available
from .sample_registry import register_bass_sample_owner

try:
    from .bass_engine import (
        BASS_ATTRIB_VOL,
        BASS_DEVICE_LATENCY,
        BASS_ERROR_ALREADY,
        BASS_UNICODE,
        _bass,
    )
except (ImportError, OSError, AttributeError):
    # mac 等：bass_engine 不可导入（_DummyCDLL 抛 AttributeError）。KeySoundPlayer
    # 仅在 bass_available 为 True 时由工厂实例化，占位常量不会被实际使用。
    _bass = None  # type: ignore[assignment]
    BASS_ATTRIB_VOL = 0
    BASS_DEVICE_LATENCY = 0
    BASS_ERROR_ALREADY = -1
    BASS_UNICODE = 0

BASS_SAMPLE_OVER_POS: int = 0x400000  # 超出 max 时复用最旧（按播放位置）
_MAX_CONCURRENT: int = 8              # 每个音效最多同时播放数


class KeySoundPlayer:
    """低延迟按键音播放器，支持重叠播放，不互相打断。

    线程安全：BASS 内部线程安全，此类不加额外锁。
    """

    def __init__(self) -> None:
        self._press_sample: int = 0
        self._release_sample: int = 0
        # 源路径记忆：BASS 会话被引擎 BASS_Free 重建后按路径惰性重载
        self._press_path: Optional[Path] = None
        self._release_path: Optional[Path] = None
        self._invalidated: bool = False
        self._enabled: bool = True
        self._volume: float = 1.0  # 0.0 ~ 2.0（对应 0 ~ 200%）
        # 登记：任一 BASS 引擎恢复设备（BASS_Free 重建会话）后统一失效
        # 本实例句柄，下一次播放时对新会话惰性重载（sample_registry）。
        register_bass_sample_owner(self)

    def load(self, press_path: Path, release_path: Path) -> None:
        """加载按下音和抬起音。已有样本先释放。"""
        self.free()
        # BASS 是进程全局单例；若已由 BassEngine 初始化则 BASS_ERROR_ALREADY 正常
        if not _bass.BASS_Init(-1, 44100, BASS_DEVICE_LATENCY, None, None):
            if _bass.BASS_ErrorGetCode() != BASS_ERROR_ALREADY:
                return  # 初始化失败，静默跳过
        self._press_path = press_path
        self._release_path = release_path
        self._press_sample = self._load_sample(press_path)
        self._release_sample = self._load_sample(release_path)
        self._invalidated = False

    def _reload_if_invalidated(self) -> None:
        """设备恢复（BASS_Free 重建会话）后的惰性重载。

        引擎恢复流程结束后本实例句柄已被失效归零；此处按记忆的源路径把
        样本重新加载到新会话。只触发一次，失败静默（_invalidated 已清，
        不会每次按键都重试无效文件 IO）。
        """
        if not self._invalidated:
            return
        self._invalidated = False
        press, release = self._press_path, self._release_path
        if press is None or release is None:
            return
        self.load(press, release)

    def _load_sample(self, path: Path) -> int:
        if not path.is_file():
            return 0
        return int(
            _bass.BASS_SampleLoad(
                False,
                ctypes.c_wchar_p(str(path)),
                0,
                0,
                _MAX_CONCURRENT,
                BASS_SAMPLE_OVER_POS | BASS_UNICODE,
            )
        )

    def play_press(self) -> None:
        """播放按下音（立即返回，不阻塞）。"""
        if not self._enabled:
            return
        self._reload_if_invalidated()
        if not self._press_sample:
            return
        try:
            chan = _bass.BASS_SampleGetChannel(self._press_sample, False)
            if chan:
                _bass.BASS_ChannelSetAttribute(chan, BASS_ATTRIB_VOL, ctypes.c_float(self._volume))
                _bass.BASS_ChannelPlay(chan, False)
        except Exception:
            pass

    def play_release(self) -> None:
        """播放抬起音（立即返回，不阻塞）。"""
        if not self._enabled:
            return
        self._reload_if_invalidated()
        if not self._release_sample:
            return
        try:
            chan = _bass.BASS_SampleGetChannel(self._release_sample, False)
            if chan:
                _bass.BASS_ChannelSetAttribute(chan, BASS_ATTRIB_VOL, ctypes.c_float(self._volume))
                _bass.BASS_ChannelPlay(chan, False)
        except Exception:
            pass

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def set_volume(self, volume_pct: int) -> None:
        """设置音量，单位为百分比（0~200）。"""
        self._volume = max(0.0, min(2.0, volume_pct / 100.0))

    def invalidate(self) -> None:
        """BASS_Free 后调用：将 handle 归零并标记待重载，不再尝试 BASS_SampleFree。

        BASS_Free 已经回收了所有资源；若之后再用旧 handle 调用 BASS_SampleFree，
        可能误释放新 BASS 会话中复用了同一 handle 值的合法资源。句柄失效后
        在下一次播放时对新会话惰性重载（见 _reload_if_invalidated）。
        """
        self._press_sample = 0
        self._release_sample = 0
        if self._press_path is not None and self._release_path is not None:
            self._invalidated = True

    def is_loaded(self) -> bool:
        return bool(self._press_sample and self._release_sample)

    def free(self) -> None:
        """释放样本资源（BASS_Free 之后调用亦安全）。"""
        try:
            if self._press_sample:
                _bass.BASS_SampleFree(self._press_sample)
        except Exception:
            pass
        finally:
            self._press_sample = 0
        try:
            if self._release_sample:
                _bass.BASS_SampleFree(self._release_sample)
        except Exception:
            pass
        finally:
            self._release_sample = 0
        # 路径一并清掉：free 后不再具备"惰性重载到新会话"的前提（实例即将
        # 弃用，或 load() 换样本前的例行清理）；否则可能按过期路径重载。
        self._press_path = None
        self._release_path = None
        self._invalidated = False


# ── mac（BASS 不可用）回退实现 ──────────────────────────────────────────────

import functools
import threading

import sounddevice as _sd
import soundfile as _sf
import numpy as _np

from .portaudio_refresh import (
    portaudio_refresh_in_progress,
    register_refresh_listener,
    unregister_refresh_listener,
)


class SdSampleStreamPool:
    """轮换 OutputStream 小池：让 sounddevice 短音效可重叠播放。

    ``sd.play`` 使用全局单例播放器——每次调用会掐掉上一次的播放，连打
    按键音/节拍音互相截断。这里维护一小池常驻 OutputStream 轮换使用：
    每次播放取下一个流，把待播数据挂到该流的槽位并确保流在跑，回调从
    槽位取数填输出。池大小即最大重叠数，超出时复用最旧的槽（语义对齐
    BASS 版的 BASS_SAMPLE_OVER_POS）。

    流按 (采样率, 声道数) 惰性创建；参数变化时整池重建。设备不可用等
    异常一律静默退回 ``sd.play`` 全局播放（旧行为，好过无声）。
    """

    def __init__(self, size: int = 3) -> None:
        self._size = max(1, int(size))
        self._lock = threading.Lock()
        self._streams: list = [None] * self._size
        self._pending: list = [None] * self._size  # 每槽 (data, read_pos) 或 None
        self._next = 0
        self._sr = 0
        self._ch = 0
        # PortAudio 设备表刷新（#126：主引擎检测到默认输出设备切换）前必须
        # 关闭本池所有流——Pa_Terminate 会摧毁在开的流。刷新后按需重建
        # （play 惰性建流）。close() 永久关池时注销监听。
        self._refresh_listener = self._close_for_refresh
        register_refresh_listener(self._refresh_listener)

    def play(self, data: "_np.ndarray", sr: int) -> None:
        """非阻塞播放一段 PCM（可为 (n,) 或 (n, ch)），与池内其它槽互不掐断。"""
        if data is None or len(data) == 0:
            return
        # PortAudio 刷新窗口内不新开流（也不退回 sd.play——它同样要开流）：
        # 键音/节拍音是毫秒级短音效，丢一发无感（BASS 路径设备恢复时同样
        # 丢弃在途样本）。
        if portaudio_refresh_in_progress():
            return
        ch = int(data.shape[1]) if data.ndim > 1 else 1
        started = False
        with self._lock:
            if sr != self._sr or ch != self._ch:
                self._close_locked()
                self._sr = int(sr)
                self._ch = ch
            slot = self._next
            self._next = (self._next + 1) % self._size
            stream = self._streams[slot]
            if stream is None:
                try:
                    stream = _sd.OutputStream(
                        samplerate=int(sr),
                        channels=ch,
                        dtype="float32",
                        callback=functools.partial(self._cb, slot=slot),
                    )
                    self._streams[slot] = stream
                except Exception:
                    stream = None
            if stream is not None:
                self._pending[slot] = (data, 0)
                try:
                    if not stream.active:
                        stream.start()
                    started = True
                except Exception:
                    self._pending[slot] = None
        if not started:
            try:
                _sd.play(data, sr)
            except Exception:
                pass  # 设备忙/不可用时不打断主流程

    def _cb(self, outdata, frames, time_info, status, slot: int) -> None:
        with self._lock:
            item = self._pending[slot]
        if item is None:
            outdata.fill(0)
            return
        data, pos = item
        n = len(data)
        take = min(frames, n - pos)
        if take > 0:
            seg = data[pos : pos + take]
            if seg.ndim == 1:
                seg = seg.reshape(-1, 1)
            outdata[:take] = seg
        if take < frames:
            outdata[take:].fill(0)
        new_pos = pos + take
        with self._lock:
            # 双检：期间 play() 可能已复用本槽（换新数据）——不覆盖新槽位
            if self._pending[slot] is item:
                if new_pos >= n:
                    self._pending[slot] = None
                else:
                    self._pending[slot] = (data, new_pos)

    def _close_locked(self) -> None:
        for i in range(self._size):
            self._pending[i] = None
            s = self._streams[i]
            self._streams[i] = None
            if s is not None:
                try:
                    s.stop()
                    s.close()
                except Exception:
                    pass

    def _close_for_refresh(self) -> None:
        """PortAudio 刷新前回调：关闭整池流并复位参数（#126）。

        保持注册状态——刷新后 play() 惰性重建流，之后的刷新仍需联动本池。
        """
        with self._lock:
            self._close_locked()
        self._sr = 0
        self._ch = 0

    def close(self) -> None:
        unregister_refresh_listener(self._refresh_listener)
        self._close_for_refresh()


class SoundDeviceKeySoundPlayer:
    """基于 sounddevice 的按键音播放器（mac 等无 BASS 平台使用）。

    与 :class:`KeySoundPlayer` 同接口：``load`` 预读 WAV 为 numpy 数组，
    ``play_*`` 调 ``sounddevice.play``。节拍器点击音对延迟容忍度高，per-call
    播放足够，不复刻主引擎的 ring buffer。
    """

    def __init__(self) -> None:
        self._press: tuple[_np.ndarray, int] | None = None  # (data, sample_rate)
        self._release: tuple[_np.ndarray, int] | None = None
        self._enabled: bool = True
        self._volume: float = 1.0  # 0.0 ~ 2.0
        # 轮换流池：连打时按键音可重叠，不再后次掐前次
        self._pool = SdSampleStreamPool(size=3)

    def load(self, press_path: Path, release_path: Path) -> None:
        """加载按下音和抬起音；失败静默跳过。"""
        self._press = self._read(press_path)
        self._release = self._read(release_path)

    @staticmethod
    def _read(path: Path) -> tuple[_np.ndarray, int] | None:
        if not path.is_file():
            return None
        try:
            data, sr = _sf.read(str(path), dtype="float32")
            return data, sr
        except Exception:
            return None

    def _play(self, sample: tuple[_np.ndarray, int] | None) -> None:
        if not self._enabled or sample is None:
            return
        data, sr = sample
        try:
            self._pool.play(data * self._volume, sr)
        except Exception:
            pass  # 设备忙/不可用时不打断主流程

    def play_press(self) -> None:
        self._play(self._press)

    def play_release(self) -> None:
        self._play(self._release)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def set_volume(self, volume_pct: int) -> None:
        self._volume = max(0.0, min(2.0, volume_pct / 100.0))

    def invalidate(self) -> None:
        """对齐 KeySoundPlayer 接口；sounddevice 无外部 handle 需失效。"""
        pass

    def is_loaded(self) -> bool:
        return self._press is not None and self._release is not None

    def free(self) -> None:
        self._press = None
        self._release = None
        self._pool.close()


def create_keysound_player():
    """按 BASS 可用性选择 keysound 实现。"""
    if bass_available:
        return KeySoundPlayer()
    return SoundDeviceKeySoundPlayer()
