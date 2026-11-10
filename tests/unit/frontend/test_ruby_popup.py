from copy import deepcopy
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QEvent, QRect, Qt, QTimer
from PyQt6.QtGui import QInputMethodEvent, QKeyEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog

from strange_uta_game.backend.domain import Character, Ruby, RubyPart
from strange_uta_game.frontend.editor.timing import ruby_popup
from strange_uta_game.frontend.editor.timing.ruby_popup import RubyEditPopup


def _popup(monkeypatch, character=None, **kwargs):
    monkeypatch.setattr(
        "strange_uta_game.frontend.editor.timing.dialogs._get_ruby_split_mode",
        lambda: "direct",
    )
    return RubyEditPopup(character or _character(), **kwargs)


def _character(*, linked: bool = False) -> Character:
    return Character(
        char="今",
        ruby=Ruby(parts=[RubyPart(text="きょ"), RubyPart(text="う")]),
        check_count=2,
        linked_to_next=linked,
    )


def test_apply_updates_ruby_and_link_state(qapp, monkeypatch):
    monkeypatch.setattr(
        "strange_uta_game.frontend.editor.timing.dialogs._get_ruby_split_mode",
        lambda: "direct",
    )
    monkeypatch.setattr(
        "strange_uta_game.backend.infrastructure.parsers.inline_format.get_ruby_pause_char",
        lambda: " ",
    )
    character = _character()
    popup = RubyEditPopup(character, can_link_next=True)

    popup.edit_ruby.setText("い,ま")
    popup._toggle_link(True)
    popup._apply()

    assert popup.result() == QDialog.DialogCode.Accepted
    assert popup.was_modified()
    assert [part.text for part in character.ruby.parts] == ["い", "ま"]
    assert character.linked_to_next is True


def test_apply_preserves_leading_trailing_spaces(qapp, monkeypatch):
    """头尾空格是实义注音字符：提交时不做剥离"""
    character = _character()
    popup = _popup(monkeypatch, character, can_link_next=True)

    popup.edit_ruby.setText(" きょ,う ")
    popup._apply()

    assert popup.was_modified()
    assert [part.text for part in character.ruby.parts] == [" きょ", "う "]


def test_unchanged_submit_preserves_existing_ruby_object(qapp, monkeypatch):
    character = _character(linked=True)
    original_ruby = character.ruby
    popup = _popup(monkeypatch, character, can_link_next=True)

    popup._apply()

    assert not popup.was_modified()
    assert character.ruby is original_ruby
    assert character.linked_to_next is True


def test_last_character_cannot_link_next(qapp, monkeypatch):
    character = _character(linked=True)
    popup = _popup(monkeypatch, character, can_link_next=False)

    popup._toggle_link(True)
    popup._apply()

    assert popup.btn_link_next.isEnabled() is False
    assert character.linked_to_next is False
    assert popup.was_modified()


