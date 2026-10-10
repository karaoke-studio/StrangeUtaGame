"""SoundDeviceEngine 默认输出设备切换跟随（#126）回归测试。

不触碰真实音频设备/PortAudio：``OutputStream``、``time.sleep``、
``refresh_portaudio_devices`` 全部打桩（沿用 test_sounddevice_position 的
测试模式）。

背景（#126）：流不带 device 参数打开，绑定的是"开流那一刻"的系统默认
设备；macOS 上 PortAudio 设备表不自动更新，默认设备切换后既无回调错误
流也不死，只能主动轮询比对并在变化时刷新设备表 + 重建流。
"""

from __future__ import annotations

import threading

import numpy as np
import pytest

import strange_uta_game.backend.infrastructure.audio.sounddevice_engine as sde
from strange_uta_game.backend.infrastructure.audio.base import PlaybackState
from strange_uta_game.backend.infrastructure.audio.ring_buffer import RingBuffer
from strange_uta_game.backend.infrastructure.audio.sounddevice_engine import (
    SoundDeviceEngine,
)

_SR = 44100


def _make_engine(seconds: float = 2.0) -> SoundDeviceEngine:
    engine = SoundDeviceEngine()
    # 测试不碰音频设备
    engine._start_streaming = lambda: None
    n = int(_SR * seconds)
    pcm = (np.random.RandomState(11).randn(n, 2) * 0.1).astype(np.float32)
    engine._original_data = pcm
    engine._sample_rate = _SR
    engine._channels = 2
    engine._duration_ms = int(seconds * 1000)
    engine._active_pcm = pcm
    engine._active_speed = 1.0
    engine._speed = 1.0
    engine._pending_speed = 1.0
    engine._ring = RingBuffer(int(0.5 * _SR), 2)
    return engine


class _FakeOutputStream:
    """记录 stop/close 调用的假 OutputStream（带 start/latency）。"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.stop_calls = 0
        self.close_calls = 0
        self.start_calls = 0
        self.active = True
        # 0 延迟：位置断言不受硬件延迟补偿扣减影响
        self.latency = 0.0

    def start(self):
        self.start_calls += 1

    def stop(self):
        self.stop_calls += 1

    def close(self):
        self.close_calls += 1


@pytest.fixture
def refresh_calls(monkeypatch):
    """打桩引擎模块内的设备表刷新，记录调用。"""
    calls: list[bool] = []
    monkeypatch.setattr(sde, "refresh_portaudio_devices", lambda: calls.append(True))
    return calls


class TestDefaultDeviceBaseline:
    def test_first_poll_adopts_baseline_without_recovery(self, monkeypatch):
        engine = _make_engine()
        recoveries: list[int] = []
        monkeypatch.setattr(engine, "_perform_hot_recovery", lambda: recoveries.append(1))

        engine.set_default_output_device_provider(lambda: "dev-a")
        engine._check_default_device_changed()

        assert engine._open_default_device_key == "dev-a"
        assert recoveries == []

    def test_same_key_keeps_silent(self, monkeypatch):
        cell = {"key": "dev-a"}
        engine = _make_engine()
        recoveries: list[int] = []
        monkeypatch.setattr(engine, "_perform_hot_recovery", lambda: recoveries.append(1))
        engine._default_device_provider = lambda: cell["key"]

        engine._check_default_device_changed()  # 采纳基线
        engine._check_default_device_changed()  # 同值

        assert engine._open_default_device_key == "dev-a"
        assert recoveries == []

    def test_provider_none_skips_check(self, monkeypatch):
        engine = _make_engine()
        recoveries: list[int] = []
        monkeypatch.setattr(engine, "_perform_hot_recovery", lambda: recoveries.append(1))
        engine._open_default_device_key = "dev-a"
        engine._default_device_provider = lambda: None

        engine._check_default_device_changed()

        assert recoveries == []
        assert engine._open_default_device_key == "dev-a"

    def test_changed_key_triggers_recovery(self, monkeypatch):
        engine = _make_engine()
        recoveries: list[int] = []
        monkeypatch.setattr(engine, "_perform_hot_recovery", lambda: recoveries.append(1))
        cell = {"key": "dev-a"}
        engine._default_device_provider = lambda: cell["key"]

        engine._check_default_device_changed()  # 采纳基线 dev-a
        cell["key"] = "dev-b"  # 模拟前端轮询到新默认设备（如插入耳机）
        engine._check_default_device_changed()

        assert recoveries == [1]

    def test_reinstalling_provider_rebases(self, monkeypatch):
        """注入/撤销 provider 重置基线，不触发恢复（下一轮直接采纳）。"""
        engine = _make_engine()
        recoveries: list[int] = []
        monkeypatch.setattr(engine, "_perform_hot_recovery", lambda: recoveries.append(1))
        engine._default_device_provider = lambda: "dev-a"
        engine._check_default_device_changed()

        engine.set_default_output_device_provider(lambda: "dev-b")
        engine._check_default_device_changed()

        assert engine._open_default_device_key == "dev-b"
        assert recoveries == []


class TestAlignBeforeOpen:
    def test_stale_baseline_refreshes_portaudio(self, refresh_calls):
        """load() 重开流场景：基线过期必须先刷新设备表（#126"重载也没用"）。"""
        engine = _make_engine()
        engine._open_default_device_key = ("pa", "old-device")
        engine._default_device_provider = lambda: "dev-b"

        engine._align_default_device_before_open()

        assert refresh_calls == [True]
        assert engine._open_default_device_key == "dev-b"

    def test_matching_baseline_skips_refresh(self, refresh_calls):
        engine = _make_engine()
        engine._open_default_device_key = "dev-b"
        engine._default_device_provider = lambda: "dev-b"

        engine._align_default_device_before_open()

        assert refresh_calls == []

    def test_no_baseline_adopts_without_refresh(self, refresh_calls):
        engine = _make_engine()
        engine._open_default_device_key = None
        engine._default_device_provider = lambda: "dev-a"

        engine._align_default_device_before_open()

        assert refresh_calls == []
        assert engine._open_default_device_key == "dev-a"


