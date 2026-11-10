"""TTML 导入丢弃内容提示的单元测试。"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

from strange_uta_game.frontend.editor.timing.ttml_notice import (
    TtmlDroppedFeaturesDialog,
    emit_ttml_import_warnings,
    show_ttml_dropped_features,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _Signal:
    def __init__(self) -> None:
        self.values = None

    def emit(self, values) -> None:
        self.values = values


class _Widget:
    def __init__(self) -> None:
        self.ttml_import_warnings = _Signal()

    def parentWidget(self):
        return None


def test_emit_ttml_import_warnings_reaches_parent_chain():
    widget = _Widget()
    emit_ttml_import_warnings(widget, ["tts 样式", ""])
    assert widget.ttml_import_warnings.values == ["tts 样式"]


def test_hidden_or_missing_parent_does_not_open_dialog():
    assert show_ttml_dropped_features(None, ["tts 样式"]) is False
    assert show_ttml_dropped_features(None, []) is False

    class _Hidden:
        def isVisible(self):
            return False

        def window(self):
            return type("Window", (), {"isVisible": lambda self: True})()

    assert show_ttml_dropped_features(_Hidden(), ["tts 样式"]) is False


def test_dialog_lists_features_and_suppress_checkbox(qapp):
    from strange_uta_game.frontend.fluent_widgets import FluentMessageBox

    dialog = TtmlDroppedFeaturesDialog(
        None, ["字体、颜色、位置、图片等 tts 样式", "Apple Music 头部翻译 / 音译 sidecar"]
    )
    assert isinstance(dialog, FluentMessageBox)
    assert dialog.cancelButton.isHidden()
    assert dialog.yesButton.text() == "知道了"
    assert "不再提示" == dialog.check_suppress.text()
    assert dialog.suppress_future is False
    dialog.check_suppress.setChecked(True)
    assert dialog.suppress_future is True
    dialog.deleteLater()