@pytest.mark.parametrize(
    ("platform", "expected_type"),
    [
        ("darwin", Qt.WindowType.Tool),
        ("win32", Qt.WindowType.Popup),
        ("linux", Qt.WindowType.Popup),
    ],
)
def test_window_type_is_tool_only_on_macos(qapp, monkeypatch, platform, expected_type):
    monkeypatch.setattr(ruby_popup, "sys", SimpleNamespace(platform=platform))
    popup = _popup(monkeypatch, can_link_next=True)

    assert popup.windowType() == expected_type


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Escape])
def test_show_above_preserves_save_and_cancel(qapp, monkeypatch, platform, key):
    monkeypatch.setattr(ruby_popup, "sys", SimpleNamespace(platform=platform))
    character = _character()
    original_ruby = character.ruby
    popup = _popup(monkeypatch, character, can_link_next=True)
    opened_state = []

    def edit_and_close():
        opened_state.append(
            (
                popup.isVisible(),
                popup.windowOpacity(),
                popup.minimumSize() == popup.maximumSize(),
            )
        )
        popup.edit_ruby.selectAll()
        QTest.keyClicks(popup.edit_ruby, "ka,na")
        QTest.keyClick(popup.edit_ruby, key)

    # Interact after the opening callback, using the real Qt event loop.
    QTimer.singleShot(0, lambda: QTimer.singleShot(0, edit_and_close))
    result = popup.show_above(QRect(200, 300, 30, 40))

    assert opened_state == [(True, 1.0, True)]
    assert not popup.isVisible()
    assert popup.windowOpacity() == 1.0
    if key == Qt.Key.Key_Return:
        assert result == QDialog.DialogCode.Accepted
        assert [part.text for part in character.ruby.parts] == ["ka", "na"]
        assert popup.was_modified()
    else:
        assert result == QDialog.DialogCode.Rejected
        assert character.ruby is original_ruby
        assert not popup.was_modified()


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Escape])
def test_close_before_opening_callback_keeps_dialog_closed(qapp, monkeypatch, key):
    monkeypatch.setattr(ruby_popup, "sys", SimpleNamespace(platform="darwin"))
    character = _character()
    original_ruby = character.ruby
    popup = _popup(monkeypatch, character, can_link_next=True)
    QTimer.singleShot(0, lambda: QTest.keyClick(popup.edit_ruby, key))
    result = popup.show_above(QRect(200, 300, 30, 40))
    qapp.processEvents()  # Deliver the pending opening callback after closing.

    expected = (
        QDialog.DialogCode.Accepted
        if key == Qt.Key.Key_Return
        else QDialog.DialogCode.Rejected
    )
    assert result == popup.result() == expected
    assert not popup.isVisible()
    assert popup.windowOpacity() == 1.0
    assert character.ruby is original_ruby
    assert not popup.was_modified()


@pytest.mark.parametrize(
    ("platform", "active_after_dispatch", "should_save"),
    [
        ("darwin", True, False),
        ("darwin", False, True),
        ("win32", True, True),
        ("linux", True, True),
    ],
)
def test_deactivation_save_depends_on_platform_and_settled_focus(
    qapp, monkeypatch, platform, active_after_dispatch, should_save
):
    monkeypatch.setattr(ruby_popup, "sys", SimpleNamespace(platform=platform))
    character = _character()
    original_ruby = character.ruby
    popup = _popup(monkeypatch, character, can_link_next=True)
    popup.edit_ruby.setText("い,ま")

    # Simulate activation changes without relying on the offscreen window manager.
    other_window = object()
    active_window = other_window
    monkeypatch.setattr(QApplication, "activeWindow", lambda: active_window)
    monkeypatch.setattr(popup, "isVisible", lambda: True)
    # A Tool can report shared activation even when another window is active.
    monkeypatch.setattr(popup, "isActiveWindow", lambda: True)
    callbacks = []
    monkeypatch.setattr(
        ruby_popup,
        "QTimer",
        SimpleNamespace(singleShot=lambda delay, callback: callbacks.append(callback)),
    )

    popup.event(QEvent(QEvent.Type.WindowDeactivate))

    assert len(callbacks) == 1
    assert not popup.was_modified()
    assert character.ruby is original_ruby

    # Opening the macOS Tool may finish activating it after the event is sent.
    active_window = popup if active_after_dispatch else other_window
    callbacks[0]()

    assert popup.was_modified() is should_save
    assert popup._finished is should_save
    if should_save:
        assert popup.result() == QDialog.DialogCode.Accepted
        assert [part.text for part in character.ruby.parts] == ["い", "ま"]
    else:
        assert character.ruby is original_ruby


def test_reject_saves(qapp, monkeypatch):
    monkeypatch.setattr(
        "strange_uta_game.frontend.editor.timing.dialogs._get_ruby_split_mode",
        lambda: "direct",
    )
    monkeypatch.setattr(
        "strange_uta_game.backend.infrastructure.parsers.inline_format.get_ruby_pause_char",
        lambda: " ",
    )
    character = _character()
    popup = RubyEditPopup(character, can_link_next=True)
    popup.edit_ruby.setText("い,ま")
    popup._toggle_link(True)

    popup.reject()

    assert popup.result() == QDialog.DialogCode.Accepted
    assert [part.text for part in character.ruby.parts] == ["い", "ま"]
    assert character.linked_to_next is True


