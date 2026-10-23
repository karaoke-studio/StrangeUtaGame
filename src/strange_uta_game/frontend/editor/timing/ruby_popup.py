"""Compact in-place editor used by the F2 ruby shortcut."""

from __future__ import annotations

import sys
from collections.abc import Callable, Collection

from PyQt6.QtCore import QEvent, QPoint, QRect, Qt, QTimer
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication, QDialog, QHBoxLayout
from qfluentwidgets import LineEdit, PushButton

from strange_uta_game.backend.domain import Character

from . import dialogs as ruby_dialogs


class RubyEditPopup(QDialog):
    """Edit one character's ruby and its link to the following character."""

    _INPUT_MIN_WIDTH = 92
    _INPUT_MAX_WIDTH = 420
    _LINK_BUTTON_MIN_WIDTH = 52
    _NAVIGATION_KEYS = {
        Qt.Key.Key_Left: "left",
        Qt.Key.Key_Up: "up",
        Qt.Key.Key_Right: "right",
        Qt.Key.Key_Down: "down",
    }
    _CURSOR_POSITIONS = {"select_all", "start", "end"}

    def __init__(
        self,
        character: Character,
        can_link_next: bool,
        parent=None,
        link_toggle_callback: Callable[[], None] | None = None,
        word_join_shortcut_callback: Callable[[], None] | None = None,
        word_join_shortcut_resolver: (
            Callable[[QKeyEvent], tuple[bool, bool, bool]] | None
        ) = None,
        shortcut_long_press_ms: int = 300,
        navigation_directions: Collection[str] | None = None,
        initial_cursor_position: str = "select_all",
    ):
        super().__init__(parent)
        self._character = character
        self._can_link_next = can_link_next
        self._link_toggle_callback = link_toggle_callback
        self._word_join_shortcut_callback = word_join_shortcut_callback
        self._word_join_shortcut_resolver = word_join_shortcut_resolver
        self._word_join_pending_key: int | None = None
        self._word_join_pending_short = False
        self._word_join_pending_long = False
        self._word_join_hold_timer = QTimer(self)
        self._word_join_hold_timer.setSingleShot(True)
        self._word_join_hold_timer.setInterval(max(1, shortcut_long_press_ms))
        self._word_join_hold_timer.timeout.connect(self._trigger_pending_long_word_join)
        self._navigation_directions = set(navigation_directions or ())
        self._navigation_request: str | None = None
        self._input_method_composing = False
        self._initial_cursor_position = (
            initial_cursor_position
            if initial_cursor_position in self._CURSOR_POSITIONS
            else "select_all"
        )
        self._split_mode = ruby_dialogs._get_ruby_split_mode()
        if self._split_mode not in ("direct", "char", "mora"):
            self._split_mode = "mora"
        self._original_ruby = self._ruby_input_text(character, self._split_mode)
        self._original_linked = bool(character.linked_to_next)
        self._linked = self._original_linked and can_link_next
        self._modified = False
        self._cancelled = False
        self._finished = False
        self._anchor: QRect | None = None

        self.setWindowFlags(
            # Cocoa popups cannot become key windows, which prevents IME input.
            (Qt.WindowType.Tool if sys.platform == "darwin" else Qt.WindowType.Popup)
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setObjectName("rubyEditPopup")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(5)

        self.edit_ruby = LineEdit(self)
        self.edit_ruby.setText(self._original_ruby)
        self.edit_ruby.setPlaceholderText(self.tr("注音"))
        self.edit_ruby.setToolTip(self.tr("回车保存，Esc 取消"))
        self.edit_ruby.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.edit_ruby.setInputMethodHints(Qt.InputMethodHint.ImhNone)
        self.edit_ruby.installEventFilter(self)
        self.edit_ruby.setClearButtonEnabled(True)
        self.edit_ruby.setFixedWidth(self._INPUT_MIN_WIDTH)
        self.edit_ruby.returnPressed.connect(self._apply)
        self.edit_ruby.textChanged.connect(self._resize_for_text)
        layout.addWidget(self.edit_ruby)

        self.btn_link_next = PushButton(self)
        self.btn_link_next.setCheckable(True)
        self.btn_link_next.setFixedWidth(self._LINK_BUTTON_MIN_WIDTH)
        self.btn_link_next.setEnabled(can_link_next)
        self.btn_link_next.installEventFilter(self)
        self.btn_link_next.clicked.connect(self._toggle_link)
        self._sync_link_button()
        layout.addWidget(self.btn_link_next)

        self._resize_for_text(self.edit_ruby.text())

    @staticmethod
    def _ruby_input_text(character: Character, mode: str) -> str:
        if not character.ruby or not character.ruby.parts:
            return ""
        if mode == "direct":
            return ",".join(part.text for part in character.ruby.parts)
        return character.ruby.text

    def _toggle_link(self, checked: bool) -> None:
        if self._link_toggle_callback is not None:
            self._link_toggle_callback()
            self._linked = bool(self._character.linked_to_next)
            self._sync_link_button()
            return
        self._linked = bool(checked) and self._can_link_next
        self._sync_link_button()

    def _sync_link_button(self) -> None:
        self.btn_link_next.setChecked(self._linked)
        self.btn_link_next.setText(self.tr("链接"))
        self._resize_link_button()
        if not self._can_link_next:
            self.btn_link_next.setToolTip(self.tr("行末字符不能链接下一字"))
        else:
            self.btn_link_next.setToolTip(
                self.tr("点击取消与下一个字的链接")
                if self._linked
                else self.tr("点击链接下一个字")
            )

    def _resize_link_button(self) -> None:
        """Fit translated labels such as Japanese ``リンク`` without clipping."""
        text_width = self.btn_link_next.fontMetrics().horizontalAdvance(
            self.btn_link_next.text()
        )
        self.btn_link_next.setFixedWidth(
            max(self._LINK_BUTTON_MIN_WIDTH, text_width + 28)
        )
        self.setFixedSize(self.sizeHint())
        if self._anchor is not None and self.isVisible():
            self._position_above_anchor()

    def _resize_for_text(self, text: str) -> None:
        text_width = self.edit_ruby.fontMetrics().horizontalAdvance(
            text or self.edit_ruby.placeholderText()
        )
        input_width = max(
            self._INPUT_MIN_WIDTH,
            min(self._INPUT_MAX_WIDTH, text_width + 42),
        )
        self.edit_ruby.setFixedWidth(input_width)
        self.setFixedSize(self.sizeHint())
        if self._anchor is not None and self.isVisible():
            self._position_above_anchor()

    def _apply(self) -> None:
        if self._finished or self._cancelled:
            return
        # 头尾空格是实义注音字符（与中间空格一致），不做 strip。
        ruby_text = self.edit_ruby.text()
        ruby_changed = ruby_text != self._original_ruby
        split_mode = self._split_mode
        mode_changed = split_mode != "direct" and "," in ruby_text
        if mode_changed:
            split_mode = "direct"
            ruby_dialogs._set_ruby_split_mode(split_mode)
            self._split_mode = split_mode
        link_changed = (
            self._link_toggle_callback is None and self._linked != self._original_linked
        )
        if ruby_changed:
            self._character.set_ruby(
                ruby_dialogs.parse_ruby_text(
                    ruby_text,
                    self._character.check_count,
                    mode=split_mode,
                )
            )
        if link_changed:
            self._character.linked_to_next = self._linked
        self._modified = ruby_changed or link_changed or mode_changed
        self._finished = True
        super().accept()

    def reject(self) -> None:
        """Treat popup dismissal (including an outside click) as save."""
        if self._cancelled:
            super().reject()
        else:
            self._apply()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        # Child widgets normally propagate an unhandled shortcut here. Keep
        # this fallback so remapped word-join keys also work after the link
        # button gained keyboard focus.
        if self._handle_word_join_shortcut_event(event):
            return
        if event.key() == Qt.Key.Key_Escape:
            self._cancelled = True
            self._finished = True
            super().reject()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if self._handle_word_join_shortcut_event(event):
            return
        super().keyReleaseEvent(event)

    def eventFilter(self, watched, event: QEvent) -> bool:  # noqa: N802
        """Handle configured word-join and arrow shortcuts inside the popup."""
        if watched is self.edit_ruby and event.type() == QEvent.Type.InputMethod:
            # Let the IME own arrows while composing/cycling candidates.
            self._input_method_composing = bool(event.preeditString())
        elif (
            watched in {self.edit_ruby, self.btn_link_next}
            and event.type() in {QEvent.Type.KeyPress, QEvent.Type.KeyRelease}
            and isinstance(event, QKeyEvent)
        ):
            if self._handle_word_join_shortcut_event(event):
                return True
            if (
                watched is self.edit_ruby
                and event.type() == QEvent.Type.KeyPress
                and self._handle_navigation_key(event)
            ):
                return True
        return super().eventFilter(watched, event)

    def _handle_word_join_shortcut_event(self, event: QKeyEvent) -> bool:
        """Run the configured short/long word-join trigger inside the popup."""
        if event.type() == QEvent.Type.KeyRelease:
            if self._word_join_pending_key != event.key():
                return False
            should_trigger_short = (
                self._word_join_hold_timer.isActive() and self._word_join_pending_short
            )
            self._word_join_hold_timer.stop()
            self._clear_pending_word_join()
            if should_trigger_short:
                self._trigger_word_join_shortcut()
            event.accept()
            return True

        if (
            event.type() != QEvent.Type.KeyPress
            or self._word_join_shortcut_resolver is None
        ):
            return False

        short_match, long_match, has_long_action = self._word_join_shortcut_resolver(
            event
        )
        if not short_match and not long_match:
            return False
        if event.isAutoRepeat():
            event.accept()
            return True

        if has_long_action:
            self._word_join_pending_key = event.key()
            self._word_join_pending_short = short_match
            self._word_join_pending_long = long_match
            self._word_join_hold_timer.start()
        elif short_match:
            self._trigger_word_join_shortcut()
        event.accept()
        return True

    def _trigger_pending_long_word_join(self) -> None:
        should_trigger = self._word_join_pending_long
        # Keep the key marker until release so the release is consumed and
        # cannot insert text into the ruby input after a long hold.
        self._word_join_pending_short = False
        self._word_join_pending_long = False
        if should_trigger:
            self._trigger_word_join_shortcut()

    def _clear_pending_word_join(self) -> None:
        self._word_join_pending_key = None
        self._word_join_pending_short = False
        self._word_join_pending_long = False

    def _trigger_word_join_shortcut(self) -> None:
        if self._word_join_shortcut_callback is not None:
            self._word_join_shortcut_callback()
            self._linked = bool(self._character.linked_to_next)
            self._sync_link_button()
        else:
            self._toggle_link(not self._linked)

    def _handle_navigation_key(self, event: QKeyEvent) -> bool:
        direction = self._NAVIGATION_KEYS.get(event.key())
        if (
            direction is None
            or direction not in self._navigation_directions
            or self._input_method_composing
            or event.modifiers()
            not in (
                Qt.KeyboardModifier.NoModifier,
                Qt.KeyboardModifier.KeypadModifier,
            )
        ):
            return False

        # Up/down always switch rows. Left/right retain normal text editing
        # until a collapsed caret is already at the corresponding boundary.
        if direction in {"left", "right"}:
            if self.edit_ruby.hasSelectedText():
                return False
            cursor = self.edit_ruby.cursorPosition()
            if direction == "left" and cursor != 0:
                return False
            if direction == "right" and cursor != len(self.edit_ruby.text()):
                return False

        self._navigation_request = direction
        self._apply()
        event.accept()
        return True

    def event(self, event: QEvent) -> bool:
        # Queue the save so Qt can finish dispatching the outside click first.
        if (
            event.type() == QEvent.Type.WindowDeactivate
            and self.isVisible()
            and not self._cancelled
            and not self._finished
        ):
            if sys.platform == "darwin":
                QTimer.singleShot(0, self._apply_if_inactive)
            else:
                QTimer.singleShot(0, self._apply)
        return super().event(event)

    def _apply_if_inactive(self) -> None:
        # A macOS Tool also receives its parent's deactivation while opening.
        # Wait for focus to settle and save only if the tool itself is inactive.
        # isActiveWindow() can also be true for a tool whose parent is active.
        if QApplication.activeWindow() is not self:
            self._apply()

    def was_modified(self) -> bool:
        return self._modified

    def navigation_request(self) -> str | None:
        """Return the arrow direction that accepted this popup, if any."""
        return self._navigation_request

    def show_above(self, anchor: QRect) -> int:
        """Run the popup just above a character rectangle in global coordinates."""
        self._anchor = QRect(anchor)
        self.adjustSize()
        self._position_above_anchor()
        if sys.platform == "darwin":
            # Keep Cocoa's initial fixed-size window adjustment invisible.
            self.setWindowOpacity(0.0)
        # The native popup must exist before Windows can bind an IME context.
        # Refocus on the first event-loop turn so language switching and
        # composition work for Japanese, Chinese, and other input methods.
        QTimer.singleShot(0, self._activate_ruby_input)
        try:
            return self.exec()
        finally:
            if sys.platform == "darwin":
                # Also restore opacity if dismissed before the callback runs.
                self.setWindowOpacity(1.0)

    def _activate_ruby_input(self) -> None:
        if self._finished:
            return
        self.activateWindow()
        self.edit_ruby.setFocus(Qt.FocusReason.PopupFocusReason)
        if self._initial_cursor_position == "start":
            self.edit_ruby.setCursorPosition(0)
        elif self._initial_cursor_position == "end":
            self.edit_ruby.setCursorPosition(len(self.edit_ruby.text()))
        else:
            self.edit_ruby.selectAll()
        input_method = QApplication.inputMethod()
        input_method.update(Qt.InputMethodQuery.ImEnabled | Qt.InputMethodQuery.ImHints)
        if sys.platform == "darwin":
            self._position_above_anchor()
            self.setWindowOpacity(1.0)

    def _position_above_anchor(self) -> None:
        if self._anchor is None:
            return
        anchor = self._anchor
        width, height = self.width(), self.height()
        x = anchor.center().x() - width // 2
        y = anchor.top() - height - 4

        screen = QApplication.screenAt(anchor.center())
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            x = max(available.left(), min(x, available.right() - width + 1))
            if y < available.top():
                y = min(anchor.bottom() + 4, available.bottom() - height + 1)
        self.move(QPoint(x, y))
