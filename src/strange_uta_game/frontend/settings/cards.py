"""设置卡片自定义组件。

基于 qfluentwidgets 的 ``SettingCard`` 扩展出的卡片类型，供 ``SettingsInterface`` 使用。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QKeyEvent, QWheelEvent
from strange_uta_game.frontend.font_utils import ui_font
from PyQt6.QtWidgets import QDialog, QFileDialog, QLabel, QVBoxLayout, QWidget
from strange_uta_game.frontend.theme import theme
from strange_uta_game.frontend.fluent_widgets import dialog_button_row
from qfluentwidgets import (
    CheckBox,
    ComboBox,
    DoubleSpinBox,
    LineEdit,
    PushButton,
    SettingCard,
    SpinBox,
    SwitchButton,
)


class NoWheelSpinBox(SpinBox):
    """禁用滚轮的 SpinBox"""

    def wheelEvent(self, event: QWheelEvent | None) -> None:
        event.ignore() if event else None


class NoWheelDoubleSpinBox(DoubleSpinBox):
    """禁用滚轮的 DoubleSpinBox"""

    def wheelEvent(self, event: QWheelEvent | None) -> None:
        event.ignore() if event else None


class SpinSettingCard(SettingCard):
    """数值设定卡片 — 整数 SpinBox"""

    value_changed = pyqtSignal(int)

    def __init__(
        self,
        icon,
        title: str,
        content: str,
        min_val: int = 0,
        max_val: int = 100,
        step: int = 1,
        suffix: str = "",
        parent=None,
    ):
        super().__init__(icon, title, content, parent)
        self.spin = NoWheelSpinBox(self)
        self.spin.setRange(min_val, max_val)
        self.spin.setSingleStep(step)
        if suffix:
            self.spin.setSuffix(suffix)
        # 用 minimumWidth 而非 fixedWidth：long suffix（如本地化后的 "minutes" / "ミリ秒"）
        # 或大数值可让控件自然变宽，不会被截断。
        self.spin.setMinimumWidth(180)
        self.spin.valueChanged.connect(self.value_changed.emit)
        self.hBoxLayout.addWidget(self.spin, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

    def setValue(self, value: int):
        self.spin.setValue(value)

    def value(self) -> int:
        return self.spin.value()


class DoubleSpinSettingCard(SettingCard):
    """数值设定卡片 — 浮点 DoubleSpinBox"""

    value_changed = pyqtSignal(float)

    def __init__(
        self,
        icon,
        title: str,
        content: str,
        min_val: float = 0.0,
        max_val: float = 100.0,
        step: float = 0.1,
        decimals: int = 1,
        suffix: str = "",
        parent=None,
    ):
        super().__init__(icon, title, content, parent)
        self.spin = NoWheelDoubleSpinBox(self)
        self.spin.setRange(min_val, max_val)
        self.spin.setSingleStep(step)
        self.spin.setDecimals(decimals)
        if suffix:
            self.spin.setSuffix(suffix)
        self.spin.setMinimumWidth(180)  # 见 SpinSettingCard 同位置注释
        self.spin.valueChanged.connect(self.value_changed.emit)
        self.hBoxLayout.addWidget(self.spin, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

    def setValue(self, value: float):
        self.spin.setValue(value)

    def value(self) -> float:
        return self.spin.value()


class TextSettingCard(SettingCard):
    """文本输入设定卡片"""

    value_changed = pyqtSignal(str)

    def __init__(
        self,
        icon,
        title: str,
        content: str,
        placeholder: str = "",
        max_length: int = 10,
        parent=None,
    ):
        super().__init__(icon, title, content, parent)
        self.line_edit = LineEdit(self)
        self.line_edit.setPlaceholderText(placeholder)
        self.line_edit.setMaxLength(max_length)
        self.line_edit.setMinimumWidth(180)
        self.line_edit.textChanged.connect(self.value_changed.emit)
        self.hBoxLayout.addWidget(self.line_edit, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

    def setValue(self, value: str):
        self.line_edit.setText(value)

    def value(self) -> str:
        return self.line_edit.text()


class SwitchSettingCard(SettingCard):
    """开关设定卡片"""

    checked_changed = pyqtSignal(bool)

    def __init__(self, icon, title: str, content: str, parent=None):
        super().__init__(icon, title, content, parent)
        self.switch = SwitchButton(self)
        # 显式覆盖默认 "On"/"Off"——pseudo 模式靠 self.tr 才能可视化 ⟦⟧。
        self.switch.setOnText(self.tr("开"))
        self.switch.setOffText(self.tr("关"))
        self.switch.checkedChanged.connect(self.checked_changed.emit)
        self.hBoxLayout.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

    def changeEvent(self, event):
        from PyQt6.QtCore import QEvent as _QEvent
        if event.type() == _QEvent.Type.LanguageChange and hasattr(self, "switch"):
            self.switch.setOnText(self.tr("开"))
            self.switch.setOffText(self.tr("关"))
        super().changeEvent(event)

    def setChecked(self, checked: bool):
        self.switch.setChecked(checked)

    def isChecked(self) -> bool:
        return self.switch.isChecked()


class ComboSettingCard(SettingCard):
    """下拉选择设定卡片"""

    index_changed = pyqtSignal(int)

    def __init__(
        self,
        icon,
        title: str,
        content: str,
        items: list,
        parent=None,
    ):
        super().__init__(icon, title, content, parent)
        self.combo = ComboBox(self)
        # 保留构造时传入的 items（已经过 tr 的成品）。后续热更新需要让
        # SubSettingInterface 通过 set_item_sources(src_list) 提供 *源串*，
        # changeEvent 时按当前语言重译。
        self._item_sources: list[str] = []
        self.combo.addItems(items)
        # 用 minimumWidth：英文/日文翻译后的"自动滚动（操作后挂起 6 秒）"等长选项
        # 在 140px 下会被裁断；让 ComboBox 按内容自然撑开。
        self.combo.setMinimumWidth(140)
        self.combo.currentIndexChanged.connect(self.index_changed.emit)
        self.hBoxLayout.addWidget(self.combo, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

    def set_item_sources(self, sources: list[str]) -> None:
        """登记每项的 *源中文串*，让 changeEvent 切语言时按当前 tr 重译。"""
        self._item_sources = list(sources)

    def changeEvent(self, event):
        from PyQt6.QtCore import QEvent as _QEvent
        if event.type() == _QEvent.Type.LanguageChange and self._item_sources:
            idx = max(0, self.combo.currentIndex())
            self.combo.blockSignals(True)
            self.combo.clear()
            self.combo.addItems([self.tr(s) for s in self._item_sources])
            # 必须先恢复 index，再显式 setCurrentText —— qfluentwidgets 的
            # ComboBox 在 clear+addItems 后**不会**自动刷新折叠态显示文本
            self.combo.setCurrentIndex(min(idx, self.combo.count() - 1))
            if self.combo.count() > 0:
                self.combo.setCurrentText(self.combo.itemText(self.combo.currentIndex()))
            self.combo.blockSignals(False)
            self.combo.update()
        super().changeEvent(event)

    def setCurrentIndex(self, idx: int):
        self.combo.setCurrentIndex(idx)

    def currentIndex(self) -> int:
        return self.combo.currentIndex()


class FontSettingCard(SettingCard):
    """字体选择设定卡片 — 点击按钮弹出带过滤器的字体选择窗口。

    系统字体可能很多，下拉不便查找；按钮显示当前字体名，点击打开
    :class:`FontPickerDialog`（Fluent 风格遮罩弹窗，支持搜索过滤与双击选用）。
    存储 / 返回字体族名称字符串。
    """

    value_changed = pyqtSignal(str)

    def __init__(
        self,
        icon,
        title: str,
        content: str,
        parent=None,
        *,
        allow_auto: bool = False,
    ):
        # SettingCard 构造期间可能收到 LanguageChange，changeEvent 会先于
        # super().__init__ 返回，因此这些状态必须预先存在。
        self._allow_auto = allow_auto
        self._family = ""
        super().__init__(icon, title, content, parent)
        self._title_text = title
        if allow_auto:
            self.btn_auto = PushButton(self.tr("自动"), self)
            self.btn_auto.setMinimumWidth(72)
            self.btn_auto.clicked.connect(self._on_auto)
            self.hBoxLayout.addWidget(self.btn_auto, 0, Qt.AlignmentFlag.AlignRight)
        self.btn = PushButton(self.tr("选择字体"), self)
        self.btn.setMinimumWidth(200)
        self.btn.clicked.connect(self._on_click)
        self.hBoxLayout.addWidget(self.btn, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

    def _on_click(self):
        from strange_uta_game.frontend.settings.font_picker_dialog import FontPickerDialog
        from qfluentwidgets import MSFluentWindow

        # FontPickerDialog 是 MessageBoxBase（遮罩式伪模态对话框），它在 parent
        # 上铺遮罩并拦截事件来实现"模态"。embedded 模式下 self.window() 是宿主
        # （工作台）主窗口——一个普通 QMainWindow，遮罩伪模态在其上会卡死。
        # 改用最近的 MSFluentWindow 祖先（即嵌入的 SUG 主窗口）作为 parent；
        # standalone 下最近的 MSFluentWindow 就是顶层窗口，行为完全不变。
        host = self
        while host is not None and not isinstance(host, MSFluentWindow):
            host = host.parentWidget()
        if host is None:
            host = self.window()

        dlg = FontPickerDialog(self._family, title=self._title_text, parent=host)
        if dlg.exec():
            family = dlg.selected_family()
            if family and family != self._family:
                self._family = family
                self.btn.setText(self._label_for(family))
                self.value_changed.emit(family)

    def _on_auto(self):
        if self._family:
            self._family = ""
            self.btn.setText(self.tr("按界面语言自动选择"))
            self.value_changed.emit("")

    @staticmethod
    def _label_for(family: str) -> str:
        try:
            from strange_uta_game.frontend.font_cache import font_display_label

            return font_display_label(family) or family
        except Exception:
            return family

    def setValue(self, family: str):
        self._family = family or ""
        if self._family:
            self.btn.setText(self._label_for(self._family))
        elif self._allow_auto:
            self.btn.setText(self.tr("按界面语言自动选择"))
        else:
            self.btn.setText(self.tr("选择字体"))

    def value(self) -> str:
        return self._family

    def changeEvent(self, event):
        from PyQt6.QtCore import QEvent as _QEvent
        if event.type() == _QEvent.Type.LanguageChange:
            if self._allow_auto and hasattr(self, "btn_auto"):
                self.btn_auto.setText(self.tr("自动"))
            if not self._family and hasattr(self, "btn"):
                self.setValue("")
        super().changeEvent(event)


class BrowseSettingCard(SettingCard):
    """目录浏览设定卡片"""

    path_changed = pyqtSignal(str)
    cleared = pyqtSignal()

    def __init__(self, icon, title: str, content: str, clearable: bool = False, parent=None):
        super().__init__(icon, title, content, parent)
        self._clearable = clearable
        self.line = LineEdit(self)
        self.line.setPlaceholderText(self.tr("未设置"))
        self.line.setReadOnly(True)
        self.line.setMinimumWidth(200)
        if clearable:
            self.btn_clear = PushButton(self.tr("清除"), self)
            self.btn_clear.setMinimumWidth(60)
            self.btn_clear.clicked.connect(self._on_clear)
        self.btn = PushButton(self.tr("浏览"), self)
        self.btn.setMinimumWidth(60)
        self.btn.clicked.connect(self._on_browse)
        self.hBoxLayout.addWidget(self.line, 0, Qt.AlignmentFlag.AlignRight)
        if clearable:
            self.hBoxLayout.addWidget(self.btn_clear, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addWidget(self.btn, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

    def _on_browse(self):
        dir_path = QFileDialog.getExistingDirectory(self, self.tr("选择目录"), "")
        if dir_path:
            self.line.setText(dir_path)
            self.path_changed.emit(dir_path)

    def _on_clear(self):
        self.line.clear()
        self.cleared.emit()
        self.path_changed.emit("")

    def changeEvent(self, event):
        from PyQt6.QtCore import QEvent as _QEvent
        if event.type() == _QEvent.Type.LanguageChange:
            self.line.setPlaceholderText(self.tr("未设置"))
            if self._clearable and hasattr(self, "btn_clear"):
                self.btn_clear.setText(self.tr("清除"))
            self.btn.setText(self.tr("浏览"))
        super().changeEvent(event)

    def setText(self, text: str):
        self.line.setText(text)

    def text(self) -> str:
        return self.line.text()


class _KeyCaptureButton(PushButton):
    """按键捕获按钮 — 点击后进入监听模式，捕获下一次按键组合。

    短按录入 → trigger_type = "short"
    长按录入（≥300ms） → trigger_type = "long"
    """

    key_captured = pyqtSignal(str)  # 捕获到的按键名称（含触发类型，如 "F5:short"）
    key_restored = pyqtSignal()     # 因冲突或其他原因恢复原值时触发

    HOLD_THRESHOLD_MS = 300  # 长按判定阈值

    def _postInit(self):
        super()._postInit()
        self._captured_key = ""
        self._trigger_type = "short"
        self._original_key = ""
        self._original_trigger = "short"
        self._listening = False
        self._pending_key = None  # 等待释放的按键名
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.setInterval(self.HOLD_THRESHOLD_MS)
        self._hold_timer.timeout.connect(self._on_hold_timeout)
        self.setMinimumWidth(140)  # 长按键名（如 "ALT+RIGHT:short"）翻译后可能更宽
        self.setFont(ui_font(9))
        self.clicked.connect(self._start_listening)

    def _start_listening(self):
        self._listening = True
        self._original_key = self._captured_key
        self._original_trigger = self._trigger_type
        self._pending_key = None
        self.setText(self.tr("按下按键..."))
        self.setStyleSheet(f"border: 2px solid {theme.accent_secondary.name()}; border-radius: 4px;")
        self.setFocus()

    def restore_original_key(self):
        """恢复修改前的按键（用于冲突处理）。"""
        self._captured_key = self._original_key
        self._trigger_type = self._original_trigger
        self._update_display()
        self.key_restored.emit()

    def keyPressEvent(self, a0: QKeyEvent | None):
        if a0 is None or not self._listening:
            super().keyPressEvent(a0)
            return
        key = a0.key()
        # 忽略单独的修饰键
        if key in (
            Qt.Key.Key_Control,
            Qt.Key.Key_Shift,
            Qt.Key.Key_Alt,
            Qt.Key.Key_Meta,
        ):
            a0.accept()
            return
        # ESC 清除快捷键
        if key == Qt.Key.Key_Escape:
            self._listening = False
            self._hold_timer.stop()
            self._pending_key = None
            self._captured_key = ""
            self._trigger_type = "short"
            self._update_display()
            self.setStyleSheet("")
            self.clearFocus()
            self.key_captured.emit("")
            a0.accept()
            return
        if a0.isAutoRepeat():
            a0.accept()
            return
        modifiers = a0.modifiers()
        key_name = _KeyCaptureButton._build_key_name(
            key, modifiers, a0.nativeVirtualKey(), a0.nativeScanCode()
        )
        if key_name:
            self._pending_key = key_name
            self._hold_timer.start(self.HOLD_THRESHOLD_MS)
        a0.accept()

    def keyReleaseEvent(self, a0: QKeyEvent | None):
        if a0 is None or not self._listening:
            super().keyReleaseEvent(a0)
            return
        key = a0.key()
        if key in (
            Qt.Key.Key_Control,
            Qt.Key.Key_Shift,
            Qt.Key.Key_Alt,
            Qt.Key.Key_Meta,
        ):
            a0.accept()
            return
        if a0.isAutoRepeat():
            a0.accept()
            return
        if self._pending_key:
            self._hold_timer.stop()
            # 短按录入
            self._captured_key = self._pending_key
            self._trigger_type = "short"
            self._listening = False
            self._pending_key = None
            self._update_display()
            self.setStyleSheet("")
            self.key_captured.emit(f"{self._captured_key}:{self._trigger_type}")
            self.clearFocus()
        a0.accept()

    def _on_hold_timeout(self):
        """长按定时器到期 — 记录为长按触发。"""
        if self._pending_key:
            self._captured_key = self._pending_key
            self._trigger_type = "long"
            self._listening = False
            self._pending_key = None
            self._update_display()
            self.setStyleSheet("")
            self.key_captured.emit(f"{self._captured_key}:{self._trigger_type}")
            self.clearFocus()

    def focusOutEvent(self, a0):
        if self._listening:
            self._listening = False
            self._hold_timer.stop()
            self._pending_key = None
            self._update_display()
            self.setStyleSheet("")
        super().focusOutEvent(a0)

    def _update_display(self):
        if self._captured_key:
            suffix = self.tr(" (长)") if self._trigger_type == "long" else ""
            # COMMA 是逗号键的内部占位名，显示时还原成字面 ","（组合键如
            # "CTRL+COMMA" 也会正确显示为 "CTRL+,"）。
            display_key = self._captured_key.replace("COMMA", ",")
            self.setText(f"{display_key}{suffix}")
        else:
            self.setText(self.tr("未设置"))

    def changeEvent(self, event):
        # 切语言时刷新当前显示文本：捕获状态下 (_listening) 维持"按下按键..."；
        # 否则按当前 captured_key/trigger 重渲染一遍。
        from PyQt6.QtCore import QEvent as _QEvent
        if event.type() == _QEvent.Type.LanguageChange:
            if self._listening:
                self.setText(self.tr("按下按键..."))
            else:
                self._update_display()
        super().changeEvent(event)

    def set_key(self, key_with_trigger: str):
        """设置按键值，支持 "F5:short" / "F5:long" / "F5" 格式。"""
        if ":" in key_with_trigger:
            key_part, trigger_part = key_with_trigger.rsplit(":", 1)
            self._captured_key = key_part.strip()
            self._trigger_type = trigger_part.strip().lower() or "short"
        else:
            self._captured_key = key_with_trigger
            self._trigger_type = "short"
        self._original_key = self._captured_key
        self._original_trigger = self._trigger_type
        self._update_display()

    def get_key(self) -> str:
        """返回原始按键名（不含触发类型）。"""
        return self._captured_key

    def get_trigger_type(self) -> str:
        """返回触发类型 ("short" / "long")。"""
        return self._trigger_type

    def get_key_with_trigger(self) -> str:
        """返回 "key:trigger" 格式字符串。"""
        if self._captured_key:
            return f"{self._captured_key}:{self._trigger_type}"
        return ""

    def clear_key(self):
        self._captured_key = ""
        self._trigger_type = "short"
        self._update_display()

    @staticmethod
    def _build_key_name(
        key,
        modifiers,
        native_virtual_key: int = 0,
        native_scan_code: int = 0,
    ) -> Optional[str]:
        """将 Qt key + modifiers 转换为规范化字符串，如 'CTRL+F4'、'SPACE'。

        macOS 上 ``QKeyEvent.key()`` 对符号按键可能返回 0，
        此时通过 ``native_virtual_key`` + Carbon kVK 映射做 fallback。
        """
        parts = []
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            parts.append("CTRL")
        if modifiers & Qt.KeyboardModifier.AltModifier:
            parts.append("ALT")
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            parts.append("SHIFT")

        _key_names = {
            Qt.Key.Key_Space: "SPACE",
            Qt.Key.Key_Escape: "ESCAPE",
            Qt.Key.Key_F1: "F1",
            Qt.Key.Key_F2: "F2",
            Qt.Key.Key_F3: "F3",
            Qt.Key.Key_F4: "F4",
            Qt.Key.Key_F5: "F5",
            Qt.Key.Key_F6: "F6",
            Qt.Key.Key_F7: "F7",
            Qt.Key.Key_F8: "F8",
            Qt.Key.Key_F9: "F9",
            Qt.Key.Key_F10: "F10",
            Qt.Key.Key_F11: "F11",
            Qt.Key.Key_F12: "F12",
            Qt.Key.Key_Up: "UP",
            Qt.Key.Key_Down: "DOWN",
            Qt.Key.Key_Left: "LEFT",
            Qt.Key.Key_Right: "RIGHT",
            Qt.Key.Key_Return: "ENTER",
            Qt.Key.Key_Enter: "ENTER",
            Qt.Key.Key_Tab: "TAB",
            Qt.Key.Key_Backspace: "BACKSPACE",
            Qt.Key.Key_Delete: "DELETE",
            Qt.Key.Key_Home: "HOME",
            Qt.Key.Key_End: "END",
            Qt.Key.Key_PageUp: "PAGEUP",
            Qt.Key.Key_PageDown: "PAGEDOWN",
            Qt.Key.Key_Insert: "INSERT",
            # 标点键（#11 修复：支持字面量键名）
            # 逗号用占位名 COMMA，避免与"主/副键"存储分隔符 "," 冲突；
            # 显示端 _update_display 会把 COMMA 还原成 ","。运行时匹配端
            # timing_interface._qt_key_to_name 必须使用相同的占位名。
            Qt.Key.Key_Comma: "COMMA",
            Qt.Key.Key_Period: ".",
            Qt.Key.Key_Slash: "/",
            Qt.Key.Key_Semicolon: ";",
            Qt.Key.Key_Apostrophe: "'",
            Qt.Key.Key_BracketLeft: "[",
            Qt.Key.Key_BracketRight: "]",
            Qt.Key.Key_Backslash: "\\",
            Qt.Key.Key_Minus: "-",
            Qt.Key.Key_Equal: "=",
            Qt.Key.Key_QuoteLeft: "`",
        }
        if key in _key_names:
            parts.append(_key_names[key])
        elif Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            parts.append(chr(key))
        elif Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            parts.append(chr(key))
        elif native_virtual_key:
            from strange_uta_game.frontend.macos_keymap import (
                macos_vk_to_key_name,
            )
            vk_name = macos_vk_to_key_name(native_virtual_key, native_scan_code)
            if vk_name:
                parts.append(vk_name)
            else:
                return None
        else:
            return None
        return "+".join(parts)


class ShortcutSettingCard(SettingCard):
    """快捷键设定卡片 — 支持键盘监听捕获和双快捷键绑定。"""

    value_changed = pyqtSignal(str)

    def __init__(
        self, icon, title: str, content: str, default_key: str = "", parent=None
    ):
        super().__init__(icon, title, content, parent)
        # 解析默认值（可能是 "Space,A" 这样的双键位格式）
        keys = (
            [k.strip() for k in default_key.split(",") if k.strip()]
            if default_key
            else []
        )
        key1 = keys[0] if len(keys) >= 1 else default_key
        key2 = keys[1] if len(keys) >= 2 else ""

        # btn_key1/btn_key2 的初始占位文本：set_key("") 后 _update_display 会
        # 调 self.tr("未设置") 覆盖。这里给 QPushButton 的初始 text 用 tr 兜底，
        # 防 set_key 路径之外的边角场景显示英文/源串。
        self.btn_key1 = _KeyCaptureButton(self.tr("点击设置"), self)
        self.btn_key1.set_key(key1)
        self.btn_key2 = _KeyCaptureButton(self.tr("点击设置"), self)
        self.btn_key2.set_key(key2)

        self._lbl_or = QLabel(self.tr("或"), self)
        self._lbl_or.setFont(ui_font(9))

        self.btn_key1.key_captured.connect(lambda k: self._on_key_changed(self.btn_key1, k))
        self.btn_key2.key_captured.connect(lambda k: self._on_key_changed(self.btn_key2, k))

        self.hBoxLayout.addWidget(self.btn_key1, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addWidget(self._lbl_or, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addWidget(self.btn_key2, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

    def changeEvent(self, event):
        from PyQt6.QtCore import QEvent as _QEvent
        if event.type() == _QEvent.Type.LanguageChange and hasattr(self, "_lbl_or"):
            self._lbl_or.setText(self.tr("或"))
        super().changeEvent(event)

    def _on_key_changed(self, btn: _KeyCaptureButton, key_name: str):
        self.value_changed.emit(self.value())

    def restore_key(self, key_name: str):
        """将指定按钮恢复为原值（针对 #2）。"""
        if self.btn_key1.get_key().strip().upper() == key_name.upper():
            self.btn_key1.restore_original_key()
        if self.btn_key2.get_key().strip().upper() == key_name.upper():
            self.btn_key2.restore_original_key()

    def setValue(self, value: str):
        """设置快捷键值，支持 'F5:short' / 'F5:long' / 'F5' / 'F5:short,A:long' 格式。"""
        keys = [k.strip() for k in value.split(",") if k.strip()] if value else []
        self.btn_key1.set_key(keys[0] if len(keys) >= 1 else "")
        self.btn_key2.set_key(keys[1] if len(keys) >= 2 else "")

    def value(self) -> str:
        """返回快捷键值，格式为 'F5:short' 或 'F5:short,A:long'。"""
        k1 = self.btn_key1.get_key_with_trigger()
        k2 = self.btn_key2.get_key_with_trigger()
        if k1 and k2:
            return f"{k1},{k2}"
        return k1 or k2

    def all_keys(self) -> list[str]:
        """返回所有已设置的快捷键列表（不含触发类型，兼容旧接口）。"""
        keys = []
        k1 = self.btn_key1.get_key().strip()
        k2 = self.btn_key2.get_key().strip()
        if k1:
            keys.append(k1.upper())
        if k2:
            keys.append(k2.upper())
        return keys

    def all_keys_with_trigger(self) -> list[tuple[str, str]]:
        """返回所有已设置的快捷键列表，每项为 (key_upper, trigger_type)。"""
        keys: list[tuple[str, str]] = []
        k1 = self.btn_key1.get_key().strip()
        t1 = self.btn_key1.get_trigger_type()
        if k1:
            keys.append((k1.upper(), t1))
        k2 = self.btn_key2.get_key().strip()
        t2 = self.btn_key2.get_trigger_type()
        if k2:
            keys.append((k2.upper(), t2))
        return keys

    def clear_key_by_name(self, key_name: str):
        """清除指定的快捷键（用于冲突解决）。"""
        if self.btn_key1.get_key().strip().upper() == key_name.upper():
            self.btn_key1.clear_key()
        if self.btn_key2.get_key().strip().upper() == key_name.upper():
            self.btn_key2.clear_key()

    def setReadOnly(self, readonly: bool):
        """设置只读模式，禁用快捷键编辑。"""
        self.btn_key1.setEnabled(not readonly)
        self.btn_key2.setEnabled(not readonly)
        if readonly:
            self.btn_key1.setToolTip(self.tr("此快捷键不可修改"))
            self.btn_key2.setToolTip(self.tr("此快捷键不可修改"))
        else:
            self.btn_key1.setToolTip("")
            self.btn_key2.setToolTip("")


