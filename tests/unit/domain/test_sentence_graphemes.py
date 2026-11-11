"""Sentence 字素簇方法测试：from_text 簇切分 / normalize 自愈 / explode-merge 往返。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from strange_uta_game.backend.domain.entities import Sentence  # noqa: E402
from strange_uta_game.backend.domain.models import Character, Ruby, RubyPart  # noqa: E402


class TestFromText:
    def test_clusters_by_default(self):
        s = Sentence.from_text("ˈt͡ʃaɪnə", singer_id="s")
        assert [c.char for c in s.characters] == ["ˈ", "t͡ʃ", "a", "ɪ", "n", "ə"]

    def test_codepoint_mode(self):
        s = Sentence.from_text("ɛ̃l", singer_id="s", by_codepoint=True)
        assert [c.char for c in s.characters] == ["ɛ", "̃", "l"]

    def test_plain_text_unchanged(self):
        s = Sentence.from_text("赤い花 hello", singer_id="s")
        assert [c.char for c in s.characters] == list("赤い花 hello")

    def test_line_end_on_last(self):
        s = Sentence.from_text("ɛ̃l", singer_id="s")
        assert s.characters[-1].is_line_end


def _legacy_sentence():
    return Sentence(
        singer_id="s",
        characters=[
            Character(char="ɛ", check_count=1, timestamps=[1000], singer_id="s"),
            Character(char="̃", check_count=1, timestamps=[1600], singer_id="s"),
            Character(char="l", check_count=1, timestamps=[2000], singer_id="s"),
            Character(
                char="t",
                check_count=0,
                is_sentence_end=True,
                sentence_end_ts=3000,
                singer_id="s",
            ),
        ],
    )


class TestNormalize:
    def test_merges_dependent_marks_into_leader(self):
        s = _legacy_sentence()
        assert s.normalize_graphemes() is True
        assert [c.char for c in s.characters] == ["ɛ̃", "l", "t"]
        leader = s.characters[0]
        # leader 优先：附加符自己的节奏点数据被丢弃
        assert leader.check_count == 1
        assert leader.timestamps == [1000]

    def test_tail_flags_move_to_cluster_end(self):
        s = _legacy_sentence()
        s.characters[1].linked_to_next = True  # ̃ 连向 l
        s.normalize_graphemes()
        assert s.characters[0].linked_to_next is True

    def test_noop_for_clean_text(self):
        s = Sentence.from_text("赤い花", singer_id="s")
        assert s.normalize_graphemes() is False
        assert [c.char for c in s.characters] == list("赤い花")

    def test_line_end_uniqueness_after_merge(self):
        s = Sentence(
            singer_id="s",
            characters=[
                Character(char="a", is_line_end=True, singer_id="s"),
                Character(char="̃", singer_id="s"),
            ],
        )
        s.normalize_graphemes()
        assert [c.char for c in s.characters] == ["ã"]
        assert s.characters[0].is_line_end is True

    def test_leading_lone_mark_kept(self):
        s = Sentence(
            singer_id="s",
            characters=[Character(char="̃", check_count=1, singer_id="s")],
        )
        s.normalize_graphemes()
        assert [c.char for c in s.characters] == ["̃"]

    def test_tie_bar_second_base_adopts_data_when_leader_empty(self):
        s = Sentence(
            singer_id="s",
            characters=[
                Character(char="t", check_count=1, singer_id="s"),
                Character(char="͡", check_count=0, singer_id="s"),
                Character(char="ʃ", check_count=1, timestamps=[800], singer_id="s"),
            ],
        )
        s.normalize_graphemes()
        assert [c.char for c in s.characters] == ["t͡ʃ"]
        # leader 无时间戳 → 继承连结线后基字的轴
        assert s.characters[0].timestamps == [800]


class TestExplodeMerge:
    def test_roundtrip_identity(self):
        s = Sentence(
            singer_id="s",
            characters=[
                Character(
                    char="t͡ʃ",
                    check_count=2,
                    timestamps=[100, 200],
                    ruby=Ruby(parts=[RubyPart(text="t͡"), RubyPart(text="ʃ")]),
                    is_sentence_end=True,
                    sentence_end_ts=900,
                    singer_id="s",
                ),
                Character(char="a", check_count=0, singer_id="s"),
            ],
        )
        before = [(c.char, c.check_count, c.timestamps, c.sentence_end_ts) for c in s.characters]
        spans = s.explode_graphemes()
        assert spans is not None
        assert len(s.characters) == 4  # t ͡ ʃ a
        # 码点视图中首件带数据、末件带停顿点
        assert s.characters[0].check_count == 2
        assert s.characters[2].is_sentence_end and s.characters[2].sentence_end_ts == 900
        s.merge_grapheme_pieces(spans)
        after = [(c.char, c.check_count, c.timestamps, c.sentence_end_ts) for c in s.characters]
        assert after == before

    def test_noop_when_all_single(self):
        s = Sentence.from_text("あかい", singer_id="s")
        spans = s.explode_graphemes()
        assert spans is None
        s.merge_grapheme_pieces(spans)
        assert [c.char for c in s.characters] == list("あかい")

    def test_pipeline_mutation_preserved(self):
        s = Sentence(
            singer_id="s",
            characters=[Character(char="ɛ̃", check_count=0, singer_id="s")],
        )
        spans = s.explode_graphemes()
        # 模拟管线在码点视图上写 check_count
        s.characters[0].check_count = 1
        s.merge_grapheme_pieces(spans)
        assert s.characters[0].char == "ɛ̃"
        assert s.characters[0].check_count == 1