def test_escape_cancels_without_changes(qapp, monkeypatch):
    character = _character()
    original_ruby = character.ruby
    popup = _popup(monkeypatch, character, can_link_next=True)
    popup.edit_ruby.setText("いま")
    popup._toggle_link(True)

    popup.keyPressEvent(
        QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Escape,
            Qt.KeyboardModifier.NoModifier,
        )
    )

    assert popup.result() == QDialog.DialogCode.Rejected
    assert popup.was_modified() is False
    assert character.ruby is original_ruby
    assert character.linked_to_next is False


def test_link_button_uses_compact_label(qapp, monkeypatch):
    popup = _popup(monkeypatch, can_link_next=True)

    assert popup.btn_link_next.text() == "链接"
    popup._toggle_link(True)
    assert popup.btn_link_next.text() == "链接"
    assert popup.btn_link_next.isChecked() is True


def test_input_expands_only_when_text_needs_more_space(qapp, monkeypatch):
    popup = _popup(monkeypatch, can_link_next=True)
    popup.edit_ruby.setText("あ")
    compact_width = popup.width()

    popup.edit_ruby.setText("とてもながいふりがなの入力テスト")

    assert popup.width() > compact_width
    assert popup.edit_ruby.width() <= popup._INPUT_MAX_WIDTH


def test_link_button_delegates_each_toggle_and_tracks_character(qapp, monkeypatch):
    character = _character()
    calls = []

    def toggle_like_f3():
        character.linked_to_next = not character.linked_to_next
        calls.append(character.linked_to_next)

    popup = _popup(
        monkeypatch,
        character,
        can_link_next=True,
        link_toggle_callback=toggle_like_f3,
    )

    popup._toggle_link(True)
    assert calls == [True]
    assert popup.btn_link_next.isChecked() is True

    popup._toggle_link(False)
    assert calls == [True, False]
    assert popup.btn_link_next.isChecked() is False


def test_remapped_word_join_shortcut_works_without_closing_popup(qapp, monkeypatch):
    character = _character()
    calls = []

    def toggle_like_bare_f3():
        character.linked_to_next = not character.linked_to_next
        calls.append(character.linked_to_next)

    popup = _popup(
        monkeypatch,
        character,
        can_link_next=True,
        word_join_shortcut_callback=toggle_like_bare_f3,
        word_join_shortcut_resolver=lambda event: (
            event.key() == Qt.Key.Key_F6,
            False,
            False,
        ),
    )

    QApplication.sendEvent(
        popup.edit_ruby,
        QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_F3,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    assert calls == []

    for expected in (True, False):
        QApplication.sendEvent(
            popup.edit_ruby,
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_F6,
                Qt.KeyboardModifier.NoModifier,
            ),
        )
        assert popup.btn_link_next.isChecked() is expected

    popup.show()
    popup.btn_link_next.setFocus()
    QTest.keyClick(popup.btn_link_next, Qt.Key.Key_F6)

    assert calls == [True, False, True]
    assert popup.btn_link_next.isChecked() is True
    assert popup._finished is False
    assert popup.navigation_request() is None


def test_word_join_shortcut_rebinds_replaced_character_before_save(qapp, monkeypatch):
    original = _character()
    current = [original]

    def replace_like_structural_edit():
        replacement = deepcopy(current[0])
        replacement.linked_to_next = True
        current[0] = replacement

    popup = _popup(
        monkeypatch,
        original,
        can_link_next=True,
        character_provider=lambda: current[0],
        word_join_shortcut_callback=replace_like_structural_edit,
    )

    popup._trigger_word_join_shortcut()
    popup.edit_ruby.setText("い,ま")
    popup._apply()

    assert current[0] is not original
    assert current[0].linked_to_next is True
    assert [part.text for part in current[0].ruby.parts] == ["い", "ま"]
    assert [part.text for part in original.ruby.parts] == ["きょ", "う"]