class MultiCheckSettingCard(SettingCard):
    """多选设定卡片 — 点击按钮弹出对话框进行多选。"""

    selection_changed = pyqtSignal(list)

    def __init__(
        self,
        icon,
        title: str,
        content: str,
        options: list[tuple[str, str]],
        parent=None,
    ):
        """
        Args:
            icon: 图标
            title: 标题
            content: 描述
            options: 选项列表，每项为 (value, label) 元组
            parent: 父组件
        """
        super().__init__(icon, title, content, parent)
        self._title_text = title
        self._content_text = content
        self._options = options
        self._selected: list[str] = []

        self.btn = PushButton(self.tr("编辑"), self)
        self.btn.setMinimumWidth(120)
        self.btn.clicked.connect(self._on_click)
        self.hBoxLayout.addWidget(self.btn, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

        self._update_button_text()

    def _on_click(self):
        """点击按钮弹出多选对话框。"""
        dlg = QDialog(self)
        dlg.setWindowTitle(self._title_text)
        dlg.setMinimumWidth(300)
        dlg.setFont(self.font())

        layout = QVBoxLayout(dlg)
        layout.setSpacing(8)

        checkboxes: list[tuple[str, CheckBox]] = []
        for value, label in self._options:
            cb = CheckBox(label, dlg)
            cb.setChecked(value in self._selected)
            layout.addWidget(cb)
            checkboxes.append((value, cb))

        btn_row, _, _ = dialog_button_row(
            dlg, ok_text=self.tr("确定"), cancel_text=self.tr("取消")
        )
        layout.addLayout(btn_row)

        if dlg.exec():
            new_selected = [value for value, cb in checkboxes if cb.isChecked()]
            self._selected = new_selected
            self._update_button_text()
            self.selection_changed.emit(self._selected)

    def _update_button_text(self):
        """更新按钮显示文本。"""
        self.btn.setText(self.tr("编辑"))

    def changeEvent(self, event):
        from PyQt6.QtCore import QEvent as _QEvent
        if event.type() == _QEvent.Type.LanguageChange:
            self.btn.setText(self.tr("编辑"))
        super().changeEvent(event)

    def setSelectedValues(self, values: list[str]):
        """设置选中的值列表。"""
        self._selected = list(values)
        self._update_button_text()

    def selectedValues(self) -> list[str]:
        """返回选中的值列表。"""
        return list(self._selected)


class MultiBoolSettingCard(SettingCard):
    """多布尔值设定卡片 — 将多个布尔配置项整合为一个多选对话框。

    每个复选框对应一个独立的 config 键（bool 值），点击按钮弹出对话框进行多选。
    """

    selection_changed = pyqtSignal(dict)

    def __init__(
        self,
        icon,
        title: str,
        content: str,
        items: list[tuple[str, str]],
        parent=None,
    ):
        """
        Args:
            icon: 图标
            title: 标题
            content: 描述
            items: 配置项列表，每项为 (config_key_suffix, label) 元组
                   config_key_suffix 是相对于父级的键名，如 "hiragana"
            parent: 父组件
        """
        super().__init__(icon, title, content, parent)
        self._title_text = title
        self._items = items
        self._values: dict[str, bool] = {key: False for key, _ in items}

        self.btn = PushButton(self.tr("编辑"), self)
        self.btn.setMinimumWidth(120)
        self.btn.clicked.connect(self._on_click)
        self.hBoxLayout.addWidget(self.btn, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

    def _on_click(self):
        """点击按钮弹出多选对话框。"""
        dlg = QDialog(self)
        dlg.setWindowTitle(self._title_text)
        dlg.setMinimumWidth(300)
        dlg.setFont(self.font())

        layout = QVBoxLayout(dlg)
        layout.setSpacing(8)

        checkboxes: list[tuple[str, CheckBox]] = []
        for key, label in self._items:
            cb = CheckBox(label, dlg)
            cb.setChecked(self._values.get(key, False))
            layout.addWidget(cb)
            checkboxes.append((key, cb))

        btn_row, _, _ = dialog_button_row(
            dlg, ok_text=self.tr("确定"), cancel_text=self.tr("取消")
        )
        layout.addLayout(btn_row)

        if dlg.exec():
            new_values = {key: cb.isChecked() for key, cb in checkboxes}
            self._values = new_values
            self.selection_changed.emit(self._values)

    def changeEvent(self, event):
        from PyQt6.QtCore import QEvent as _QEvent
        if event.type() == _QEvent.Type.LanguageChange:
            self.btn.setText(self.tr("编辑"))
        super().changeEvent(event)

    def setValues(self, values: dict[str, bool]):
        """设置各配置项的值。"""
        self._values = dict(values)

    def values(self) -> dict[str, bool]:
        """返回各配置项的值。"""
        return dict(self._values)
