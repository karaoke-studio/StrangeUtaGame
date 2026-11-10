"""TTML 导入丢弃内容提示。"""

from __future__ import annotations

from typing import Iterable, List, Optional

from PyQt6.QtCore import Qt
from qfluentwidgets import BodyLabel, CheckBox

from strange_uta_game.frontend.fluent_widgets import FluentMessageBox
from strange_uta_game.frontend.settings.app_settings import AppSettings

DROPPED_FEATURES_SUPPRESSED_KEY = "import.ttml_dropped_features_suppressed"


def emit_ttml_import_warnings(widget, features: Iterable[str]) -> None:
    """沿 parentWidget 链找到 MainWindow 并发出 ttml_import_warnings。"""
    values = [str(item) for item in features if item]
    if not values:
        return
    current = widget
    while current is not None:
        signal = getattr(current, "ttml_import_warnings", None)
        if signal is not None:
            try:
                signal.emit(values)
            except Exception:
                pass
            return
        parent_getter = getattr(current, "parentWidget", None)
        current = parent_getter() if callable(parent_getter) else None


class TtmlDroppedFeaturesDialog(FluentMessageBox):
    """项目标准 Fluent 消息框：列出 SUG 无法修改、TTML 导出会丢弃的内容。"""

    def __init__(self, parent, features: List[str]) -> None:
        super().__init__(
            self.tr("TTML 导入提示（Beta）"),
            self.tr(
                "AMLL/TTML 文件的以下内容无法在 SUG 中修改，"
                "导出为 TTML 时将被丢弃："
            ),
            parent,
        )
        self.hideCancelButton()
        self.yesButton.setText(self.tr("知道了"))
        self.setContentCopyable(True)
        self.contentLabel.setMinimumWidth(480)

        for feature in features:
            item = BodyLabel(f"• {feature}", self)
            item.setWordWrap(True)
            item.setMinimumWidth(480)
            item.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self.textLayout.addWidget(item)

        self.check_suppress = CheckBox(self.tr("不再提示"), self)
        self.textLayout.addWidget(self.check_suppress)

        self.textLayout.activate()
        self.vBoxLayout.activate()
        height = (
            self.textLayout.sizeHint().height()
            + self.buttonGroup.height()
            + 24
        )
        self.setFixedSize(560, max(220, min(680, height)))

    @property
    def suppress_future(self) -> bool:
        return self.check_suppress.isChecked()


def show_ttml_dropped_features(
    parent,
    features: Optional[Iterable[str]],
) -> bool:
    """展示提示；返回是否实际弹出了对话框。

    即使设置里已关闭提示，也仍会发出 ``ttml_import_warnings`` 信号，保证
    嵌入式宿主可以自行展示/记录，而不是被 SUG 的“不再提示”静默吞掉。
    """
    values = [str(item) for item in (features or []) if item]
    if not values:
        return False
    emit_ttml_import_warnings(parent, values)

    settings = AppSettings()
    if settings.get(DROPPED_FEATURES_SUPPRESSED_KEY, False):
        return False
    if parent is None:
        return False
    parent_visible = getattr(parent, "isVisible", None)
    if callable(parent_visible) and not parent_visible():
        # embedded 宿主可能把 SUG 切到后台页面但顶层窗口仍可见；此时不能
        # 用 SUG 自己的模态框盖住宿主 UI。
        return False
    dialog_parent = parent
    window_getter = getattr(parent, "window", None)
    if callable(window_getter):
        dialog_parent = window_getter()
    if dialog_parent is None or not dialog_parent.isVisible():
        return False
    is_minimized = getattr(dialog_parent, "isMinimized", None)
    if callable(is_minimized) and is_minimized():
        return False

    dialog = TtmlDroppedFeaturesDialog(dialog_parent, values)
    dialog.exec()
    if dialog.suppress_future:
        settings.set(DROPPED_FEATURES_SUPPRESSED_KEY, True)
        settings.save()
    return True