def test_long_word_join_shortcut_waits_for_hold_threshold(qapp, monkeypatch):
    character = _character()
    calls = []

    def toggle_link():
        character.linked_to_next = not character.linked_to_next
        calls.append(character.linked_to_next)

    popup = _popup(
        monkeypatch,
        character,
        can_link_next=True,
        word_join_shortcut_callback=toggle_link,
        word_join_shortcut_resolver=lambda event: (
            False,
            event.key() == Qt.Key.Key_F6,
            event.key() == Qt.Key.Key_F6,
        ),
        shortcut_long_press_ms=20,
    )
    popup.show()

    QTest.keyPress(popup.edit_ruby, Qt.Key.Key_F6)
    assert calls == []
    QTest.qWait(30)
    assert calls == [True]
    QTest.keyRelease(popup.edit_ruby, Qt.Key.Key_F6)

    assert calls == [True]
    assert popup._finished is False


def test_ruby_input_accepts_unrestricted_ime_text(qapp, monkeypatch):
    popup = _popup(monkeypatch, can_link_next=True)

    assert popup.edit_ruby.testAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled)
    assert popup.edit_ruby.inputMethodHints() == Qt.InputMethodHint.ImhNone

    popup.edit_ruby.setText("かな・拼音・한글・ruby")
    assert popup.edit_ruby.text() == "かな・拼音・한글・ruby"


@pytest.mark.parametrize(
    ("key", "cursor", "direction"),
    [
        (Qt.Key.Key_Left, 0, "left"),
        (Qt.Key.Key_Up, 1, "up"),
        (Qt.Key.Key_Right, len("きょ,う"), "right"),
        (Qt.Key.Key_Down, 1, "down"),
    ],
)
def test_arrow_requests_navigation_at_input_boundary(
    qapp, monkeypatch, key, cursor, direction
):
    popup = _popup(
        monkeypatch,
        can_link_next=True,
        navigation_directions={"left", "up", "right", "down"},
    )
    popup.edit_ruby.setCursorPosition(cursor)

    QApplication.sendEvent(
        popup.edit_ruby,
        QKeyEvent(
            QEvent.Type.KeyPress,
            key,
            Qt.KeyboardModifier.NoModifier,
        ),
    )

    assert popup.navigation_request() == direction
    assert popup.result() == QDialog.DialogCode.Accepted


def test_arrow_navigation_saves_current_ruby_before_switch(qapp, monkeypatch):
    character = _character()
    popup = _popup(
        monkeypatch,
        character,
        can_link_next=True,
        navigation_directions={"right"},
    )
    popup.edit_ruby.setText("い,ま")
    popup.edit_ruby.setCursorPosition(len(popup.edit_ruby.text()))

    QApplication.sendEvent(
        popup.edit_ruby,
        QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Right,
            Qt.KeyboardModifier.NoModifier,
        ),
    )

    assert popup.navigation_request() == "right"
    assert popup.was_modified()
    assert [part.text for part in character.ruby.parts] == ["い", "ま"]


def test_empty_ruby_can_navigate_in_either_horizontal_direction(qapp, monkeypatch):
    for key, direction in (
        (Qt.Key.Key_Left, "left"),
        (Qt.Key.Key_Right, "right"),
    ):
        popup = _popup(
            monkeypatch,
            Character(char="の", check_count=1),
            can_link_next=True,
            navigation_directions={"left", "right"},
        )

        QApplication.sendEvent(
            popup.edit_ruby,
            QKeyEvent(
                QEvent.Type.KeyPress,
                key,
                Qt.KeyboardModifier.NoModifier,
            ),
        )

        assert popup.navigation_request() == direction


@pytest.mark.parametrize(
    ("key", "cursor"),
    [
        (Qt.Key.Key_Left, 2),
        (Qt.Key.Key_Right, 2),
    ],
)
def test_horizontal_arrow_keeps_editing_inside_ruby(qapp, monkeypatch, key, cursor):
    popup = _popup(
        monkeypatch,
        can_link_next=True,
        navigation_directions={"left", "right"},
    )
    popup.edit_ruby.setCursorPosition(cursor)

    QApplication.sendEvent(
        popup.edit_ruby,
        QKeyEvent(
            QEvent.Type.KeyPress,
            key,
            Qt.KeyboardModifier.NoModifier,
        ),
    )

    assert popup.navigation_request() is None
    assert popup._finished is False


