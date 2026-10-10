"""主题化文件弹窗（fluent_widgets.themed_get_* 包装）测试。

Windows 原生文件弹窗无法跟随应用主题，包装改为 Qt 自绘
（DontUseNativeDialog），由 theme._DialogTitleBarThemeFilter 在弹窗
Show 时套上随主题 QSS（标题栏明暗亦由该过滤器同步）。这里验证：

- 包装函数把自绘选项传给 QFileDialog 静态方法（测试 monkeypatch 类
  属性仍可拦截，与现有用例的 mock 手法兼容）；
- 过滤器对自绘文件弹窗套 QSS，对原生模式不套；
- QSS 深浅两套取色正确。
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication, QFileDialog

from strange_uta_game.frontend import fluent_widgets
from strange_uta_game.frontend import theme as theme_mod
from strange_uta_game.frontend.theme import ThemeMode, theme


@pytest.fixture(scope="session")
def qapp():
    # 会话级持有 QApplication 引用：theme 单例先于 app 创建，若 app 在模块
    # 间被 GC，PyQt 会连带析构 theme 的 C++ 对象（存量隐患，见
    # test_theme_titlebar_sync 同名 fixture）。
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def restore_mode():
    saved = theme.mode
    yield
    if theme.mode != saved:
        theme.mode = saved
        theme._invalidate()


def test_options_are_native_off_windows(monkeypatch):
    monkeypatch.setattr(fluent_widgets.sys, "platform", "darwin")
    assert fluent_widgets._themed_file_dialog_options() == QFileDialog.Option(0)


def test_options_are_non_native_on_windows(monkeypatch):
    monkeypatch.setattr(fluent_widgets.sys, "platform", "win32")
    assert (
        fluent_widgets._themed_file_dialog_options()
        == QFileDialog.Option.DontUseNativeDialog
    )


@pytest.mark.skipif(
    fluent_widgets.sys.platform != "win32", reason="仅 Windows 用自绘弹窗"
)
def test_wrapper_passes_non_native_option_and_class_mock_still_intercepts(
    qapp, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *a, **k: calls.append((a, k)) or ("", "")),
    )
    result = fluent_widgets.themed_get_open_file_name(None, "标题", "/tmp", "*.txt")
    assert result == ("", "")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:4] == (None, "标题", "/tmp", "*.txt")
    assert QFileDialog.Option.DontUseNativeDialog in kwargs.get(
        "options", QFileDialog.Option(0)
    ) or args[5:] == (QFileDialog.Option.DontUseNativeDialog,)


def test_filter_applies_qss_to_non_native_dialog(qapp, restore_mode):
    theme.mode = ThemeMode.DARK

    dlg = QFileDialog()
    dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dlg.show()
    qapp.processEvents()

    qss = dlg.styleSheet()
    assert "QLineEdit" in qss and "QPushButton" in qss
    dark = theme_mod._build_file_dialog_qss(theme)
    assert qss == dark
    dlg.close()


def test_filter_skips_native_mode_dialog(qapp, restore_mode):
    theme.mode = ThemeMode.DARK

    dlg = QFileDialog()
    dlg.show()  # 未设 DontUseNativeDialog（offscreen 下无原生面板，仅验证不套 QSS）
    qapp.processEvents()

    assert dlg.styleSheet() == ""
    dlg.close()


def test_qss_follows_light_dark_colors():
    theme._colors = theme_mod.ThemeColors(True)
    dark_qss = theme_mod._build_file_dialog_qss(theme)
    theme._colors = theme_mod.ThemeColors(False)
    light_qss = theme_mod._build_file_dialog_qss(theme)
    theme._invalidate()

    assert dark_qss != light_qss
    # ThemeColors：深色 bg_tertiary=#2d2d2d，浅色=#ffffff（QColor.name() 小写）
    assert "#2d2d2d" in dark_qss
    assert "#ffffff" in light_qss