class TestDeviceChangeHotRecovery:
    def test_recovery_refreshes_rebuilds_and_restores_position(
        self, monkeypatch, refresh_calls
    ):
        """设备切换 → 热重载：刷新设备表、旧流关闭、新流重建、基线与位置恢复。"""
        created: list[_FakeOutputStream] = []

        def _factory(**kwargs):
            stream = _FakeOutputStream(**kwargs)
            created.append(stream)
            return stream

        monkeypatch.setattr(sde.sd, "OutputStream", _factory)
        monkeypatch.setattr(sde.time, "sleep", lambda *_: None)

        engine = _make_engine()
        old_stream = _FakeOutputStream()
        engine._stream = old_stream
        engine._state = PlaybackState.PLAYING
        cell = {"key": "dev-a"}
        engine._default_device_provider = lambda: cell["key"]
        engine._check_default_device_changed()  # 采纳基线
        engine.set_position_ms(1000)
        position_before = engine.get_position_ms()

        cell["key"] = "dev-b"  # 默认设备切换
        engine._check_default_device_changed()

        assert refresh_calls == [True], "热重载必须刷新 PortAudio 设备表"
        assert old_stream.close_calls == 1, "旧流必须销毁"
        assert len(created) == 1 and engine._stream is created[0]
        assert created[0].kwargs.get("callback") is not None
        assert engine._open_default_device_key == "dev-b", "基线更新，避免循环恢复"
        assert abs(engine.get_position_ms() - position_before) < 50, "播放位置应恢复"

    def test_producer_loop_detects_change_while_stopped(self, monkeypatch, refresh_calls):
        """停止状态（流保持存活）下，producer 轮询也要能发现默认设备切换。"""
        engine = _make_engine()
        recovered = threading.Event()
        monkeypatch.setattr(engine, "_perform_hot_recovery", recovered.set)
        engine._stream = _FakeOutputStream()
        engine._state = PlaybackState.STOPPED
        engine._open_default_device_key = "dev-a"
        engine._default_device_provider = lambda: "dev-b"
        engine._last_device_poll_ts = 0.0  # 强制首轮轮询立即到期

        engine._producer_stop.clear()
        t = threading.Thread(target=engine._producer_loop, daemon=True)
        t.start()
        try:
            assert recovered.wait(timeout=5.0), "producer 应在轮询中发现设备切换"
        finally:
            engine._producer_stop.set()
            t.join(timeout=2.0)

    def test_device_poll_is_throttled(self):
        engine = _make_engine()
        engine._last_device_poll_ts = 0.0
        assert engine._default_device_poll_due() is True
        # 紧接着的第二次在节流间隔内，不再到期
        assert engine._default_device_poll_due() is False
