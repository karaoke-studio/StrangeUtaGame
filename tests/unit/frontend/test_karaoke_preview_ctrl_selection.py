"""Ctrl + left mouse scattered character selection.

Semantics under test:
- plain click/drag keeps the legacy single contiguous range (single-line and cross-line);
- Ctrl+click toggles a whole character object;
- Ctrl+drag adds a whole-character range (possibly across lines);
- a set that equals a normal contiguous range collapses back to the legacy state;
- supported batch operations work on all ranges;
- single-char/line-scoped operations exit the extended selection, keep the last
  selected char, and continue with one InfoBar (copy/paste stay single-InfoBar).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QObject, QPoint, QRect, Qt
from PyQt6.QtTest import QTest

from strange_uta_game.backend.domain import Character, Project, Sentence, Singer
from strange_uta_game.frontend.editor import timing_interface as timing_module
from strange_uta_game.frontend.editor.timing.karaoke_preview import KaraokePreview
from strange_uta_game.frontend.editor.timing_interface import EditorInterface

_APP_HOLDER = []

# Japanese kana as escapes so this test file stays ASCII-only.
LINE1 = "\u3042\u3044\u3046\u3048\u304a"  # あいうえお
LINE2 = "\u304b\u304d\u304f\u3051\u3053"  # かきくけこ
LINE3 = "\u3055\u3057\u3059\u305b\u305d"  # さしすせそ


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    _APP_HOLDER.append(app)
    yield app


def _sentence(text: str, singer_id: str) -> Sentence:
    chars = [Character(char=ch, singer_id=singer_id) for ch in text]
    chars[-1].is_sentence_end = True
    return Sentence(singer_id=singer_id, characters=chars)


def _project() -> Project:
    project = Project()
    project.singers.clear()
    singer = Singer(name="Default", is_default=True)
    project.singers.append(singer)
    for text in (LINE1, LINE2, LINE3):
        project.add_sentence(_sentence(text, singer.id))
    return project


def _preview(qapp, project: Project | None = None) -> KaraokePreview:
    preview = KaraokePreview()
    preview.resize(900, 500)
    preview.set_project(project or _project())
    preview.show()
    QTest.qWait(80)
    preview.grab()
    return preview


def _rect(preview: KaraokePreview, line: int, char: int) -> QRect:
    for rect, li, ci in preview._char_hitboxes:
        if li == line and ci == char:
            return rect
    raise AssertionError(f"missing hitbox line={line} char={char}")


def _ctrl_click(preview: KaraokePreview, line: int, char: int) -> None:
    QTest.mouseClick(
        preview,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier,
        _rect(preview, line, char).center(),
    )


def _select_char0_and_2(preview: KaraokePreview) -> None:
    """Ctrl+click char 0 (explicit first selection), then char 2."""
    _ctrl_click(preview, 0, 0)
    assert preview.is_extended_selection() is False
    assert preview.get_normalized_selection() == (0, 0, 0, 0)
    _ctrl_click(preview, 0, 2)
    assert preview.is_extended_selection() is True
    assert preview.get_selected_char_ranges() == [(0, 0, 0), (0, 2, 2)]


def _ctrl_drag(preview: KaraokePreview, line: int, char: int, to_line: int, to_char: int) -> None:
    start = _rect(preview, line, char).center()
    end = _rect(preview, to_line, to_char).center()
    QTest.mousePress(preview, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, start)
    QTest.mouseMove(preview, end)
    QTest.mouseRelease(preview, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, end)


def _plain_drag(preview: KaraokePreview, line: int, char: int, to_line: int, to_char: int) -> None:
    start = _rect(preview, line, char).center()
    end = _rect(preview, to_line, to_char).center()
    QTest.mousePress(preview, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(preview, end)
    QTest.mouseRelease(preview, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)


# -- preview selection model -------------------------------------------------


def test_ctrl_click_builds_scattered_selection(qapp):
    preview = _preview(qapp)
    try:
        _ctrl_click(preview, 0, 0)  # first Ctrl+click starts an explicit selection
        assert preview.is_extended_selection() is False
        assert preview.get_selected_char_ranges() == [(0, 0, 0)]

        _ctrl_click(preview, 0, 2)
        assert preview.is_extended_selection() is True
        assert preview.get_selected_char_ranges() == [(0, 0, 0), (0, 2, 2)]

        _ctrl_click(preview, 0, 4)
        assert preview.is_extended_selection() is True
        assert preview.get_selected_char_ranges() == [
            (0, 0, 0),
            (0, 2, 2),
            (0, 4, 4),
        ]
        assert preview.get_selected_char_count() == 3
        assert preview.get_selected_text() == "\u3042\u3046\u304a"  # あうお
        assert preview.get_normalized_selection() is None
    finally:
        preview.close()


def test_first_ctrl_drag_does_not_include_implicit_focus(qapp):
    preview = _preview(qapp)
    try:
        _ctrl_drag(preview, 2, 1, 2, 2)
        assert preview.is_extended_selection() is False
        assert preview.get_normalized_selection() == (2, 1, 2, 2)
        assert preview.get_selected_char_ranges() == [(2, 1, 2)]
    finally:
        preview.close()


def test_ctrl_click_toggles_selected_char_off(qapp):
    preview = _preview(qapp)
    try:
        _select_char0_and_2(preview)

        _ctrl_click(preview, 0, 2)
        assert preview.is_extended_selection() is False
        assert preview.get_selected_char_ranges() == [(0, 0, 0)]

        _ctrl_click(preview, 0, 0)  # last explicit char can be cleared
        assert preview.is_extended_selection() is False
        assert preview.get_selected_char_ranges() == []
    finally:
        preview.close()


def test_ctrl_drag_same_char_still_toggles(qapp):
    preview = _preview(qapp)
    try:
        _select_char0_and_2(preview)
        rect = _rect(preview, 0, 2)
        center = rect.center()
        assert preview._begin_ctrl_selection(center.x(), center.y())
        preview._ctrl_sel_moved = True
        preview._ctrl_sel_extent = (0, 2)
        preview._finish_ctrl_selection(center.x() + 7, center.y())
        assert preview.is_extended_selection() is False
        assert preview.get_selected_char_ranges() == [(0, 0, 0)]
    finally:
        preview.close()


def test_ctrl_drag_adds_range_across_lines_to_existing_selection(qapp):
    preview = _preview(qapp)
    try:
        _select_char0_and_2(preview)
        _ctrl_drag(preview, 1, 1, 2, 0)
        assert preview.is_extended_selection() is True
        assert preview.get_selected_char_ranges() == [
            (0, 0, 0),
            (0, 2, 2),
            (1, 1, 4),
            (2, 0, 0),
        ]
        assert preview.get_selected_char_count() == 2 + 4 + 1
    finally:
        preview.close()


def test_contiguous_ctrl_range_collapses_to_normal_selection(qapp):
    preview = _preview(qapp)
    try:
        # Build an explicit scattered set first, then bridge the gap with Ctrl+drag.
        _ctrl_click(preview, 0, 0)
        _ctrl_click(preview, 0, 3)
        assert preview.is_extended_selection() is True
        assert preview.get_selected_char_ranges() == [(0, 0, 0), (0, 3, 3)]

        _ctrl_drag(preview, 0, 1, 0, 2)
        assert preview.is_extended_selection() is False
        assert preview.get_normalized_selection() == (0, 0, 0, 3)

        # Cross-line contiguous range also collapses.
        preview.clear_extended_selection()
        preview.set_focus_position(0, 0)
        _ctrl_drag(preview, 0, 0, 1, 4)
        assert preview.is_extended_selection() is False
        assert preview.get_normalized_selection() == (0, 0, 1, 4)
        assert preview.is_multi_line_selection() is True
    finally:
        preview.close()


def test_plain_click_clears_ctrl_selection(qapp):
    preview = _preview(qapp)
    try:
        _select_char0_and_2(preview)
        QTest.mouseClick(
            preview,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            _rect(preview, 0, 4).center(),
        )
        assert preview.is_extended_selection() is False
        assert preview._focus_line_idx == 0
        assert preview._focus_char_idx == 4
        assert preview._focus_line_range_end == 0
        assert preview._focus_char_range_end == 4
        assert preview.get_normalized_selection() == (0, 4, 0, 4)
    finally:
        preview.close()


def test_plain_single_line_drag_unchanged(qapp):
    preview = _preview(qapp)
    try:
        _plain_drag(preview, 0, 1, 0, 3)
        assert preview.is_extended_selection() is False
        assert preview.get_normalized_selection() == (0, 1, 0, 3)
        assert preview.is_multi_line_selection() is False
    finally:
        preview.close()


def test_plain_cross_line_drag_unchanged(qapp):
    preview = _preview(qapp)
    try:
        _plain_drag(preview, 0, 2, 1, 1)
        assert preview.is_extended_selection() is False
        assert preview.get_normalized_selection() == (0, 2, 1, 1)
        assert preview.is_multi_line_selection() is True
        assert preview.get_selected_char_ranges() == [(0, 2, 4), (1, 0, 1)]
    finally:
        preview.close()


def test_focus_move_clears_extended_selection(qapp):
    preview = _preview(qapp)
    try:
        _select_char0_and_2(preview)
        preview.set_focus_position(1, 0)
        assert preview.is_extended_selection() is False
    finally:
        preview.close()


def test_reversed_ctrl_drag_and_single_char_drag(qapp):
    preview = _preview(qapp)
    try:
        _ctrl_click(preview, 0, 0)
        _ctrl_drag(preview, 0, 4, 0, 2)
        assert preview.is_extended_selection() is True
        assert preview.get_selected_char_ranges() == [(0, 0, 0), (0, 2, 4)]

        preview.clear_extended_selection()
        preview.set_focus_position(0, 0)
        _ctrl_click(preview, 0, 0)
        _ctrl_drag(preview, 0, 2, 0, 2)
        assert preview.is_extended_selection() is True
        assert preview.get_selected_char_ranges() == [(0, 0, 0), (0, 2, 2)]
    finally:
        preview.close()


def test_empty_line_between_cross_line_ctrl_drag_stays_simple(qapp):
    project = _project()
    project.sentences.insert(1, Sentence(singer_id=project.singers[0].id, characters=[]))
    preview = _preview(qapp, project)
    try:
        _ctrl_drag(preview, 0, 0, 2, 4)
        assert preview.is_extended_selection() is False
        assert preview.get_normalized_selection() == (0, 0, 2, 4)
        assert preview.get_selected_char_ranges() == [(0, 0, 4), (2, 0, 4)]
    finally:
        preview.close()


def test_plain_drag_replaces_extended_selection(qapp):
    preview = _preview(qapp)
    try:
        _select_char0_and_2(preview)
        _plain_drag(preview, 0, 1, 0, 3)
        assert preview.is_extended_selection() is False
        assert preview.get_normalized_selection() == (0, 1, 0, 3)
    finally:
        preview.close()


def test_blank_ctrl_click_does_not_change_selection(qapp):
    preview = _preview(qapp)
    try:
        _select_char0_and_2(preview)
        expected = preview.get_selected_char_ranges()
        # Far above every rendered line: must be treated as blank, not snapped.
        QTest.mouseClick(
            preview,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ControlModifier,
            QPoint(450, 5),
        )
        assert preview.get_selected_char_ranges() == expected
        assert preview.is_extended_selection() is True
    finally:
        preview.close()


def test_ctrl_click_on_empty_line_does_not_change_selection(qapp):
    project = _project()
    project.sentences.insert(1, Sentence(singer_id=project.singers[0].id, characters=[]))
    preview = _preview(qapp, project)
    try:
        _select_char0_and_2(preview)
        expected = preview.get_selected_char_ranges()
        line_height = preview.height() / preview._visible_lines
        y = int(preview.height() / 2 + line_height)
        QTest.mouseClick(
            preview,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ControlModifier,
            QPoint(450, y),
        )
        assert preview.get_selected_char_ranges() == expected
    finally:
        preview.close()


def test_extended_selection_is_painted_with_hidden_highlights(qapp):
    from strange_uta_game.frontend.theme import theme

    preview = _preview(qapp)
    try:
        preview.set_hide_hitbox_highlights(True)
        _select_char0_and_2(preview)
        preview.grab()  # refresh hitboxes
        image = preview.grab().toImage()
        selected = _rect(preview, 0, 2)
        unselected = _rect(preview, 0, 1)
        assert image.pixelColor(
            selected.left() + 2, selected.top() + 2
        ) == theme.karaoke_selection_bg
        assert image.pixelColor(
            unselected.left() + 2, unselected.top() + 2
        ) != theme.karaoke_selection_bg
    finally:
        preview.close()



class _SpyMenu(QObject):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.actions = []

    def addAction(self, action):  # noqa: N802 - RoundMenu API
        self.actions.append(action)

    def addMenu(self, menu):  # noqa: N802 - RoundMenu API
        self.actions.append(menu)

    def addSeparator(self):  # noqa: N802 - RoundMenu API
        pass

    def exec(self, *args, **kwargs):
        pass


class _SpyMenuFactory:
    def __init__(self):
        self.menus = []

    def __call__(self, *args, **kwargs):
        menu = _SpyMenu(*args, **kwargs)
        self.menus.append(menu)
        return menu


def test_right_click_cancels_active_ctrl_gesture(qapp, monkeypatch):
    from strange_uta_game.frontend.editor.timing import (
        karaoke_preview as preview_module,
    )

    preview = _preview(qapp)
    monkeypatch.setattr(preview_module, "RoundMenu", _SpyMenuFactory())
    try:
        rect = _rect(preview, 0, 1)
        QTest.mousePress(
            preview,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ControlModifier,
            rect.center(),
        )
        assert preview._ctrl_sel_active is True
        QTest.mousePress(
            preview,
            Qt.MouseButton.RightButton,
            Qt.KeyboardModifier.NoModifier,
            rect.center(),
        )
        assert preview._ctrl_sel_active is False
        QTest.mouseRelease(
            preview,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            rect.center(),
        )
    finally:
        preview.close()


# -- EditorInterface integration --------------------------------------------


def _fake_editor(project: Project, preview: KaraokePreview):
    return SimpleNamespace(
        _project=project,
        preview=preview,
        _current_line_idx=0,
        _current_char_idx=0,
        tr=lambda text: text,
        _preview_has_extended_selection=lambda: preview.is_extended_selection(),
        _ruby_analysis_in_progress=lambda: False,
        _reject_during_ruby_analysis=lambda: False,
    )


def _patch_structural_edit(editor):
    editor._execute_structural_edit = (
        lambda desc, mutate, **kwargs: (mutate() is not None)
    )


def test_collect_selected_char_ranges_returns_extended_segments(qapp):
    project = _project()
    preview = _preview(qapp, project)
    try:
        _select_char0_and_2(preview)
        editor = _fake_editor(project, preview)
        assert EditorInterface._collect_selected_char_ranges(editor) == [
            (0, 0, 0),
            (0, 2, 2),
        ]
    finally:
        preview.close()


def test_scope_label_mentions_extended_selection(qapp):
    project = _project()
    preview = _preview(qapp, project)
    try:
        _select_char0_and_2(preview)
        editor = _fake_editor(project, preview)
        label = EditorInterface._format_selected_scope_label(
            editor, preview.get_selected_char_ranges()
        )
        assert "\u8de8\u5b57\u7b26\u9009\u533a" in label  # 跨字符选区
        assert "\u5171 2 \u5b57" in label  # 共 2 字
    finally:
        preview.close()


def test_collapse_extended_selection_keeps_last_char_and_one_infobar(qapp, monkeypatch):
    project = _project()
    preview = _preview(qapp, project)
    infos = []
    monkeypatch.setattr(
        timing_module.InfoBar, "info", lambda **kwargs: infos.append(kwargs)
    )
    try:
        _select_char0_and_2(preview)  # last selected = (0,2)
        editor = _fake_editor(project, preview)
        moved = []
        editor._on_char_selected = lambda line, char: moved.append((line, char))
        editor._update_line_info = lambda: None

        target = EditorInterface._collapse_extended_selection_for_action(
            editor, "\u6d4b\u8bd5", notify=True
        )
        assert target == (0, 2)
        assert preview.is_extended_selection() is False
        assert preview.get_normalized_selection() == (0, 2, 0, 2)
        assert moved == [(0, 2)]
        assert len(infos) == 1
    finally:
        preview.close()


def test_collapse_still_runs_for_non_structural_action_while_busy(qapp, monkeypatch):
    project = _project()
    preview = _preview(qapp, project)
    infos = []
    monkeypatch.setattr(
        timing_module.InfoBar, "info", lambda **kwargs: infos.append(kwargs)
    )
    try:
        _select_char0_and_2(preview)
        editor = _fake_editor(project, preview)
        editor._ruby_analysis_in_progress = lambda: True
        target = EditorInterface._collapse_extended_selection_for_action(
            editor, "\u6d4b\u8bd5", notify=True
        )
        assert target == (0, 2)
        assert preview.is_extended_selection() is False
        assert preview.get_normalized_selection() == (0, 2, 0, 2)
        assert len(infos) == 1
    finally:
        preview.close()


def test_busy_structural_handler_rejects_and_keeps_selection(qapp):
    project = _project()
    preview = _preview(qapp, project)
    warnings = []
    try:
        _select_char0_and_2(preview)
        before = [s.text for s in project.sentences]
        editor = _fake_editor(project, preview)
        editor._ruby_analysis_in_progress = lambda: True
        editor._reject_during_ruby_analysis = (
            lambda: warnings.append("busy") or True
        )
        EditorInterface._insert_space_at_current(editor)
        assert preview.is_extended_selection() is True
        assert [s.text for s in project.sentences] == before
        assert warnings == ["busy"]
    finally:
        preview.close()


def test_delete_selection_ranges_deletes_all_and_one_infobar(qapp, monkeypatch):
    project = _project()
    preview = _preview(qapp, project)
    successes = []
    monkeypatch.setattr(
        timing_module.InfoBar, "success", lambda **kwargs: successes.append(kwargs)
    )
    try:
        _select_char0_and_2(preview)
        _ctrl_drag(preview, 2, 1, 2, 1)
        ranges = preview.get_selected_char_ranges()
        editor = _fake_editor(project, preview)
        _patch_structural_edit(editor)
        EditorInterface._on_delete_selection_ranges(editor, ranges)
        assert [c.char for c in project.sentences[0].characters] == list(
            "\u3044\u3048\u304a"
        )
        assert [c.char for c in project.sentences[2].characters] == list(
            "\u3055\u3059\u305b\u305d"
        )
        assert len(successes) == 1
    finally:
        preview.close()


def test_delete_selection_ranges_removes_emptied_lines(qapp, monkeypatch):
    project = Project()
    project.singers.clear()
    singer = Singer(name="Default", is_default=True)
    project.singers.append(singer)
    for text in ("\u3042", "\u304b", "\u3055"):
        project.add_sentence(_sentence(text, singer.id))
    preview = _preview(qapp, project)
    successes = []
    monkeypatch.setattr(
        timing_module.InfoBar, "success", lambda **kwargs: successes.append(kwargs)
    )
    try:
        _ctrl_click(preview, 0, 0)
        _ctrl_click(preview, 2, 0)
        ranges = preview.get_selected_char_ranges()
        assert ranges == [(0, 0, 0), (2, 0, 0)]
        editor = _fake_editor(project, preview)
        _patch_structural_edit(editor)
        EditorInterface._on_delete_selection_ranges(editor, ranges)
        assert len(project.sentences) == 1
        assert project.sentences[0].text == "\u304b"
        assert len(successes) == 1
    finally:
        preview.close()


def test_word_join_ranges_links_and_unlinks_each_segment(qapp, monkeypatch):
    project = _project()
    preview = _preview(qapp, project)
    monkeypatch.setattr(timing_module.InfoBar, "success", lambda **kwargs: None)
    try:
        _ctrl_drag(preview, 0, 0, 0, 1)
        _ctrl_drag(preview, 0, 3, 0, 4)
        ranges = preview.get_selected_char_ranges()
        assert ranges == [(0, 0, 1), (0, 3, 4)]
        editor = _fake_editor(project, preview)
        _patch_structural_edit(editor)
        chars = project.sentences[0].characters

        EditorInterface._toggle_word_join_ranges(editor, ranges)
        assert [c.linked_to_next for c in chars] == [
            True,
            False,
            False,
            True,
            False,
        ]

        EditorInterface._toggle_word_join_ranges(editor, ranges)
        assert [c.linked_to_next for c in chars] == [
            False,
            False,
            False,
            False,
            False,
        ]
    finally:
        preview.close()


def test_word_join_single_char_never_links_outside_selection(qapp, monkeypatch):
    project = _project()
    preview = _preview(qapp, project)
    successes = []
    monkeypatch.setattr(
        timing_module.InfoBar, "success", lambda **kwargs: successes.append(kwargs)
    )
    try:
        chars = project.sentences[0].characters
        chars[0].linked_to_next = False
        chars[2].linked_to_next = True
        editor = _fake_editor(project, preview)
        _patch_structural_edit(editor)
        EditorInterface._toggle_word_join_ranges(
            editor, [(0, 0, 0), (0, 2, 2)]
        )
        assert chars[0].linked_to_next is False  # never link to unselected char 1
        assert chars[2].linked_to_next is False  # only cancels existing link
        assert len(successes) == 1
    finally:
        preview.close()


def test_apply_singer_ranges_batch_updates_and_one_infobar(qapp, monkeypatch):
    project = _project()
    preview = _preview(qapp, project)
    other = Singer(name="Other", color="#123456")
    project.singers.append(other)
    successes = []
    monkeypatch.setattr(
        timing_module.InfoBar, "success", lambda **kwargs: successes.append(kwargs)
    )
    try:
        _select_char0_and_2(preview)
        editor = _fake_editor(project, preview)
        _patch_structural_edit(editor)
        EditorInterface._on_apply_singer_ranges(
            editor, preview.get_selected_char_ranges(), other.id
        )
        assert project.sentences[0].characters[0].singer_id == other.id
        assert project.sentences[0].characters[2].singer_id == other.id
        assert project.sentences[0].characters[1].singer_id != other.id
        assert len(successes) == 1
    finally:
        preview.close()


def test_apply_singer_ranges_across_lines(qapp, monkeypatch):
    project = _project()
    preview = _preview(qapp, project)
    other = Singer(name="Other", color="#123456")
    project.singers.append(other)
    monkeypatch.setattr(timing_module.InfoBar, "success", lambda **kwargs: None)
    try:
        _select_char0_and_2(preview)
        _ctrl_drag(preview, 2, 1, 2, 1)
        ranges = preview.get_selected_char_ranges()
        editor = _fake_editor(project, preview)
        _patch_structural_edit(editor)
        EditorInterface._on_apply_singer_ranges(editor, ranges, other.id)
        assert project.sentences[0].characters[0].singer_id == other.id
        assert project.sentences[0].characters[2].singer_id == other.id
        assert project.sentences[2].characters[1].singer_id == other.id
        assert project.sentences[1].characters[1].singer_id != other.id
    finally:
        preview.close()


def test_clear_timestamps_ranges_batch_and_one_infobar(qapp, monkeypatch):
    project = _project()
    for ch in project.sentences[0].characters:
        ch.timestamps = [1000]
    preview = _preview(qapp, project)
    successes = []
    monkeypatch.setattr(
        timing_module.InfoBar, "success", lambda **kwargs: successes.append(kwargs)
    )
    try:
        _select_char0_and_2(preview)
        editor = _fake_editor(project, preview)
        _patch_structural_edit(editor)
        EditorInterface._on_selection_char_ranges_action(
            editor, "clear_timestamps", preview.get_selected_char_ranges()
        )
        assert project.sentences[0].characters[0].timestamps == []
        assert project.sentences[0].characters[2].timestamps == []
        assert project.sentences[0].characters[1].timestamps == [1000]
        assert len(successes) == 1
    finally:
        preview.close()


def test_checkpoint_and_pause_batch_actions_only_touch_selection(qapp):
    project = _project()
    preview = _preview(qapp, project)
    try:
        _select_char0_and_2(preview)
        editor = _fake_editor(project, preview)
        calls = []
        editor._execute_structural_edit = (
            lambda desc, mutate, **kwargs: (calls.append(kwargs), mutate() is not None)[1]
        )
        ranges = preview.get_selected_char_ranges()
        chars = project.sentences[0].characters

        before = [ch.check_count for ch in chars]
        EditorInterface._on_selection_char_ranges_action(editor, "add_checkpoint", ranges)
        assert chars[0].check_count == before[0] + 1
        assert chars[2].check_count == before[2] + 1
        assert chars[1].check_count == before[1]

        EditorInterface._on_selection_char_ranges_action(
            editor, "toggle_sentence_end", ranges
        )
        assert chars[0].is_sentence_end is True
        assert chars[2].is_sentence_end is True
        assert chars[1].is_sentence_end is False

        EditorInterface._on_selection_char_ranges_action(
            editor, "toggle_needs_guide", ranges
        )
        assert chars[0].needs_guide is True
        assert chars[2].needs_guide is True
        assert chars[1].needs_guide is False
        assert calls[-1].get("move_cp") is False
    finally:
        preview.close()


def test_delete_timestamps_selected_ruby_busy_emits_one_infobar(qapp, monkeypatch):
    project = _project()
    for ch in project.sentences[0].characters:
        ch.timestamps = [1000]
    preview = _preview(qapp, project)
    warnings = []
    infos = []
    monkeypatch.setattr(
        timing_module.InfoBar, "warning", lambda **kwargs: warnings.append(kwargs)
    )
    monkeypatch.setattr(
        timing_module.InfoBar, "info", lambda **kwargs: infos.append(kwargs)
    )
    try:
        _select_char0_and_2(preview)
        editor = _fake_editor(project, preview)
        editor._collect_selected_char_ranges = (
            lambda: EditorInterface._collect_selected_char_ranges(editor)
        )
        editor._ruby_analysis_in_progress = lambda: True
        editor._reject_during_ruby_analysis = lambda: warnings.append("busy") or True
        EditorInterface._on_delete_timestamps_selected(editor)
        assert len(warnings) == 1
        assert infos == []
        assert project.sentences[0].characters[0].timestamps == [1000]
    finally:
        preview.close()


def _bind_collapse_helpers(editor):
    editor._collapse_extended_selection_for_action = (
        lambda label, notify=True: EditorInterface._collapse_extended_selection_for_action(
            editor, label, notify=notify
        )
    )
    editor._on_char_selected = lambda line, char: None
    editor._update_line_info = lambda: None


def test_delete_line_after_extended_selection_collapses_and_runs(qapp, monkeypatch):
    project = _project()
    preview = _preview(qapp, project)
    infos = []
    monkeypatch.setattr(
        timing_module.InfoBar, "info", lambda **kwargs: infos.append(kwargs)
    )
    try:
        _select_char0_and_2(preview)
        editor = _fake_editor(project, preview)
        _bind_collapse_helpers(editor)
        _patch_structural_edit(editor)
        EditorInterface._on_delete_line_requested(editor, 1)
        assert preview.is_extended_selection() is False
        assert preview.get_normalized_selection() == (0, 2, 0, 2)
        assert len(project.sentences) == 2
        assert [s.text for s in project.sentences] == [LINE2, LINE3]
        assert len(infos) == 1
    finally:
        preview.close()


def test_insert_space_after_extended_selection_collapses_and_runs(qapp, monkeypatch):
    project = _project()
    preview = _preview(qapp, project)
    monkeypatch.setattr(timing_module.InfoBar, "info", lambda **kwargs: None)
    try:
        _select_char0_and_2(preview)
        editor = _fake_editor(project, preview)
        _bind_collapse_helpers(editor)
        _patch_structural_edit(editor)
        before = len(project.sentences[0].characters)
        EditorInterface._on_insert_space_before_requested(editor, 0, 0)
        assert preview.is_extended_selection() is False
        assert preview.get_normalized_selection() == (0, 2, 0, 2)
        assert len(project.sentences[0].characters) == before + 1
        assert project.sentences[0].characters[2].char == " "
    finally:
        preview.close()




# -- context menu ------------------------------------------------------------


def test_context_menu_singer_applies_to_extended_ranges(qapp, monkeypatch):
    from strange_uta_game.frontend.editor.timing import (
        karaoke_preview as preview_module,
    )

    project = _project()
    other = Singer(name="Other", color="#123456")
    project.singers.append(other)
    preview = _preview(qapp, project)
    factory = _SpyMenuFactory()
    monkeypatch.setattr(preview_module, "RoundMenu", factory)
    emitted = []
    preview.singer_change_ranges_requested.connect(
        lambda ranges, sid: emitted.append((ranges, sid))
    )
    try:
        _select_char0_and_2(preview)
        rect = _rect(preview, 0, 2)
        preview._show_context_menu(QPoint(10, 10), rect.center().x(), rect.center().y())

        main_menu = factory.menus[0]
        singer_menu = next(m for m in main_menu.actions if isinstance(m, _SpyMenu))
        next(a for a in singer_menu.actions if a.text() == "Other").trigger()

        assert emitted
        ranges, sid = emitted[0]
        assert ranges == [(0, 0, 0), (0, 2, 2)]
        assert sid == other.id
    finally:
        preview.close()


def test_context_menu_delete_char_emits_extended_ranges(qapp, monkeypatch):
    from strange_uta_game.frontend.editor.timing import (
        karaoke_preview as preview_module,
    )

    project = _project()
    preview = _preview(qapp, project)
    factory = _SpyMenuFactory()
    monkeypatch.setattr(preview_module, "RoundMenu", factory)
    emitted = []
    preview.delete_selection_ranges_requested.connect(emitted.append)
    try:
        _select_char0_and_2(preview)
        rect = _rect(preview, 0, 2)
        preview._show_context_menu(QPoint(10, 10), rect.center().x(), rect.center().y())
        factory.menus[0].actions[0].trigger()
        assert emitted == [[(0, 0, 0), (0, 2, 2)]]
    finally:
        preview.close()


def test_context_menu_line_delete_uses_last_selected_not_target(qapp, monkeypatch):
    from strange_uta_game.frontend.editor.timing import (
        karaoke_preview as preview_module,
    )

    project = _project()
    preview = _preview(qapp, project)
    factory = _SpyMenuFactory()
    monkeypatch.setattr(preview_module, "RoundMenu", factory)
    emitted = []
    preview.delete_line_requested.connect(emitted.append)
    try:
        _select_char0_and_2(preview)
        _ctrl_drag(preview, 2, 1, 2, 1)  # last selected char becomes (2, 1)
        rect = _rect(preview, 0, 0)  # right-click a different selected char
        preview._show_context_menu(QPoint(10, 10), rect.center().x(), rect.center().y())
        action = next(
            a for a in factory.menus[0].actions if "\u5220\u9664\u672c\u884c" in a.text()
        )
        action.trigger()
        assert preview.is_extended_selection() is False
        assert preview.get_normalized_selection() == (2, 1, 2, 1)
        assert emitted == [2]
    finally:
        preview.close()


def test_context_menu_c_action_guard_keeps_selection(qapp, monkeypatch):
    from strange_uta_game.frontend.editor.timing import (
        karaoke_preview as preview_module,
    )

    project = _project()
    preview = _preview(qapp, project)
    factory = _SpyMenuFactory()
    monkeypatch.setattr(preview_module, "RoundMenu", factory)
    emitted = []
    preview.delete_line_requested.connect(emitted.append)
    try:
        _select_char0_and_2(preview)
        preview._c_class_action_guard = lambda: False
        rect = _rect(preview, 0, 0)
        preview._show_context_menu(QPoint(10, 10), rect.center().x(), rect.center().y())
        action = next(
            a for a in factory.menus[0].actions if "\u5220\u9664\u672c\u884c" in a.text()
        )
        action.trigger()
        assert emitted == []
        assert preview.is_extended_selection() is True
    finally:
        preview.close()


def test_context_menu_outside_extended_selection_collapses_it(qapp, monkeypatch):
    from strange_uta_game.frontend.editor.timing import (
        karaoke_preview as preview_module,
    )

    project = _project()
    preview = _preview(qapp, project)
    factory = _SpyMenuFactory()
    monkeypatch.setattr(preview_module, "RoundMenu", factory)
    try:
        _select_char0_and_2(preview)
        rect = _rect(preview, 0, 4)
        preview._show_context_menu(QPoint(10, 10), rect.center().x(), rect.center().y())
        assert preview.is_extended_selection() is False
        assert preview._focus_line_idx == 0
        assert preview._focus_char_idx == 4
        assert preview._focus_line_range_end == 0
        assert preview._focus_char_range_end == 4
        assert preview.get_normalized_selection() == (0, 4, 0, 4)
    finally:
        preview.close()
