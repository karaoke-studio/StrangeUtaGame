"""全文本编辑「显示↔应用」集成测试（带内联时间戳格式）。

新模型：全文本编辑器显示带内联时间戳的文本，应用时逐行独立解码。
时间轴随文本走，因此行的重排/增删/文本撞车都不丢轴。本测试覆盖：
- 显示→应用 往返恒等（时间戳/ruby/停顿点/演唱者/连词全保留）
- 整行重排后各行时间轴跟随文本
- 应用后全局偏移派生到所有字符
- 新增的裸文本行 → 空轴（无 token）
"""

from __future__ import annotations



import pytest
from PyQt6.QtWidgets import QApplication

from strange_uta_game.backend.domain import (
    Character,
    Project,
    Ruby,
    RubyPart,
    Sentence,
    Singer,
)
from strange_uta_game.frontend.editor.fulltext_interface import RubyInterface


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _ruby_char(ch, moras, ts, *, linked=False, singer="", end_ts=None):
    c = Character(
        char=ch,
        check_count=len(moras),
        timestamps=list(ts),
        linked_to_next=linked,
        is_sentence_end=end_ts is not None,
        sentence_end_ts=end_ts,
        singer_id=singer,
    )
    c.set_ruby(Ruby(parts=[RubyPart(text=m) for m in moras]))
    c.push_to_ruby()
    return c


def _build_project():
    p = Project()
    p.global_offset_ms = 300
    taro = Singer(name="太郎", is_default=True)
    hanako = Singer(name="花子")
    p.singers = [taro, hanako]
    line0 = Sentence(
        singer_id=taro.id,
        characters=[
            _ruby_char("大", ["だ", "い"], [1000, 1200], linked=True, singer=taro.id),
            _ruby_char("険", ["け", "ん"], [1800, 2000], singer=taro.id),
        ],
    )
    c = Character(
        char="あ", check_count=1, timestamps=[500],
        is_sentence_end=True, sentence_end_ts=900, singer_id=hanako.id,
    )
    line1 = Sentence(singer_id=hanako.id, characters=[c])
    p.sentences = [line0, line1]
    return p, taro, hanako


def _snapshot(p):
    return [
        [
            (ch.char, list(ch.timestamps), ch.sentence_end_ts,
             [pt.text for pt in ch.ruby.parts] if ch.ruby else None,
             ch.linked_to_next, ch.singer_id)
            for ch in s.characters
        ]
        for s in p.sentences
    ]


def test_roundtrip_identity_preserves_all(qapp):
    """显示→不改→应用：时间戳/ruby/停顿点/连词/演唱者完全保留。"""
    p, taro, hanako = _build_project()
    before = _snapshot(p)
    w = RubyInterface()
    w.set_project(p)
    # 不做任何编辑，直接应用
    w._on_apply_changes()
    assert _snapshot(p) == before


def test_apply_preserves_song_part_and_aux_rows(qapp):
    """全文本应用不能把副轴行/段落标签降级成普通主行。"""
    p, taro, _hanako = _build_project()
    main = p.sentences[0]
    main.song_part = "Verse"
    aux = Sentence.from_text("译文", taro.id)
    aux.aux_role = "x-translation"
    aux.aux_parent_id = main.id
    aux.aux_lang = "zh-CN"
    p.sentences.insert(1, aux)

    w = RubyInterface()
    w.set_project(p)
    w._on_apply_changes()

    assert p.sentences[0].song_part == "Verse"
    assert p.sentences[1].aux_role == "x-translation"
    assert p.sentences[1].aux_parent_id == p.sentences[0].id
    assert p.sentences[1].aux_lang == "zh-CN"