def test_horizontal_arrow_does_not_navigate_with_selection(qapp, monkeypatch):
    popup = _popup(
        monkeypatch,
        can_link_next=True,
        navigation_directions={"right"},
    )
    popup.edit_ruby.selectAll()

    QApplication.sendEvent(
        popup.edit_ruby,
        QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Right,
            Qt.KeyboardModifier.NoModifier,
        ),
    )

    assert popup.navigation_request() is None
    assert popup._finished is False


def test_arrow_navigation_ignores_ime_preedit(qapp, monkeypatch):
    popup = _popup(
        monkeypatch,
        can_link_next=True,
        navigation_directions={"right"},
    )
    popup.edit_ruby.setCursorPosition(len(popup.edit_ruby.text()))
    QApplication.sendEvent(
        popup.edit_ruby,
        QInputMethodEvent("かな", []),
    )

    QApplication.sendEvent(
        popup.edit_ruby,
        QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Right,
            Qt.KeyboardModifier.NoModifier,
        ),
    )

    assert popup.navigation_request() is None
    assert popup._finished is False


@pytest.mark.parametrize(
    ("position", "expected_cursor", "selected"),
    [
        ("start", 0, False),
        ("end", len("きょ,う"), False),
        ("select_all", len("きょ,う"), True),
    ],
)
def test_initial_cursor_position(
    qapp, monkeypatch, position, expected_cursor, selected
):
    popup = _popup(
        monkeypatch,
        can_link_next=True,
        initial_cursor_position=position,
    )

    popup._activate_ruby_input()

    assert popup.edit_ruby.cursorPosition() == expected_cursor
    assert popup.edit_ruby.hasSelectedText() is selected


def test_link_button_expands_for_wider_translation(qapp, monkeypatch):
    popup = _popup(monkeypatch, can_link_next=True)
    compact_width = popup.width()

    popup.btn_link_next.setText("リンクする")
    popup._resize_link_button()

    expected = popup.btn_link_next.fontMetrics().horizontalAdvance("リンクする") + 28
    assert popup.btn_link_next.width() >= expected
    assert popup.width() > compact_width


def test_direct_mode_displays_existing_parts_with_commas(qapp, monkeypatch):
    monkeypatch.setattr(
        "strange_uta_game.frontend.editor.timing.dialogs._get_ruby_split_mode",
        lambda: "direct",
    )

    popup = RubyEditPopup(_character(), can_link_next=True)

    assert popup.edit_ruby.text() == "きょ,う"


def test_mora_mode_displays_existing_ruby_as_plain_text(qapp, monkeypatch):
    monkeypatch.setattr(
        "strange_uta_game.frontend.editor.timing.dialogs._get_ruby_split_mode",
        lambda: "mora",
    )

    popup = RubyEditPopup(_character(), can_link_next=True)

    assert popup.edit_ruby.text() == "きょう"


def test_char_mode_applies_using_current_mode(qapp, monkeypatch):
    monkeypatch.setattr(
        "strange_uta_game.frontend.editor.timing.dialogs._get_ruby_split_mode",
        lambda: "char",
    )
    monkeypatch.setattr(
        "strange_uta_game.backend.infrastructure.parsers.inline_format.get_ruby_pause_char",
        lambda: " ",
    )
    character = _character()
    popup = RubyEditPopup(character, can_link_next=True)
    popup.edit_ruby.setText("いま")

    popup._apply()

    assert [part.text for part in character.ruby.parts] == ["い", "ま"]


def test_comma_in_automatic_mode_forces_direct_and_saves_mode(qapp, monkeypatch):
    monkeypatch.setattr(
        "strange_uta_game.frontend.editor.timing.dialogs._get_ruby_split_mode",
        lambda: "mora",
    )
    saved_modes = []
    monkeypatch.setattr(
        "strange_uta_game.frontend.editor.timing.dialogs._set_ruby_split_mode",
        saved_modes.append,
    )
    monkeypatch.setattr(
        "strange_uta_game.backend.infrastructure.parsers.inline_format.get_ruby_pause_char",
        lambda: " ",
    )
    character = _character()
    popup = RubyEditPopup(character, can_link_next=True)
    popup.edit_ruby.setText("い,ま")

    popup._apply()

    assert saved_modes == ["direct"]
    assert popup._split_mode == "direct"
    assert [part.text for part in character.ruby.parts] == ["い", "ま"]
