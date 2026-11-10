"""TTML 导入丢弃内容提示。"""

from __future__ import annotations

from typing import Iterable, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

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


class TtmlDroppedFeaturesDialog(QDialog):
    """列出 SUG 无法修改、TTML 导出会丢弃的内容，并允许不再提示。"""

    def __init__(self, parent, features: List[str]) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("TTML 导入提示"))
        self.setModal(True)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        intro = QLabel(
            self.tr(
                "AMLL/TTML 文件的以下内容无法在 SUG 中修改，导出为 TTML 时将被丢弃："
            ),
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        for feature in features:
            item = QLabel(f"• {feature}", self)
            item.setWordWrap(True)
            item.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            layout.addWidget(item)

        self.check_suppress = QCheckBox(self.tr("不再提示"), self)
        layout.addWidget(self.check_suppress)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        btn_ok = QPushButton(self.tr("知道了"), self)
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self.accept)
        buttons.addWidget(btn_ok)
        layout.addLayout(buttons)

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
