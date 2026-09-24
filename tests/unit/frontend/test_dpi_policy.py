import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from strange_uta_game.frontend.dpi_policy import (
    HIGH_DPI_SCALING_KEY,
    WINDOWS_DPI_UNAWARE_PLATFORM,
    build_qt_argv,
)
from strange_uta_game.frontend.settings.app_settings import AppSettings
from strange_uta_game.frontend.settings.sub_interfaces.timing import (
    TimingSubInterface,
)


class _SettingsStub:
    def __init__(self, high_dpi_scaling=True, *, provider=None):
        self._provider = provider
        self.values = {HIGH_DPI_SCALING_KEY: high_dpi_scaling}
        self.save_count = 0

    def get(self, path, default=None):
        return self.values.get(path, default)

    def set(self, path, value):
        self.values[path] = value

    def save(self):
        self.save_count += 1


def test_high_dpi_scaling_is_enabled_by_default():
    assert AppSettings.DEFAULT_SETTINGS["ui"]["high_dpi_scaling"] is True


def test_enabled_high_dpi_keeps_qt_arguments_unchanged():
    original = ["StrangeUtaGame.exe", "song.sug"]

    actual = build_qt_argv(
        original,
        high_dpi_scaling=True,
        platform="win32",
    )

    assert actual == original
    assert actual is not original


def test_disabled_high_dpi_uses_windows_system_scaling():
    original = ["StrangeUtaGame.exe", "song.sug"]

    actual = build_qt_argv(
        original,
        high_dpi_scaling=False,
        platform="win32",
    )

    assert actual == [
        "StrangeUtaGame.exe",
        "-platform",
        WINDOWS_DPI_UNAWARE_PLATFORM,
        "song.sug",
    ]
    assert original == ["StrangeUtaGame.exe", "song.sug"]


def test_disabled_high_dpi_does_not_change_other_platforms():
    original = ["StrangeUtaGame", "song.sug"]

    assert (
        build_qt_argv(
            original,
            high_dpi_scaling=False,
            platform="darwin",
        )
        == original
    )


def test_disabled_high_dpi_handles_empty_arguments():
    assert build_qt_argv(
        [],
        high_dpi_scaling=False,
        platform="win32",
    ) == ["StrangeUtaGame", "-platform", WINDOWS_DPI_UNAWARE_PLATFORM]


def test_platform_default_matches_running_python():
    original = ["StrangeUtaGame"]
    actual = build_qt_argv(original, high_dpi_scaling=False)
    if sys.platform == "win32":
        assert WINDOWS_DPI_UNAWARE_PLATFORM in actual
    else:
        assert actual == original


def test_timing_switch_is_enabled_and_saved_immediately_on_windows(qapp, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    settings = _SettingsStub(high_dpi_scaling=True)
    page = TimingSubInterface()
    page.connect_signals()

    page.load_settings(settings)
    assert page.card_high_dpi_scaling.isChecked()
    assert not page.card_high_dpi_scaling.isHidden()
    assert settings.save_count == 0

    page.card_high_dpi_scaling.setChecked(False)

    assert settings.values[HIGH_DPI_SCALING_KEY] is False
    assert settings.save_count == 1
    page.deleteLater()
    qapp.processEvents()


def test_timing_switch_is_hidden_when_sug_is_embedded(qapp, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    settings = _SettingsStub(high_dpi_scaling=True)
    page = TimingSubInterface(embedded=True)
    page.connect_signals()

    page.load_settings(settings)

    assert page.card_high_dpi_scaling.isHidden()

    # Hidden host-owned settings must neither persist immediately nor leak
    # through the normal collect pass.
    page.card_high_dpi_scaling.setChecked(False)
    page.collect_settings(settings)
    assert settings.values[HIGH_DPI_SCALING_KEY] is True
    assert settings.save_count == 0
    page.deleteLater()
    qapp.processEvents()


def test_timing_switch_keeps_provider_based_embedded_compatibility(qapp, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    settings = _SettingsStub(high_dpi_scaling=True, provider=object())
    page = TimingSubInterface()

    page.load_settings(settings)

    assert page.card_high_dpi_scaling.isHidden()
    page.deleteLater()
    qapp.processEvents()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPI API only")
@pytest.mark.parametrize(
    ("enabled", "expected_context"),
    [(True, -4), (False, -1)],
    ids=["per-monitor-v2", "dpi-unaware"],
)
def test_qt_process_uses_selected_windows_dpi_context(enabled, expected_context):
    """在独立进程验证 Qt 真正提交给 Windows 的 DPI awareness。"""
    script = textwrap.dedent(f"""
        import ctypes

        from PyQt6.QtWidgets import QApplication
        from strange_uta_game.frontend.dpi_policy import build_qt_argv

        app = QApplication(build_qt_argv(
            ["dpi-probe"], high_dpi_scaling={enabled!r}
        ))
        user32 = ctypes.WinDLL("user32")
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.AreDpiAwarenessContextsEqual.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p
        ]
        user32.AreDpiAwarenessContextsEqual.restype = ctypes.c_bool
        actual = user32.GetThreadDpiAwarenessContext()
        expected = ctypes.c_void_p({expected_context})
        raise SystemExit(
            0 if user32.AreDpiAwarenessContextsEqual(actual, expected) else 1
        )
        """)
    repo_root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    # pytest-qt/conftest may force the offscreen QPA plugin; this probe needs
    # the real Windows plugin because that is what owns dpi awareness.
    env.pop("QT_QPA_PLATFORM", None)
    old_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(repo_root / "src"), old_pythonpath) if part
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
