"""行详情保存：字符级标志回填 + 演唱者名危险字符检测。

覆盖 H2 / H13：
- 保存按 6 列重建 Character，表格承载不了的 force_singer_tag / needs_guide /
  is_guide 在结构未变（字符数一致）时按原位置回填（H2）；
- 增删字符导致无法对齐时丢弃标志并提示，且仅在原字符确实带标志时提示（H2）；
- 演唱者名含逗号（列分隔符）时保存中止、提示改名，不重建字符（H13）。
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

import strange_uta_game.frontend.editor.line_interface as li_mod
from strange_uta_game.backend.domain import Character, Project, Sentence, Singer
from strange_uta_game.frontend.editor.line_interface import LineDetailDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _InfoBarRecorder:
    """记录 line_interface 模块内 InfoBar 调用（避免 offscreen 弹气泡）。"""

    calls: list = []

    @classmethod
    def warning(cls, **kw):
        cls.calls.append(("warning", kw))

    @classmethod
    def error(cls, **kw):
        cls.calls.append(("error", kw))

    @classmethod
    def success(cls, **kw):
        cls.calls.append(("success", kw))

    @classmethod
    def info(cls, **kw):
        cls.calls.append(("info", kw))


def _make_project() -> tuple[Project, Singer, Singer]:
    project = Project(
        singers=[
            Singer(name="A", color="#FF0000", is_default=True),
            Singer(name="B", color="#00FF00"),
        ]
    )
    return project, project.singers[0], project.singers[1]


def _linked_chars(a: Singer, b: Singer) -> list[Character]:
    # 前两字连词 → 表格为单行 "あいう"，便于整行编辑
    c0 = Character(
        char="あ", check_count=1, timestamps=[1000],
        linked_to_next=True, singer_id=a.id, needs_guide=True,
    )
    c1 = Character(
        char="い", check_count=1, timestamps=[2000],
        linked_to_next=True, singer_id=a.id, force_singer_tag=True,
    )
    c2 = Character(char="う", check_count=1, timestamps=[3000], singer_id=b.id, is_guide=True)
    return [c0, c1, c2]


def test_flags_restored_when_structure_unchanged(qapp, monkeypatch):
    """结构未变：保存重建后三个字符级标志按原位置保留。"""
    project, a, b = _make_project()
    sentence = Sentence(singer_id=a.id, characters=_linked_chars(a, b))
    dialog = LineDetailDialog(sentence, project=project)
    monkeypatch.setattr(li_mod, "InfoBar", _InfoBarRecorder)
    _InfoBarRecorder.calls.clear()

    dialog._on_save()

    assert [c.char for c in sentence.characters] == ["あ", "い", "う"]
    assert sentence.characters[0].needs_guide is True
    assert sentence.characters[1].force_singer_tag is True
    assert sentence.characters[2].is_guide is True
    # 正常保存不应触发 warning/error
    kinds = [kind for kind, _kw in _InfoBarRecorder.calls]
    assert "warning" not in kinds and "error" not in kinds


def test_flags_dropped_and_warned_when_structure_changed(qapp, monkeypatch):
    """增删字符无法按位对齐：标志丢弃并给出提示。"""
    project, a, b = _make_project()
    sentence = Sentence(singer_id=a.id, characters=_linked_chars(a, b))
    dialog = LineDetailDialog(sentence, project=project)
    monkeypatch.setattr(li_mod, "InfoBar", _InfoBarRecorder)
    _InfoBarRecorder.calls.clear()

    # 单行 3 字 → 4 字：字符列加一字，节奏点列加一项
    dialog.table.item(0, 0).setText("あいうえ")
    dialog.table.item(0, 2).setText("1,1,1,1")
    dialog._on_save()

    assert len(sentence.characters) == 4
    assert all(not c.needs_guide for c in sentence.characters)
    assert all(not c.force_singer_tag for c in sentence.characters)
    assert all(not c.is_guide for c in sentence.characters)
    warnings = [kw for kind, kw in _InfoBarRecorder.calls if kind == "warning"]
    assert warnings and "标志" in warnings[0]["title"]


def test_no_flag_warning_when_no_flags_present(qapp, monkeypatch):
    """原字符不带任何标志时，结构变化不应误报「标志已丢弃」。"""
    project, a, b = _make_project()
    plain = [
        Character(char="あ", check_count=1, timestamps=[1000],
                  linked_to_next=True, singer_id=a.id),
        Character(char="い", check_count=1, timestamps=[2000],
                  linked_to_next=True, singer_id=a.id),
        Character(char="う", check_count=1, timestamps=[3000], singer_id=b.id),
    ]
    sentence = Sentence(singer_id=a.id, characters=plain)
    dialog = LineDetailDialog(sentence, project=project)
    monkeypatch.setattr(li_mod, "InfoBar", _InfoBarRecorder)
    _InfoBarRecorder.calls.clear()

    dialog.table.item(0, 0).setText("あいうえ")
    dialog.table.item(0, 2).setText("1,1,1,1")
    dialog._on_save()

    assert len(sentence.characters) == 4
    warnings = [kw for kind, kw in _InfoBarRecorder.calls if kind == "warning"]
    assert warnings == []


def test_comma_singer_name_aborts_save(qapp, monkeypatch):
    """演唱者名含逗号：保存中止并提示改名，字符不被重建。"""
    project = Project(singers=[Singer(name="A,B", color="#FF0000", is_default=True)])
    s = project.singers[0]
    sentence = Sentence(
        singer_id=s.id,
        characters=[
            Character(char="あ", check_count=1, timestamps=[1000], singer_id=s.id),
        ],
    )
    dialog = LineDetailDialog(sentence, project=project)
    monkeypatch.setattr(li_mod, "InfoBar", _InfoBarRecorder)
    _InfoBarRecorder.calls.clear()

    original_chars = sentence.characters
    dialog.edit_song_part.setText("Chorus")
    dialog._on_save()

    errors = [kw for kind, kw in _InfoBarRecorder.calls if kind == "error"]
    assert errors and "逗号" in errors[0]["title"]
    # 未重建字符、未标记已修改、段落也未被部分写入
    assert sentence.characters is original_chars
    assert sentence.song_part == ""
    assert dialog.was_modified() is False


def test_song_part_edits_sentence(qapp):
    project, singer, _other = _make_project()
    sentence = Sentence.from_text("あ", singer.id)
    dialog = LineDetailDialog(sentence, project=project)
    dialog.edit_song_part.setText("Chorus")
    dialog._on_save()
    assert sentence.song_part == "Chorus"
    dialog.deleteLater()