def test_apply_does_not_move_aux_role_to_inserted_line(qapp):
    """行号变化时按旧文本匹配，不能把翻译角色套到新插入的行。"""
    p, taro, _hanako = _build_project()
    main = p.sentences[0]
    main.song_part = "Verse"
    aux = Sentence.from_text("译文", taro.id)
    aux.aux_role = "x-translation"
    aux.aux_parent_id = main.id
    p.sentences.insert(1, aux)

    w = RubyInterface()
    w.set_project(p)
    w.text_edit.setPlainText("新行\n" + w.text_edit.toPlainText())
    w._on_apply_changes()

    assert p.sentences[0].text == "新行"
    assert p.sentences[0].aux_role == ""
    translated = [s for s in p.sentences if s.text == "译文"]
    assert translated and translated[0].aux_role == "x-translation"
    assert translated[0].aux_parent_id in {s.id for s in p.sentences}


def test_apply_keeps_aux_role_when_aux_text_is_edited(qapp):
    p, taro, _hanako = _build_project()
    main = p.sentences[0]
    aux = Sentence.from_text("译文", taro.id)
    aux.aux_role = "x-translation"
    aux.aux_parent_id = main.id
    p.sentences.insert(1, aux)

    w = RubyInterface()
    w.set_project(p)
    w.text_edit.setPlainText(
        w.text_edit.toPlainText().replace("[T]文[>T]", "[T]文改[>T]")
    )
    w._on_apply_changes()

    changed = [s for s in p.sentences if s.text == "译文改"]
    assert changed and changed[0].aux_role == "x-translation"
    assert changed[0].aux_parent_id == p.sentences[0].id


def test_line_swap_follows_text(qapp):
    """整行重排后，各行时间轴跟随其文本，不串行、不丢失。"""
    p, taro, hanako = _build_project()
    w = RubyInterface()
    w.set_project(p)
    lines = w.text_edit.toPlainText().split("\n")
    assert len(lines) == 2
    w.text_edit.setPlainText("\n".join(reversed(lines)))
    w._on_apply_changes()
    # 原 line1(あ, 花子, ts500/停顿点900) 现在排第一
    assert [c.char for c in p.sentences[0].characters] == ["あ"]
    assert p.sentences[0].characters[0].timestamps == [500]
    assert p.sentences[0].characters[0].sentence_end_ts == 900
    assert p.sentences[0].characters[0].singer_id == hanako.id
    # 原 line0(大険) 现在排第二：时间戳完整跟随文本
    assert [c.char for c in p.sentences[1].characters] == ["大", "険"]
    assert p.sentences[1].characters[0].timestamps == [1000, 1200]
    assert p.sentences[1].characters[1].timestamps == [1800, 2000]
    # 演唱者按 Nicokara 约定（切换处才打标签）：太郎行原为默认无标签，
    # 被移到花子行之后 → 继承花子。若要保持太郎需在该行前加 【太郎】。
    assert p.sentences[1].characters[0].singer_id == hanako.id


def test_global_offset_applied(qapp):
    """应用后 global_timestamps = 原始 + 全局偏移(300)。"""
    p, taro, hanako = _build_project()
    w = RubyInterface()
    w.set_project(p)
    w._on_apply_changes()
    assert p.sentences[0].characters[0].global_timestamps[0] == 1300
    assert p.sentences[1].characters[0].global_timestamps[0] == 800


def test_new_bare_line_has_empty_axis(qapp):
    """新增的裸文本行（无 token）→ 空轴、无 ruby。"""
    p, taro, hanako = _build_project()
    w = RubyInterface()
    w.set_project(p)
    txt = w.text_edit.toPlainText()
    w.text_edit.setPlainText(txt + "\nなにぬ")
    w._on_apply_changes()
    assert len(p.sentences) == 3
    new_line = p.sentences[2]
    assert [c.char for c in new_line.characters] == ["な", "に", "ぬ"]
    assert all(c.timestamps == [] for c in new_line.characters)
    assert all(c.ruby is None for c in new_line.characters)
    # 原有两行时间轴不受影响
    assert p.sentences[0].characters[0].timestamps == [1000, 1200]
