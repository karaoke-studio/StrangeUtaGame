"""Regression tests for choosing the F2 ruby editor style."""

from types import SimpleNamespace

from strange_uta_game.backend.domain import Character, Project, Sentence
from strange_uta_game.frontend.editor.timing_interface import EditorInterface
from strange_uta_game.frontend.settings.app_settings import AppSettings


def test_f2_ruby_editor_defaults_to_compact():
    assert AppSettings.DEFAULT_SETTINGS["ui"]["f2_ruby_editor_mode"] == "compact"


def test_f2_classic_mode_routes_to_legacy_editor():
    sentence = Sentence(
        singer_id="singer",
        characters=[Character(char="字", check_count=1)],
    )
    calls = []
    editor = SimpleNamespace(
        _project=Project(sentences=[sentence]),
        _f2_ruby_editor_mode="classic",
        _show_classic_char_editor=lambda line_idx, char_idx, target: calls.append(
            (line_idx, char_idx, target)
        ),
    )

    EditorInterface._on_char_edit_requested(editor, 0, 0)

    assert calls == [(0, 0, sentence)]


def _sentence(text: str) -> Sentence:
    return Sentence(
        singer_id="singer",
        characters=[Character(char=char, check_count=1) for char in text],
    )


def test_f2_horizontal_navigation_wraps_and_skips_empty_lines():
    project = Project(sentences=[_sentence("今宵"), _sentence(""), _sentence("綺麗")])
    editor = SimpleNamespace(_project=project)

    assert EditorInterface._ruby_navigation_target(editor, 0, 1, "right") == (
        2,
        0,
    )
    assert EditorInterface._ruby_navigation_target(editor, 2, 0, "left") == (
        0,
        1,
    )
    assert EditorInterface._ruby_navigation_target(editor, 0, 0, "left") is None
    assert EditorInterface._ruby_navigation_target(editor, 2, 1, "right") is None


def test_f2_vertical_navigation_keeps_logical_column_and_clamps():
    project = Project(
        sentences=[_sentence("今宵の頭上"), _sentence(""), _sentence("綺麗")]
    )
    editor = SimpleNamespace(_project=project)

    assert EditorInterface._ruby_navigation_target(editor, 2, 1, "up") == (
        0,
        1,
    )
    assert EditorInterface._ruby_navigation_target(editor, 0, 4, "down") == (
        2,
        1,
    )
    assert EditorInterface._ruby_navigation_target(editor, 0, 2, "up") is None
    assert EditorInterface._ruby_navigation_target(editor, 2, 0, "down") is None


def test_f2_popup_reopens_on_arrow_target_with_expected_caret(monkeypatch):
    from PyQt6.QtCore import QRect

    from strange_uta_game.frontend.editor import timing_interface as module

    project = Project(sentences=[_sentence("今宵"), _sentence(""), _sentence("綺麗")])
    requests = iter(["right", "down", None])
    opened = []
    focused = []

    class FakePopup:
        def __init__(
            self,
            character,
            *,
            navigation_directions,
            initial_cursor_position,
            **_kwargs,
        ):
            self._request = next(requests)
            opened.append(
                (
                    character.char,
                    set(navigation_directions),
                    initial_cursor_position,
                )
            )

        def show_above(self, _anchor):
            return None

        def was_modified(self):
            return False

        def navigation_request(self):
            return self._request

    preview = SimpleNamespace(
        character_global_rect=lambda _line, _char: QRect(10, 10, 20, 20),
    )
    editor = SimpleNamespace(
        _project=project,
        _f2_ruby_editor_mode="compact",
        preview=preview,
        _resolve_word_join_shortcut=lambda _event: (False, False, False),
        _long_press_timer=SimpleNamespace(interval=lambda: 300),
    )
    editor._ruby_navigation_target = lambda line, char, direction: (
        EditorInterface._ruby_navigation_target(editor, line, char, direction)
    )
    editor._focus_inline_ruby_target = lambda previous_line, line, char: focused.append(
        (previous_line, line, char)
    )

    monkeypatch.setattr(module, "RubyEditPopup", FakePopup)

    EditorInterface._on_char_edit_requested(editor, 0, 0)

    assert [entry[0] for entry in opened] == ["今", "宵", "麗"]
    assert [entry[2] for entry in opened] == ["end", "start", "end"]
    assert focused == [(0, 0, 1), (0, 2, 1)]


def test_configured_word_join_inside_f2_delegates_to_regular_action(monkeypatch):
    from PyQt6.QtCore import QEvent, QRect, Qt
    from PyQt6.QtGui import QKeyEvent

    from strange_uta_game.frontend.editor import timing_interface as module

    project = Project(sentences=[_sentence("今宵")])
    calls = []

    class FakePopup:
        def __init__(
            self,
            _character,
            *,
            word_join_shortcut_callback,
            word_join_shortcut_resolver,
            **_kwargs,
        ):
            self._word_join_shortcut_callback = word_join_shortcut_callback
            self._word_join_shortcut_resolver = word_join_shortcut_resolver

        def show_above(self, _anchor):
            event = QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_F6,
                Qt.KeyboardModifier.NoModifier,
            )
            if self._word_join_shortcut_resolver(event)[0]:
                self._word_join_shortcut_callback()

        def was_modified(self):
            return False

        def navigation_request(self):
            return None

    editor = SimpleNamespace(
        _project=project,
        _f2_ruby_editor_mode="compact",
        preview=SimpleNamespace(
            character_global_rect=lambda _line, _char: QRect(10, 10, 20, 20)
        ),
        _toggle_word_join=lambda: calls.append("word_join"),
        _key_map_short={"F6": "toggle_word_join"},
        _key_map_long={},
        _settings_loaded=True,
        _qt_key_to_name=lambda key, *_args: "F6" if key == Qt.Key.Key_F6 else "F3",
        _long_press_timer=SimpleNamespace(interval=lambda: 300),
    )
    editor._ruby_navigation_target = lambda line, char, direction: (
        EditorInterface._ruby_navigation_target(editor, line, char, direction)
    )
    editor._resolve_word_join_shortcut = lambda event: (
        EditorInterface._resolve_word_join_shortcut(editor, event)
    )

    monkeypatch.setattr(module, "RubyEditPopup", FakePopup)

    EditorInterface._on_char_edit_requested(editor, 0, 0)

    assert calls == ["word_join"]


def test_f2_editor_setting_is_below_ruby_shortcut(qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QWidget

    from strange_uta_game.frontend.settings.sub_interfaces.shortcut import (
        ShortcutSubInterface,
    )

    page = ShortcutSubInterface()
    shortcut_card = page._shortcut_cards["timing_mode"]["edit_ruby"]
    group = shortcut_card.parentWidget()

    assert page.card_f2_ruby_editor.parentWidget() is group
    cards = group.findChildren(
        QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
    )
    assert cards.index(page.card_f2_ruby_editor) == cards.index(shortcut_card) + 1
    page.close()


def test_rapid_f2_editor_switches_notify_once_after_popup_stack(qapp):
    from strange_uta_game.frontend.settings.sub_interfaces.shortcut import (
        ShortcutSubInterface,
    )

    page = ShortcutSubInterface()
    notifications = []
    page.set_change_callback(lambda: notifications.append(1))

    page.card_f2_ruby_editor.setCurrentIndex(1)
    page.card_f2_ruby_editor.setCurrentIndex(0)

    assert page._f2_editor_apply_pending is True
    assert notifications == []
    for _ in range(10):
        qapp.processEvents()
    assert page._f2_editor_apply_pending is False
    assert notifications == [1]
    page.close()
