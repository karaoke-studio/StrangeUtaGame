"""弹窗原生标题栏跟随应用主题的同步机制测试。

DWM 沉浸式暗色属性只对真实 Windows 窗口生效（DWMWA_USE_IMMERSIVE_DARK_MODE，
与主窗口 MSFluentWindow 同机制），测试进程跑 offscreen 平台没有原生 HWND，
无法断言属性值本身——该部分由真机冒烟验证。这里验证同步机制的触发路径：

- 弹窗显示时按当前主题应用（应用级 Show 事件过滤器，覆盖全部 QDialog）；
- 主题切换时对已打开弹窗全量重刷；
- 切换后新建的弹窗使用新主题；
- 非弹窗控件不触发。
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication, QDialog, QWidget

from strange_uta_game.frontend import theme as theme_mod
from strange_uta_game.frontend.theme import ThemeMode, theme


@pytest.fixture(scope="session")
def qapp():
    # 会话级持有 QApplication 引用：theme 单例先于 app 创建，若 app 在模块
    # 间被 GC，PyQt 会连带析构 theme 的 C++ 对象（下一个模块再操作
    # theme.mode 即 RuntimeError，改动前已复现的存量隐患）。生产环境 app
    # 不会中途销毁，仅测试需要。
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def titlebar_calls(monkeypatch):
    """把 DWM 落地函数替换为记录调用的替身，返回 calls 列表。"""
    calls: list = []

    def _fake(dialog, dark):
        calls.append((dialog, dark))

    monkeypatch.setattr(theme_mod, "_apply_native_titlebar_theme", _fake)
    return calls


@pytest.fixture()
def restore_mode():
    """保存/恢复主题模式，避免污染同会话其它测试。"""
    saved = theme.mode
    yield
    if theme.mode != saved:
        theme.mode = saved
        theme._invalidate()


def test_dialog_show_applies_current_theme(qapp, titlebar_calls, restore_mode):
    theme.mode = ThemeMode.DARK

    dlg = QDialog()
    dlg.show()
    qapp.processEvents()

    assert (dlg, True) in titlebar_calls
    dlg.close()


def test_theme_switch_refreshes_open_dialogs(qapp, titlebar_calls, restore_mode):
    dlg = QDialog()
    dlg.show()
    qapp.processEvents()

    titlebar_calls.clear()
    theme.mode = ThemeMode.DARK
    qapp.processEvents()

    assert (dlg, True) in titlebar_calls
    dlg.close()


def test_new_dialog_after_switch_uses_new_theme(qapp, titlebar_calls, restore_mode):
    theme.mode = ThemeMode.DARK
    titlebar_calls.clear()

    dlg = QDialog()
    dlg.show()
    qapp.processEvents()
    assert (dlg, True) in titlebar_calls
    dlg.close()

    titlebar_calls.clear()
    theme.mode = ThemeMode.LIGHT

    dlg2 = QDialog()
    dlg2.show()
    qapp.processEvents()
    assert (dlg2, False) in titlebar_calls
    dlg2.close()


def test_plain_widget_show_does_not_trigger(qapp, titlebar_calls, restore_mode):
    theme.mode = ThemeMode.DARK

    w = QWidget()
    w.show()
    qapp.processEvents()

    assert not any(
        isinstance(obj, QWidget) and not isinstance(obj, QDialog)
        for obj, _dark in titlebar_calls
    )
    w.close()
