"""SoundDevice 音频引擎（离线预渲染 + RingBuffer 架构）。

设计目标：
彻底消除"回调线程跑 Python WSOLA + 持锁 + 大 blocksize"导致的爆音/卡顿/
左右声道交替伪像。

架构分三层：

1. :class:`TSMRenderCache`：UI/控制线程在切换速度时**离线**把整段原始 PCM
   预渲染成目标速度的 PCM，缓存在内存里（LRU 5 份）。1.0x 直接复用原始
   PCM，零开销。

2. :class:`RingBuffer`：单生产者单消费者无锁环形缓冲（float32 (n, ch)），
   生产者把"已渲染好的 PCM"按时间顺序填进去，消费者（回调）按 frames
   取出。

3. **Producer 线程**：tight loop 把 ``_active_pcm[_read_pos:]`` 里的样本
   切片送进 ring；监控用户的速度切换/seek 请求，在合适时机原子地把
   ``_active_pcm`` 替换成新速度的预渲染结果（保位 = 把当前位置的
   "原始时间轴"映射到新 PCM 上）。

4. **音频回调**：极简——只调 ``ring.read_into(outdata)``，不足补零，
   零分配、零锁、零 Python 重活。同时更新消费者侧时基锚点
   （``_consumed_in_active_pcm`` + ``_last_callback_perf_time``）。

位置语义保持不变：
- 对外暴露的 ``position_ms`` 始终指**原始音频时间轴**（不随速度伸缩）。
- 位置查询基于消费者侧锚点 + ``time.perf_counter()`` 外推，精度 <1ms，
  消除轮询抖动和 RingBuffer 竞态。
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import sounddevice as sd
import soundfile as sf

from .base import (
    AudioInfo,
    AudioLoadError,
    AudioPlaybackError,
    IAudioEngine,
    PlaybackState,
    compute_mono_samples,
)
from .portaudio_refresh import refresh_portaudio_devices
from .ring_buffer import RingBuffer
from .tsm_cache import TSMRenderCache, LoadProgressCallback, _quantize


# ---- Windows 线程优先级工具 ----
# Windows THREAD_PRIORITY_* 常量
_THREAD_PRIORITY_BELOW_NORMAL = -1
_THREAD_PRIORITY_NORMAL = 0
_THREAD_PRIORITY_ABOVE_NORMAL = 1
_THREAD_PRIORITY_HIGHEST = 2


def _set_thread_priority(priority: int) -> None:
    """设置当前线程的 Windows 优先级（仅 Windows，其他平台静默忽略）。"""
    if sys.platform != "win32":
        return
    try:
        handle = ctypes.windll.kernel32.GetCurrentThread()
        ctypes.windll.kernel32.SetThreadPriority(handle, priority)
    except Exception:
        pass  # 非关键路径，失败不影响功能


# ---- 常量 ----

# 回调块大小：1024 帧（@ 44.1kHz ≈ 23ms），远小于旧版 4410（100ms），
# 显著降低 underrun 概率与延迟。
_BLOCK_FRAMES = 1024

# Ring buffer 容量：~500ms 缓冲，足以吸收任何 GIL/调度抖动。
_RING_SECONDS = 0.5

# Producer tick：达到目标余量后睡眠的间隔（秒）
_PRODUCER_TICK = 0.005

# 目标硬件延迟：传给 PortAudio 的建议值（秒）。
# WASAPI Shared 模式硬下限约 20ms，传更小值会被 clamp 到硬件最小值。
# 传 0.1 让 PortAudio 申请标准低缓冲，实际值由 stream.latency 读回。
_TARGET_LATENCY = 0.1

# 默认输出设备轮询间隔（秒）：producer 线程以此频率比对"系统当前默认
# 输出设备"与"当前流绑定的默认设备"（#126）。
_DEFAULT_DEVICE_POLL_INTERVAL = 0.5


def _portaudio_default_output_key():
    """回退 provider：PortAudio 视角下的默认输出设备标识。

    Windows 上 PortAudio 对设备热插拔有一定自动刷新能力；macOS 上设备表
    在 ``Pa_Initialize`` 后不再更新，此函数看不到后续变化——真正的检测
    依赖前端注入的 provider（Qt ``QMediaDevices``，见 main_window 接线）。

    Returns:
        可哈希的设备标识；无法确定时返回 None（调用方跳过本轮比对）。
    """
    try:
        idx = sd.default.device[1]
        if idx is None or idx < 0:
            return None
        info = sd.query_devices(idx)
        # 用 name 不用 index：设备表刷新后 index 会整体重排
        return ("pa", info["name"])
    except Exception:
        return None


class SoundDeviceEngine(IAudioEngine):
    """音频引擎（离线预渲染 + RingBuffer）。"""

    def __init__(self) -> None:
        # ---- 音频元数据 ----
        self._original_data: Optional[np.ndarray] = None  # (n, channels) float32（原始文件，仅用于波形显示）
        self._mono_data: Optional[np.ndarray] = None  # 1-D 分析用单声道（加载线程预混，波形/声谱/BPM 共用）
        self._original_sample_rate: int = 44100  # 原始文件采样率（用于时长/位置计算）
        self._sample_rate: int = 44100  # 播放采样率（MP3 采样率，用于 stream/ring）
        self._channels: int = 2
        self._file_path: Optional[str] = None
        self._duration_ms: int = 0

        # ---- 播放状态 ----
        self._state = PlaybackState.STOPPED
        self._speed: float = 1.0
        self._volume: float = 1.0

        # ---- 渲染缓存 ----
        self._cache = TSMRenderCache()

        # ---- Active PCM（producer 当前在喂的那一份）----
        # 不可变快照：切换 active 时整体替换 (pcm, speed)
        self._active_pcm: Optional[np.ndarray] = None  # (n, channels)
        self._active_speed: float = 1.0
        self._read_pos_samples: int = 0  # 在 active_pcm 上的偏移

        # ---- 速度切换请求 ----
        # producer 检测到 _pending_speed != _active_speed 时，若新速度的
        # PCM 已就绪则立即原子换 active；否则先 ensure() 触发后台渲染，
        # 渲染完成后再换。
        self._pending_speed: float = 1.0

        # ---- Ring buffer（在 load 后按 channels 重建）----
        self._ring: Optional[RingBuffer] = None

        # ---- 线程 ----
        self._producer_thread: Optional[threading.Thread] = None
        self._producer_stop = threading.Event()
        # active 切换/seek 时 producer 必须暂停喂数据并 reset ring
        # _state_lock 只保护 producer 的"控制平面"（active/pos/pending），
        # 回调线程从不持此锁。
        self._state_lock = threading.Lock()

        # ---- 喂数据代数（epoch）----
        # seek/换源/load/stop 都会使 producer 当前这轮"读 pos → 写 ring →
        # 推进 pos"失效。原先仅凭 active_pcm 身份比对无法识别"同一份 PCM
        # 上的 seek"，锁外窗口里的 seek 会被吞掉、ring 混入旧位置音频。
        # 控制路径改 pos 时递增；producer 全程比对写入前后的 epoch。
        self._epoch: int = 0

        self._stream: Optional[sd.OutputStream] = None

        # ---- 硬件延迟补偿 ----
        # load() 建流后从 stream.latency 读取实际值，转换为帧数。
        # 用于 get_position_ms() 补偿"数据进 ring → 从硬件输出"之间的固定延迟。
        self._stream_latency_frames: int = 0

        # ---- 热重载标志位 ----
        # 当音频回调检测到底层设备异常时置位，由 producer 线程执行恢复
        self._needs_recovery = threading.Event()

        # ---- 默认输出设备跟踪（#126）----
        # 流不指定 device 打开，绑定的是"开流那一刻"的系统默认设备；用户
        # 中途改默认设备（插耳机等）在 macOS 上既无回调错误、流也不失效，
        # 只能轮询比对。_default_device_provider：由前端注入，返回当前系统
        # 默认输出设备的可哈希标识（Qt QAudioDevice.id()）；未注入时回退
        # PortAudio 视角。_open_default_device_key：当前流实际绑定的默认
        # 设备标识（None = 基线待建立）。
        self._default_device_provider: Optional[Callable[[], object]] = None
        self._open_default_device_key: object = None
        self._last_device_poll_ts: float = 0.0

        # ---- 消费者侧高精度时基锚点 ----
        # 由 _audio_callback（PortAudio 实时线程）更新，无竞态：
        # _consumed_in_active_pcm = 当前 active PCM 上已消费的帧数
        # _last_callback_perf_time = 上次回调的 perf_counter 时间戳
        # 注意：int 的 += 并非原子（LOAD/ADD/STORE 三步可被 GIL 切开），
        # 回调线程与 seek/换源（控制线程）的写入必须经 _anchor_lock 串行。
        # 该锁内只有毫秒级赋值，回调线程可安全使用；且回调从不取
        # _state_lock，两个锁之间不构成环，无死锁风险。
        self._anchor_lock = threading.Lock()
        self._consumed_in_active_pcm: int = 0
        self._last_callback_perf_time: float = 0.0

        # ---- 位置回调（保留接口，不再主动轮询）----
        self._position_callback: Optional[Callable[[int], None]] = None

        # ---- 渲染进度回调 ----
        # 签名 ``(speed, progress∈[0,1])``；progress=1.0 表示已就绪/切换完成。
        # 可能从 TSMRenderCache 的 worker 线程调用——UI 侧需自行 marshal。
        self._render_progress_cb: Optional[Callable[[float, float], None]] = None

    # ==================== 加载 / 资源 ====================

    def load(self, file_path: str, progress_cb: Optional[LoadProgressCallback] = None) -> None:
        # 加载新音频前，彻底停止并销毁旧的流和线程，防止"幽灵流"叠加导致倍速播放
        self.stop()

        try:
            if progress_cb:
                progress_cb("读取音频文件...", 0.0)
            data, sr = sf.read(file_path, dtype="float32")
            if data.ndim == 1:
                data = data.reshape(-1, 1)

            self._original_sample_rate = int(sr)
            self._channels = int(data.shape[1])
            self._file_path = file_path
            self._original_data = np.ascontiguousarray(data, dtype=np.float32)

            # 获取歌曲名称（不含扩展名）用于缓存文件命名
            song_name = Path(file_path).stem
            self._cache.set_source(song_name, self._original_data, self._original_sample_rate, progress_cb)

            # 从缓存获取 MP3 后的 PCM
            cached_pcm = self._cache.get(1.0)
            if cached_pcm is not None:
                self._sample_rate = self._cache._sample_rate
                self._channels = self._cache._channels
            else:
                self._sample_rate = self._original_sample_rate
                cached_pcm = self._original_data

            # mono 必须与显示 PCM（get_original_samples）处于同一采样空间：
            # 缓存 PCM 可能被重采样（如 22050→32000）且含 MP3 编解码延迟/
            # 补齐（~26ms）。从 _original_data 预混会让波形/声谱/BPM 与
            # AudioInfo.sample_rate 解释的时间轴整体错位（P1-1）。
            self._mono_data = compute_mono_samples(cached_pcm)

            # 时长基于 MP3 实际数据（确保位置计算一致）
            self._duration_ms = int(len(cached_pcm) / self._sample_rate * 1000)

            # 重建 ring
            cap = max(_BLOCK_FRAMES * 4, int(_RING_SECONDS * self._sample_rate))
            self._ring = RingBuffer(cap, self._channels)

            with self._state_lock:
                self._active_pcm = cached_pcm
                self._active_speed = 1.0
                self._speed = 1.0
                self._pending_speed = 1.0
                self._read_pos_samples = 0
                # 换歌：作废旧 producer 可能仍在途的喂数据轮次
                self._epoch += 1

            self._state = PlaybackState.STOPPED

            # 立即建流并保持存活（静音输出）：
            # 避免每次 play() 重新握手 Windows 音频端点，消除 0~200ms 启动抖动。
            # 流一旦建立，play/pause/stop 仅改 _state 标志位，不再销毁重建。
            self._start_streaming()

        except FileNotFoundError:
            raise AudioLoadError(f"文件不存在: {file_path}")
        except Exception as e:
            raise AudioLoadError(f"加载音频失败: {e}")

    def prewarm_speeds(
        self,
        speed_min: float = 0.2,
        speed_max: float = 2.0,
    ) -> None:
        """以指定速度范围触发后台预渲染（公共接口，供 UI 层调用）。"""
        self._prewarm_common_speeds(speed_min=speed_min, speed_max=speed_max)

    def _prewarm_common_speeds(
        self,
        speed_min: float = 0.2,
        speed_max: float = 2.0,
    ) -> None:
        """预渲染常用速度到磁盘缓存（后台进行，不阻塞加载）"""
        if self._original_data is None:
            return

        # 预渲染速度列表（按优先级排序，优先级越小越优先）
        common_speeds = [
            (0.75, 0),
            (0.5, 1),
            (0.9, 2),
            (0.8, 3),
            (0.7, 4),
            (0.6, 5),
            (0.4, 6),
            (0.3, 7),
            (0.2, 8),
        ]
        for speed, priority in common_speeds:
            if speed_min - 1e-9 <= speed <= speed_max + 1e-9:
                # check_only=True：预热只查存在性/派发渲染，绝不在调用线程
                # （UI 线程）对已缓存的档位做整曲 MP3 同步解码再丢弃。
                self._cache.ensure(speed, priority=priority, check_only=True)

    def release(self) -> None:
        # 彻底关闭 PortAudio 流并停掉 producer 线程：只 stop() 不关流的话，
        # 引擎释放后设备仍被占用，新旧引擎实例会争抢同一输出端点。
        self._stop_streaming()
        self.stop()
        self._cache.clear()
        with self._state_lock:
            self._original_data = None
            self._mono_data = None
            self._active_pcm = None
            self._file_path = None
            self._duration_ms = 0
            self._read_pos_samples = 0
            with self._anchor_lock:
                self._consumed_in_active_pcm = 0
                self._last_callback_perf_time = 0.0
            self._stream_latency_frames = 0
        self._ring = None
        # 默认设备基线作废：下次 load() 开流时重新采纳
        self._open_default_device_key = None

    # ==================== 播放控制 ====================

    def play(self) -> None:
        if self._original_data is None:
            raise AudioPlaybackError("没有加载音频文件")

        if self._state == PlaybackState.PLAYING:
            return

        if self._state == PlaybackState.PAUSED:
            self._state = PlaybackState.PLAYING
            # 恢复外推锚点（暂停期间 perf_counter 已停更）
            self._last_callback_perf_time = time.perf_counter()
            return

        # STOPPED → PLAYING：流已在 load() 时建好并保持存活，
        # 此处只需切换状态标志位即可，producer 会立即开始喂数据，
        # 回调检测到 PLAYING 后输出真实音频，无需重新握手 OS 音频端点。
        # 先强制对齐 active 速度，避免 producer 喂旧速度数据
        self._maybe_swap_active_speed()
        # 若流因某种原因不存在（首次 load 失败后重试等边缘情况），补建
        if self._stream is None or not self._stream.active:
            self._start_streaming()
        self._state = PlaybackState.PLAYING

    def pause(self) -> None:
        if self._state == PlaybackState.PLAYING:
            self._state = PlaybackState.PAUSED
            # 不停 stream/producer，回调里检测 PAUSED 直接静音输出

    def stop(self) -> None:
        # 不销毁流：流在整个文件生命周期内保持存活（静音输出），
        # 消除下次 play() 时重新握手 OS 端点带来的启动延迟抖动。
        self._state = PlaybackState.STOPPED

        with self._state_lock:
            self._read_pos_samples = 0
            self._epoch += 1
            with self._anchor_lock:
                self._consumed_in_active_pcm = 0
                self._last_callback_perf_time = 0.0
            if self._original_data is not None:
                # 始终让 active 与当前速度保持一致
                if abs(self._speed - 1.0) < 1e-9:
                    self._active_pcm = self._cache.get(1.0)
                    self._active_speed = 1.0
                else:
                    cached = self._cache.get(self._speed)
                    if cached is not None:
                        self._active_pcm = cached
                        self._active_speed = self._speed
                    else:
                        self._active_pcm = self._cache.get(1.0)
                        self._active_speed = 1.0
                        self._speed = 1.0
                        self._pending_speed = 1.0
        # 清空 ring 里的残余样本，避免下次 play() 时输出旧数据
        if self._ring is not None:
            self._ring.reset()

    # ==================== 位置 ====================

    def get_position_ms(self) -> int:
        """获取当前播放位置（毫秒），基于高精度时钟外推，并补偿硬件延迟。

        锚点由 PortAudio 实时回调线程维护（零竞态），调用时通过
        ``time.perf_counter()`` 外推至当前微秒，消除轮询抖动。

        硬件延迟补偿：
        ``_stream_latency_frames`` 是 PortAudio 实际分配的输出缓冲延迟帧数
        （"回调把数据写入缓冲" → "硬件实际播出"的固定差值）。
        ``_consumed_in_active_pcm`` 在回调写入时更新，但声音还在硬件缓冲里，
        减去该偏移量后位置轴与用户听到的声音对齐。
        """
        # 锚点由回调线程经 _anchor_lock 写入（+= 非原子，见 __init__ 注释），
        # 读取也必须走同一把锁；其余控制平面字段仍归 _state_lock。
        with self._anchor_lock:
            base_consumed = self._consumed_in_active_pcm
            base_time = self._last_callback_perf_time
        with self._state_lock:
            if self._active_pcm is None or self._sample_rate == 0:
                return 0
            speed = self._active_speed
            sr = self._sample_rate
            latency_frames = self._stream_latency_frames

        # 播放中：用 perf_counter 外推自上次回调以来的额外帧数
        if self._state == PlaybackState.PLAYING and base_time > 0:
            elapsed = time.perf_counter() - base_time
            extra_frames = elapsed * sr
            total_consumed = base_consumed + extra_frames
        else:
            total_consumed = base_consumed

        # 补偿硬件输出延迟：消费者锚点领先于实际发声位置 latency_frames 帧。
        # 仅在 PLAYING 状态且有回调触发过（base_time > 0）时补偿；
        # STOPPED/PAUSED 状态下没有音频在硬件缓冲里，不需要补偿。
        if self._state == PlaybackState.PLAYING and base_time > 0:
            total_consumed = max(0.0, total_consumed - latency_frames)

        # 换算到原始时间轴：consumed_frames * speed = 原始采样位置
        orig_samples = total_consumed * speed
        ms = int(orig_samples / sr * 1000)
        return min(max(ms, 0), self._duration_ms)

    def set_position_ms(self, position_ms: int) -> None:
        if self._original_data is None:
            return
        position_ms = max(0, min(position_ms, self._duration_ms))
        orig_samples = int(position_ms / 1000 * self._sample_rate)
        self._seek_to_orig_samples(orig_samples)

    def _seek_to_orig_samples(self, orig_samples: int) -> None:
        """把播放头移到原始时间轴 ``orig_samples`` 处。"""
        with self._state_lock:
            if self._active_pcm is None or self._active_speed <= 0:
                return
            # 原始时间轴偏移 → 变速后 PCM 偏移
            # read_pos = orig_samples / active_speed
            new_pos = int(orig_samples / self._active_speed)
            new_pos = max(0, min(new_pos, len(self._active_pcm)))
            self._read_pos_samples = new_pos
            # epoch 递增：使 producer 在途的"读旧 pos → 写 ring → 推进 pos"
            # 整轮作废（写入前后的 epoch 比对不通过），不再污染 ring/吞 seek。
            self._epoch += 1
            # 重置消费者锚点（seek 后回调线程会从新位置开始消费）
            with self._anchor_lock:
                self._consumed_in_active_pcm = new_pos
                self._last_callback_perf_time = time.perf_counter()
        # 丢弃 ring 里的旧样本
        if self._ring is not None:
            self._ring.reset()

    def get_duration_ms(self) -> int:
        return self._duration_ms

    def get_output_latency_ms(self) -> int:
        if self._sample_rate <= 0:
            return 0
        return int(round(self._stream_latency_frames / self._sample_rate * 1000))

    # ==================== 状态查询 ====================

    def get_playback_state(self) -> PlaybackState:
        return self._state

    def is_playing(self) -> bool:
        return self._state == PlaybackState.PLAYING

    # ==================== 速度 ====================

    def set_speed(self, speed: float) -> None:
        """切换速度。

        - 同步更新 ``self._speed`` 与 ``_pending_speed``。
        - 若新速度的 PCM 已在缓存里，producer 在下一 tick 即原子切换；
          否则触发后台渲染，**继续用当前速度播放**直到渲染完成（无缝切）。
        - 永远非阻塞。
        """
        if not 0.2 <= speed <= 2.0:
            raise ValueError(f"速度 {speed} 超出范围 [0.2, 2.0]")
        self._speed = float(speed)

        with self._state_lock:
            self._pending_speed = self._speed

        # 触发后台渲染（如已缓存或 1.0x 则立即返回 ndarray，producer 下一
        # tick 会自动 pickup；这里不在 UI 线程做切换以避免持锁等待）
        if self._original_data is not None:
            q = _quantize(self._speed)
            cb = self._render_progress_cb
            result = self._cache.ensure(
                self._speed,
                progress_cb=cb,
            )
            # 命中缓存 / 1.0x 直通：立刻通知 UI "已就绪"
            if result is not None and cb is not None:
                try:
                    cb(q, 1.0)
                except Exception:
                    pass

    def get_speed(self) -> float:
        return self._speed

    # ==================== 音量 ====================

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, float(volume)))

    def get_volume(self) -> float:
        return self._volume

    # ==================== 回调注册 ====================

    def set_position_callback(self, callback: Callable[[int], None]) -> None:
        """保留接口兼容性。UI 侧应改用 QTimer 主动拉取 get_position_ms()。"""
        self._position_callback = callback

    def clear_position_callback(self) -> None:
        self._position_callback = None

    def set_render_progress_callback(
        self, callback: Optional[Callable[[float, float], None]]
    ) -> None:
        """注册渲染进度回调。

        签名 ``(speed, progress)``；``progress`` ∈ [0, 1]，1.0 表示已就绪。
        **注意**：回调可能在 TSMRenderCache 的 worker 线程被调用，UI 侧
        需通过 Qt 信号或 ``QMetaObject.invokeMethod`` marshal 到主线程。
        """
        self._render_progress_cb = callback

    def get_audio_info(self) -> Optional[AudioInfo]:
        if self._file_path is None:
            return None
        return AudioInfo(
            file_path=self._file_path,
            duration_ms=self._duration_ms,
            sample_rate=self._sample_rate,
            channels=self._channels,
        )

    def get_original_samples(self) -> Optional[np.ndarray]:
        """获取音频采样数据（用于波形可视化）

        Returns:
            MP3 后的 PCM 数据，形状为 (n_samples, channels) 的 float32 数组，
            采样率与播放采样率一致。如果没有加载音频则返回 None
        """
        return self._cache.get(1.0)

    def get_mono_samples(self) -> Optional[np.ndarray]:
        """分析用单声道（加载线程预混），波形/声谱/BPM 检测共用。"""
        return self._mono_data

    # ==================== 默认输出设备跟踪（#126） ====================

    def set_default_output_device_provider(
        self, provider: Optional[Callable[[], object]]
    ) -> None:
        """注入"当前系统默认输出设备"标识提供器（mac 等无 BASS 平台使用）。

        provider 返回任意可哈希标识（前端用 Qt ``QMediaDevices.defaultAudioOutput``
        的 ``QAudioDevice.id()``），返回 None 表示暂不可用。标识相对**当前流
        绑定的默认设备**（``_open_default_device_key``）发生变化时，producer
        线程会触发热重载：刷新 PortAudio 设备表并在新默认设备上重建流，
        保持与 BASS 路径一致的"跟随系统默认输出设备"行为。

        注入/撤销会重置基线（下一轮轮询直接采纳当前值，不触发恢复）。
        """
        self._default_device_provider = provider
        self._open_default_device_key = None

    def _current_default_device_key(self):
        """取当前系统默认输出设备标识（注入 provider 优先，失败/未注入回退 PortAudio）。"""
        provider = self._default_device_provider
        if provider is not None:
            try:
                return provider()
            except Exception:
                return None
        return _portaudio_default_output_key()

    def _align_default_device_before_open(self) -> None:
        """开流前对齐默认设备基线；基线已过期则先刷新 PortAudio 设备表。

        macOS 上 PortAudio 的设备表/默认设备缓存在 ``Pa_Initialize`` 后不再
        更新：仅销毁重建流仍会绑到旧默认设备（#126："重载音频也没用"）。
        必须先 terminate/initialize 强制重枚举，才能让不带 device 参数的新流
        落到**当前**系统默认设备上。
        """
        key = self._current_default_device_key()
        if key is None:
            return
        if self._open_default_device_key is not None and key != self._open_default_device_key:
            print("[SoundDeviceEngine] 默认输出设备已变化，开流前刷新 PortAudio 设备表")
            refresh_portaudio_devices()
        self._open_default_device_key = key

    def _check_default_device_changed(self) -> None:
        """比对当前系统默认输出设备与流绑定的设备，变化则触发热重载。"""
        key = self._current_default_device_key()
        if key is None:
            return
        if self._open_default_device_key is None:
            # 基线尚未建立（刚注入 provider / 刚 release / 首次开流前）
            self._open_default_device_key = key
            return
        if key != self._open_default_device_key:
            print("[SoundDeviceEngine] 检测到默认输出设备切换，准备迁移输出端点...")
            self._perform_hot_recovery()

    def _default_device_poll_due(self) -> bool:
        """默认设备轮询节流：间隔 _DEFAULT_DEVICE_POLL_INTERVAL 秒至多一次。"""
        now = time.monotonic()
        if now - self._last_device_poll_ts < _DEFAULT_DEVICE_POLL_INTERVAL:
            return False
        self._last_device_poll_ts = now
        return True

    # ==================== 内部：流 / Producer / 回调 ====================

    def _start_streaming(self) -> None:
        """启动 PortAudio stream + producer 线程。

        流建立后保持存活直到 release() 或热重载，play/pause/stop 均不销毁它。
        建流时指定 latency=_TARGET_LATENCY（建议值），实际分配值由 stream.latency
        读回并存入 _stream_latency_frames，用于 get_position_ms() 补偿。
        """
        if self._ring is None or self._original_data is None:
            return

        # 防御性清理：绝对不允许同一个实例开启两个 stream
        self._stop_streaming()

        # 对齐默认设备基线：自上次开流以来默认设备若已变化（含 load() 重载
        # 场景，#126），先刷新 PortAudio 设备表再开流，避免新流仍绑在旧
        # 默认设备上——此时本引擎的流已关闭，刷新只会联动关闭键音/节拍器
        # 的流池，随后按需重建。
        self._align_default_device_before_open()

        # 启动 producer
        self._producer_stop.clear()
        self._producer_thread = threading.Thread(
            target=self._producer_loop, daemon=True, name="AudioProducer"
        )
        self._producer_thread.start()

        # 启动 stream
        try:
            self._stream = sd.OutputStream(
                samplerate=self._sample_rate,
                channels=self._channels,
                dtype="float32",
                blocksize=_BLOCK_FRAMES,
                latency=_TARGET_LATENCY,
                callback=self._audio_callback,
            )
            self._stream.start()
            # 读回 PortAudio 实际分配的硬件延迟并转换为帧数，用于位置补偿。
            # stream.latency 是输出延迟（秒），即"回调写入 → 硬件实际播出"的固定延迟。
            actual_latency_s = self._stream.latency
            self._stream_latency_frames = int(actual_latency_s * self._sample_rate)
            print(
                f"[SoundDeviceEngine] stream ready: "
                f"requested latency={_TARGET_LATENCY*1000:.1f}ms, "
                f"actual={actual_latency_s*1000:.1f}ms "
                f"({self._stream_latency_frames} frames)"
            )
        except Exception as e:
            print(f"[SoundDeviceEngine] open stream failed: {e}")
            self._producer_stop.set()
            self._stream = None
            self._stream_latency_frames = 0

    def _stop_streaming(self) -> None:
        # 先停 stream（保证回调不再被调用）
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        # 再停 producer
        self._producer_stop.set()
        if self._producer_thread and self._producer_thread.is_alive():
            self._producer_thread.join(timeout=1.0)
        self._producer_thread = None

        if self._ring is not None:
            self._ring.reset()

    def _audio_callback(self, outdata, frames, time_info, status) -> None:  # noqa: D401
        """**实时回调**：极简、零分配、零锁、零 Python 重活。"""
        if status:
            if status.output_underflow:
                # underflow 只是说明 Python 喂数据慢了，属于正常性能抖动，不用管
                pass
            else:
                # 遇到了设备断开、采样率突变等致命状态
                print(f"[SoundDeviceEngine] 底层设备状态异常，请求热重载: {status}")
                self._needs_recovery.set()
                outdata.fill(0)
                return

        if self._state != PlaybackState.PLAYING:
            outdata.fill(0)
            return

        ring = self._ring
        if ring is None:
            outdata.fill(0)
            return

        n = ring.read_into(outdata)
        if n < frames:
            # underrun：补零，不抛错（旧版会触发 PortAudio 错位）
            outdata[n:].fill(0)

        # 更新消费者侧锚点（PortAudio 实时线程）。
        # += 不是原子操作（LOAD/ADD/STORE 可被 GIL 切开），与 seek/换源线程
        # 的锚点重置并发时会互相覆盖——用专用 _anchor_lock 串行（锁内仅两次
        # 赋值，回调线程可安全使用；时间戳与消费量成对更新保证外推一致性）。
        with self._anchor_lock:
            self._consumed_in_active_pcm += n
            self._last_callback_perf_time = time.perf_counter()

        if self._volume != 1.0:
            outdata *= self._volume

    def _producer_loop(self) -> None:
        """生产者：把 active PCM 切片送进 ring；处理速度切换 / EOF。"""
        # 提升本线程优先级，防止被 TSMWorker 等 CPU 密集线程抢占。
        # ABOVE_NORMAL 足够保证 ring 持续喂饱，不需要 HIGHEST（避免影响 UI）。
        _set_thread_priority(_THREAD_PRIORITY_ABOVE_NORMAL)

        if self._ring is None:
            return

        # 目标余量：保持 ring 至少有 _BLOCK_FRAMES * 2 帧可读
        target_low = _BLOCK_FRAMES * 2

        while not self._producer_stop.is_set():
            # 0) 看门狗：检测是否需要热重载 (设备切换、流崩溃或抛出异常)
            if self._state == PlaybackState.PLAYING and self._stream is not None:
                if self._needs_recovery.is_set() or not self._stream.active:
                    self._perform_hot_recovery()
                    continue

            # 0.5) 默认输出设备轮询（节流）：暂停/停止状态也要跟踪——流在
            # 整个文件生命周期内保持存活，停止期间切的设备不会触发 play()
            # 的补建分支（_stream.active 仍为 True），只能在这里发现。
            if self._default_device_poll_due():
                self._check_default_device_changed()

            # 1) 检查待切换的速度
            self._maybe_swap_active_speed()

            # 2) 暂停或停止时：不喂数据，回调输出零（流保持存活）
            if self._state in (PlaybackState.PAUSED, PlaybackState.STOPPED):
                time.sleep(_PRODUCER_TICK)
                continue

            # 3) 喂数据
            # epoch 快照 + 写入前后双检：read pos 之后、写 ring 之前可能有
            # seek/换源/load 插入（原先仅凭 active_pcm 身份比对，识别不了
            # "同一份 PCM 上的 seek"）——旧位置数据会写进 ring 且 seek 被吞。
            with self._state_lock:
                pcm = self._active_pcm
                pos = self._read_pos_samples
                epoch = self._epoch
                total = 0 if pcm is None else len(pcm)

            if pcm is None:
                time.sleep(_PRODUCER_TICK)
                continue

            free = self._ring.available_write()
            if free < target_low:
                time.sleep(_PRODUCER_TICK)
                continue

            remaining = total - pos
            if remaining <= 0:
                # EOF：暂停播放，保持流存活以便 seek 后继续播放
                self._on_eof()
                continue

            chunk_n = min(free, remaining)
            written = 0
            with self._state_lock:
                if self._epoch != epoch:
                    # 快照后发生了 seek/换源/load：本轮作废，下轮从新位置读
                    continue
                chunk = pcm[pos : pos + chunk_n]
                written = self._ring.write_from(chunk)
                # 仅当 epoch 未变时推进 pos，避免覆盖 seek/换源设置的新位置
                if written > 0 and self._epoch == epoch:
                    self._read_pos_samples += written

    def _maybe_swap_active_speed(self) -> None:
        """如有 pending 速度切换且新 PCM 已就绪，原子换 active 并保位。"""
        with self._state_lock:
            pending = _quantize(self._pending_speed)
            current = _quantize(self._active_speed)
            if abs(pending - current) < 1e-9:
                return
            cur_pcm = self._active_pcm
            cur_speed = self._active_speed
            cur_consumed = self._consumed_in_active_pcm

        # 检查缓存是否已就绪
        new_pcm = self._cache.get(pending)
        if new_pcm is None:
            return  # 还没渲染好，下次再试

        # 计算"当前原始时间轴位置" → 在新 PCM 上的偏移
        if cur_pcm is None or self._sample_rate == 0:
            return
        # orig_samples = consumed * cur_speed（原始时间轴位置）
        orig_samples = cur_consumed * cur_speed
        # 在新 PCM 上的位置 = orig_samples / pending
        new_consumed = int(orig_samples / max(pending, 1e-6))
        new_consumed = max(0, min(new_consumed, len(new_pcm)))

        with self._state_lock:
            self._active_pcm = new_pcm
            self._active_speed = pending  # 使用用户设置的速度
            self._read_pos_samples = new_consumed
            # epoch 递增：本轮换源使 producer 在途喂数据作废（同 seek）。
            self._epoch += 1
            # 锚点扣除 ring 里尚未消费的旧速度残留帧：回调会先把这段旧音频
            # 播完才开始消费新 PCM，若直接把锚点设为 new_consumed，位置会
            # 领先实际声音约"残留时长 × 速度"（ring 最多 ~500ms），且要等
            # 下次 seek 才能纠正。
            residual = self._ring.available_read() if self._ring is not None else 0
            with self._anchor_lock:
                self._consumed_in_active_pcm = max(0, new_consumed - residual)
                self._last_callback_perf_time = time.perf_counter()
            # 更新 pending_speed 为当前 speed，避免重复 swap
            self._pending_speed = self._speed

        # 不 reset ring buffer，让新旧 PCM 平滑过渡（扣除残留帧后位置轴
        # 与"先把旧音频播完再播新音频"的实际输出保持一致）

    def _on_eof(self) -> None:
        """active PCM 喂完了：等 ring 排空，置 PAUSED。

        播放结束后保持流存活，状态设为 PAUSED 而非 STOPPED，
        这样用户拖动时间戳后可以继续播放，无需重建流。
        """
        # 等待 ring 自然排空（最多 1s）
        if self._ring is not None:
            t0 = time.time()
            while self._ring.available_read() > 0 and time.time() - t0 < 1.0:
                if self._producer_stop.is_set():
                    return
                time.sleep(_PRODUCER_TICK)
        # D14 竞态守卫：排空期间 stop()/release() 可能已把状态置为 STOPPED
        # （stop() 的锁内段此时可能尚未跑到，但 _producer_stop 已在
        # _stop_streaming 里置位）——不得把 STOPPED 改写成 PAUSED。
        if self._producer_stop.is_set():
            return
        if self._state == PlaybackState.PLAYING:
            self._state = PlaybackState.PAUSED

    def _perform_hot_recovery(self) -> None:
        """执行音频流热重载（断线重连 / 默认设备切换）

        当检测到底层设备异常（如采样率突变、设备拔插），或系统默认输出
        设备变化（#126，producer 轮询发现）时：静默销毁旧流 → 等待设备
        稳定 → 刷新 PortAudio 设备表 → 在新默认设备上创建新流 → 恢复
        播放进度。整个过程对用户来说只是短暂卡顿，然后自动恢复正常。
        """
        print("[SoundDeviceEngine] 正在执行音频流热重载...")
        self._needs_recovery.clear()

        # 1. 记住当前的播放位置（毫秒）
        current_ms = self.get_position_ms()

        # 2. 强行销毁旧的异常流
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        # 3. 稍微等待，给操作系统和 PortAudio 留出切换默认音频端点的时间
        # (比如蓝牙切换协议通常需要几百毫秒)
        time.sleep(0.5)

        # 3.5) 停机守卫：release()/load() 已要求停机时没必要刷新设备表/重建流
        if self._producer_stop.is_set():
            return

        # 3.6) 刷新 PortAudio 设备表：macOS 上设备表在 Pa_Initialize 后不再
        # 自动更新，重开流必须先 terminate/initialize 才能解析到**当前**的
        # 默认设备（#126）。该调用会先让进程内其它 PortAudio 使用方
        # （键音/节拍器流池）关闭各自的流，避免 Pa_Terminate 摧毁在开的流。
        refresh_portaudio_devices()

        try:
            self._stream = sd.OutputStream(
                samplerate=self._sample_rate,
                channels=self._channels,
                dtype="float32",
                blocksize=_BLOCK_FRAMES,
                latency=_TARGET_LATENCY,
                callback=self._audio_callback,
            )
            # D14 竞态守卫：开流是耗时操作，期间 release()/load() 可能已调
            # _stop_streaming（其 join 超时后不再等待本线程）——此刻不能再
            # start 一个无人认领的新流，否则旧 producer 复活设备占用，
            # 与 _start_streaming 新起的 producer 形成双线程并发。
            if self._producer_stop.is_set():
                try:
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
                return
            self._stream.start()

            # 更新硬件延迟补偿帧数（新设备的 latency 可能与旧设备不同）
            actual_latency_s = self._stream.latency
            self._stream_latency_frames = int(actual_latency_s * self._sample_rate)
            # 更新默认设备基线（新流已绑定刷新后的默认设备），避免恢复完成
            # 后轮询再次判定"变化"造成循环恢复
            new_key = self._current_default_device_key()
            if new_key is not None:
                self._open_default_device_key = new_key
            print(
                f"[SoundDeviceEngine] 热重载 stream: "
                f"actual latency={actual_latency_s*1000:.1f}ms "
                f"({self._stream_latency_frames} frames)"
            )

            # 恢复播放头位置，内部会调用 self._ring.reset() 并清空旧的废弃缓冲
            self.set_position_ms(current_ms)

            print("[SoundDeviceEngine] 热重载成功！已恢复播放。")
        except Exception as e:
            print(f"[SoundDeviceEngine] 热重载失败: {e}")
            self._stream_latency_frames = 0
            # 如果实在救不回来，暂停播放，让用户后续手动点击播放重试
            self._state = PlaybackState.PAUSED


