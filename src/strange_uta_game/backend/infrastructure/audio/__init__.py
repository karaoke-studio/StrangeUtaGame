"""Audio module.

BASS 引擎仅 Windows 可用（依赖 bass.dll / bass_fx.dll）。本包以
``bass_available`` 暴露「BASS 能否导入」这一能力信号：mac 等无 BASS 的平台
上导入失败时回落到 ``SoundDeviceEngine``（已内置 TSM 变速），Windows 上保持
原有 BASS 行为不变。
"""

from __future__ import annotations

import os

from .base import (
    IAudioEngine,
    AudioError,
    AudioLoadError,
    AudioPlaybackError,
    PlaybackState,
    AudioInfo,
)
# sounddevice 是播放引擎依赖：AI Runtime 等无播放组件的环境里缺失时，
# 不能让整个 backend 包导入失败（曾导致对齐 worker 启动即崩）。
try:
    from .sounddevice_engine import SoundDeviceEngine
except ImportError:  # pragma: no cover - 环境相关
    SoundDeviceEngine = None  # type: ignore[assignment,misc]

# BASS 能力信号：仅在 Windows（打包了 bass.dll）上为 True。导入失败时静默回落，
# 绝不让应用启动失败。开发期在 mac 上从源码运行时同样为 False（DLL 物理不存在）。
# 注意异常类型：bass_engine.py 在非 win32 上用 _DummyCDLL 占位（CDLL() 不报错），
# 但模块级 `_bass.BASS_Init.restype = ...` 会因 dummy 无该属性抛 AttributeError；
# win32 缺 DLL 时 ctypes.CDLL 抛 OSError。三者都要捕获。
bass_available = False
try:
    from .bass_engine import BassEngine
    from .bass_tsm_engine import BassTsmEngine

    bass_available = True
except (ImportError, OSError, AttributeError):
    BassEngine = None  # type: ignore[assignment,misc]
    BassTsmEngine = None  # type: ignore[assignment,misc]


def select_audio_engine(hq_enabled: bool) -> IAudioEngine:
    """按 BASS 可用性选择音频引擎实例。

    - BASS 不可用（mac 等）：始终返回 ``SoundDeviceEngine``（其内置 TSMRenderCache
      承担 HQ 变速，HQ 开关在此平台为 no-op）。
    - BASS 可用（Windows）：HQ 开 → ``BassTsmEngine``（离线预渲染）；关 → ``BassEngine``。

    调试开关 ``SUG_FORCE_SD_AUDIO=1``：强制使用回退引擎。用于在 Windows 上
    实测回退引擎的行为（如 #126 默认输出设备切换跟随）——BASS 平台平时
    走不到该引擎，只能这样打开。正常发布路径不受影响。
    """
    if os.environ.get("SUG_FORCE_SD_AUDIO") and SoundDeviceEngine is not None:
        return SoundDeviceEngine()
    if bass_available and BassEngine is not None and BassTsmEngine is not None:
        return BassTsmEngine() if hq_enabled else BassEngine()
    return SoundDeviceEngine()


__all__ = [
    "IAudioEngine",
    "AudioError",
    "AudioLoadError",
    "AudioPlaybackError",
    "PlaybackState",
    "AudioInfo",
    "SoundDeviceEngine",
    "bass_available",
    "select_audio_engine",
]
if bass_available:
    __all__ += ["BassEngine", "BassTsmEngine"]
