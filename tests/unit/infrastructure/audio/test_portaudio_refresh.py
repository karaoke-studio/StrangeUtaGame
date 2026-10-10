"""portaudio_refresh 协调器单元测试（#126）。

不触碰真实 PortAudio：``sd._terminate/_initialize`` 打桩并记录调用顺序，
监听器注册表逐用例隔离，避免与其它用例（真实登记的流池）串扰。
"""

from __future__ import annotations

import threading

import pytest

import strange_uta_game.backend.infrastructure.audio.keysound_player as kp
import strange_uta_game.backend.infrastructure.audio.portaudio_refresh as par
from strange_uta_game.backend.infrastructure.audio.keysound_player import (
    SdSampleStreamPool,
)


@pytest.fixture(autouse=True)
def _stub_portaudio(monkeypatch):
    """terminate/initialize 打桩并记录顺序；用独立注册表避免用例间串扰。"""
    order: list[str] = []
    monkeypatch.setattr(par, "_listeners", [])
    monkeypatch.setattr(par, "_lock", threading.RLock())
    monkeypatch.setattr(par, "_refreshing", False)
    monkeypatch.setattr(par.sd, "_terminate", lambda: order.append("terminate"))
    monkeypatch.setattr(par.sd, "_initialize", lambda: order.append("initialize"))
    return order


class TestRefreshCoordination:
    def test_listeners_called_before_terminate_initialize(self, _stub_portaudio):
        """硬性顺序：先关流（监听器）再 terminate/initialize（#126 崩溃风险点）。"""
        calls: list[str] = []
        par.register_refresh_listener(lambda: calls.append("l1"))
        par.register_refresh_listener(lambda: calls.append("l2"))

        assert par.refresh_portaudio_devices() is True

        assert calls == ["l1", "l2"]
        assert _stub_portaudio == ["terminate", "initialize"]

    def test_duplicate_registration_ignored(self, _stub_portaudio):
        calls: list[int] = []

        def listener() -> None:
            calls.append(1)

        par.register_refresh_listener(listener)
        par.register_refresh_listener(listener)
        par.refresh_portaudio_devices()

        assert calls == [1]

    def test_failing_listener_does_not_block_refresh(self, _stub_portaudio):
        """单个监听器异常只影响它自己，刷新必须继续（否则设备表永远陈旧）。"""
        seen: list[str] = []

        def bad() -> None:
            raise RuntimeError("listener boom")

        par.register_refresh_listener(bad)
        par.register_refresh_listener(lambda: seen.append("ok"))
        assert par.refresh_portaudio_devices() is True
        assert seen == ["ok"]
        assert _stub_portaudio == ["terminate", "initialize"]

    def test_in_progress_visible_inside_listener_and_cleared_after(self, _stub_portaudio):
        seen: list[bool] = []
        par.register_refresh_listener(
            lambda: seen.append(par.portaudio_refresh_in_progress())
        )

        par.refresh_portaudio_devices()

        assert seen == [True]
        assert par.portaudio_refresh_in_progress() is False

    def test_unregister_stops_notifications(self, _stub_portaudio):
        calls: list[int] = []
        listener = lambda: calls.append(1)  # noqa: E731
        par.register_refresh_listener(listener)
        par.unregister_refresh_listener(listener)
        par.refresh_portaudio_devices()
        assert calls == []
        # 未登记的注销不报错
        par.unregister_refresh_listener(listener)

    def test_initialize_failure_returns_false(self, _stub_portaudio, monkeypatch):
        def _boom() -> None:
            raise RuntimeError("init boom")

        monkeypatch.setattr(par.sd, "_initialize", _boom)
        assert par.refresh_portaudio_devices() is False
        # terminate 仍已执行（配平尝试）
        assert _stub_portaudio == ["terminate"]


class _FakePoolStream:
    def __init__(self) -> None:
        self.stop_calls = 0
        self.close_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class TestPoolIntegration:
    def test_pool_streams_closed_on_refresh(self, _stub_portaudio):
        """刷新联动：池内所有流被 stop/close，参数复位（play 惰性重建）。"""
        pool = SdSampleStreamPool(size=2)
        fakes = [_FakePoolStream(), _FakePoolStream()]
        pool._streams = list(fakes)
        pool._sr = 44100
        pool._ch = 1

        assert par.refresh_portaudio_devices() is True

        assert all(s.stop_calls == 1 and s.close_calls == 1 for s in fakes)
        assert pool._streams == [None, None]
        assert pool._sr == 0 and pool._ch == 0

    def test_pool_close_unregisters_listener(self, _stub_portaudio):
        pool = SdSampleStreamPool(size=2)
        assert par._listeners, "构造即登记刷新监听"
        pool.close()
        assert par._listeners == []

    def test_pool_survives_refresh_and_stays_registered(self, _stub_portaudio):
        """_close_for_refresh 不得注销自己：刷新后池仍受后续刷新联动。"""
        pool = SdSampleStreamPool(size=1)
        par.refresh_portaudio_devices()
        assert pool._refresh_listener in par._listeners

    def test_play_dropped_inside_refresh_window(self, _stub_portaudio, monkeypatch):
        """刷新窗口内的播放直接丢弃，不得新开流（也不退回 sd.play）。"""
        created: list = []

        class _Never:
            def __init__(self, **kwargs) -> None:
                created.append(kwargs)

        monkeypatch.setattr(kp._sd, "OutputStream", _Never)
        monkeypatch.setattr(kp._sd, "play", lambda *a, **k: created.append("sd.play"))
        pool = SdSampleStreamPool(size=2)

        par._refreshing = True
        try:
            import numpy as np

            pool.play(np.zeros(8, dtype="float32"), 44100)
        finally:
            par._refreshing = False

        assert created == []
        assert pool._streams == [None, None]
