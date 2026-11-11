"""IPA 全链路支持集成测试：自动注音 / 文本格式往返 / .sug 加载自愈。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from strange_uta_game.backend.application.auto_check_service import (  # noqa: E402
    AutoCheckService,
)
from strange_uta_game.backend.domain.entities import Sentence  # noqa: E402
from strange_uta_game.backend.domain.models import Character, Ruby, RubyPart  # noqa: E402
from strange_uta_game.backend.infrastructure.parsers.annotated_text import (  # noqa: E402
    parse_timed_line,
    sentence_to_timed_line,
)
from strange_uta_game.backend.infrastructure.parsers.inline_format import (  # noqa: E402
    from_inline_text,
    split_into_moras,
    split_ruby_for_checkpoints,
    split_ruby_segments,
    to_inline_text,
)


def _pipeline(text: str):
    svc = AutoCheckService()
    sent = Sentence.from_text(text, singer_id="s")
    svc.analyze_and_apply_sentence_pipeline(sent)
    return sent


class TestAutoCheckIPAMainText:
    def test_syllable_onset_checkpoints(self):
        sent = _pipeline("ˈhɛləʊ wɜːld")
        got = [(c.char, c.check_count) for c in sent.characters]
        assert got == [
            ("ˈ", 0), ("h", 1), ("ɛ", 0), ("l", 1), ("ə", 0), ("ʊ", 0),
            (" ", 0),
            ("w", 1), ("ɜ", 0), ("ː", 0), ("l", 0), ("d", 0),
        ]

    def test_no_letter_name_katakana_ruby(self):
        sent = _pipeline("ˈhɛləʊ")
        # 修复前：h→エイチ、l→エル（e2k 对单字母的片假名注音）
        for c in sent.characters:
            assert c.ruby is None

    def test_modifiers_never_get_checkpoints(self):
        sent = _pipeline("tʰɪŋ kʷɔɪ̯t")
        # 独立修饰符簇（ʰ ʷ ː ˈ ˌ）永不承载节奏点；带附加符的音段
        # （k̚ 一类成词辅音除外——整词只有它一个音段，节奏点在它上面）
        for c in sent.characters:
            if c.char in ("ʰ", "ʷ", "ː", "ˈ", "ˌ"):
                assert c.check_count == 0, c.char
        # kʷɔɪ̯t 是一个音节：只有 k 有节奏点
        got = [(c.char, c.check_count) for c in sent.characters]
        assert got == [
            ("t", 1), ("ʰ", 0), ("ɪ", 0), ("ŋ", 0),
            (" ", 0),
            ("k", 1), ("ʷ", 0), ("ɔ", 0), ("ɪ̯", 0), ("t", 0),
        ]

    def test_ascii_fragment_inside_ipa_word_not_english(self):
        # 修复前：wɜːld 的 "ld" 被当英文词，d 在词尾自动成停顿点（英文词尾
        # 规则）。词中、非空格前的 d 不应有停顿点：
        sent = _pipeline("wɜːldx")
        d_char = [c for c in sent.characters if c.char == "d"][0]
        assert d_char.is_sentence_end is False

    def test_affricate_single_cluster(self):
        sent = _pipeline("ˈt͡ʃaɪnə")
        clusters = [c.char for c in sent.characters]
        assert "t͡ʃ" in clusters
        assert "͡" not in clusters

    def test_english_words_unchanged(self):
        sent = _pipeline("hello world")
        # e2k 词典仍生效（ヘロー）
        assert sent.characters[0].ruby is not None
        assert sent.characters[0].ruby.text == "ヘロー"


class TestRubySplitting:
    def test_mora_mode_uses_ipa_syllables(self):
        assert split_into_moras("həˈləʊ") == ["hə", "ˈləʊ"]
        assert split_into_moras("tʰiɛn˥") == ["tʰiɛn˥"]

    def test_kana_mora_unchanged(self):
        assert split_into_moras("きょうと") == ["きょ", "う", "と"]

    def test_split_ruby_for_checkpoints(self):
        assert split_ruby_for_checkpoints("həˈləʊ", 2) == ["hə", "ˈləʊ"]
        assert split_ruby_for_checkpoints("tʰiɛn˥", 1) == ["tʰiɛn˥"]

    def test_split_ruby_segments_never_breaks_cluster(self):
        parts = split_ruby_segments("ɛ̃ːn̩d̥", 2, "mora")
        assert "".join(parts) == "ɛ̃ːn̩d̥"
        assert len(parts) == 2


class TestInlineFormatRoundTrip:
    def test_plain_clusters(self):
        # [N|ts] 后的多拍字符：第二拍用无前缀 [ts] 标签
        s = from_inline_text("[2|00:10:00]ɛ̃[00:10:50]", "s")
        assert [c.char for c in s.characters] == ["ɛ̃"]
        assert s.characters[0].check_count == 2
        assert s.characters[0].timestamps == [10000, 10500]
        # 簇后跟裸字符
        s2 = from_inline_text("[1|00:10:00]ɛ̃l", "s")
        assert [c.char for c in s2.characters] == ["ɛ̃", "l"]
        assert s2.characters[1].check_count == 0

    def test_ruby_group_with_ipa(self):
        line = "{ɛ̃l|[1|00:10:00]hə＋[1|00:10:50]ləʊ}"
        s = from_inline_text(line, "s")
        assert [c.char for c in s.characters] == ["ɛ̃", "l"]
        assert [c.ruby.text for c in s.characters] == ["hə", "ləʊ"]
        assert [c.timestamps for c in s.characters] == [[10000], [10500]]
        out = to_inline_text(s)
        s2 = from_inline_text(out, "s")
        assert [(c.char, c.ruby.text if c.ruby else None) for c in s2.characters] == [
            ("ɛ̃", "hə"),
            ("l", "ləʊ"),
        ]


class TestTimedLineRoundTrip:
    def test_ipa_line_roundtrip(self):
        # 真实簇数据（Character.char 是单个字素簇）
        chars = [
            Character(char="t͡ʃ", check_count=1, timestamps=[1000], singer_id="s"),
            Character(char="ɛ̃", check_count=1, is_sentence_end=True,
                      sentence_end_ts=2000, singer_id="s"),
        ]
        chars[0].set_ruby(Ruby(parts=[RubyPart(text="t͡ʃɛ̃")]))
        line, _ = sentence_to_timed_line(chars)
        parsed, _ = parse_timed_line(line)
        assert [c.char for c in parsed] == ["t͡ʃ", "ɛ̃"]
        assert parsed[0].timestamps == [1000]
        assert parsed[0].ruby is not None and parsed[0].ruby.text == "t͡ʃɛ̃"
        assert parsed[1].is_sentence_end and parsed[1].sentence_end_ts == 2000


class TestSugLoadNormalization:
    def test_dict_to_sentence_merges_marks(self):
        from strange_uta_game.backend.infrastructure.persistence.sug_io import (
            SugProjectParser,
        )

        data = {
            "singer_id": "s",
            "characters": [
                {
                    "char": "ɛ", "check_count": 1, "timestamps": [1000],
                    "ruby": {"parts": [{"text": "ɛ̃", "offset_ms": 0}]},
                },
                {"char": "̃", "check_count": 0},
                {"char": "l", "check_count": 1, "timestamps": [2000], "is_line_end": True},
            ],
        }
        sent = SugProjectParser._dict_to_sentence(data, "^")
        assert [c.char for c in sent.characters] == ["ɛ̃", "l"]
        assert sent.characters[0].timestamps == [1000]
        assert sent.characters[0].ruby.text == "ɛ̃"
        assert sent.characters[-1].is_line_end
