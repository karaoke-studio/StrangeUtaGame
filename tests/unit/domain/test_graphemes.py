"""graphemes 模块单元测试：字素簇切分 + IPA 裁剪 + 工具函数。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from strange_uta_game.backend.domain.graphemes import (  # noqa: E402
    chars_to_offsets,
    grapheme_base,
    grapheme_boundaries,
    grapheme_count,
    is_dependent_text,
    is_grapheme_extender,
    is_single_grapheme,
    next_grapheme_end,
    offset_to_index,
    split_graphemes,
    truncate_graphemes,
)


def _codes(units):
    return ["+".join(f"{ord(c):04X}" for c in u) for u in units]


class TestBasicSegmentation:
    def test_ascii_identity(self):
        assert split_graphemes("hello") == list("hello")

    def test_cjk_identity(self):
        assert split_graphemes("赤い花") == ["赤", "い", "花"]

    def test_kana_identity(self):
        assert split_graphemes("だいぼうけん") == list("だいぼうけん")

    def test_lossless(self):
        for text in ["赤い花", "ˈhɛləʊ", "t͡ʃaɪ", "e\u0301\u0303", "🇯🇵"]:
            assert "".join(split_graphemes(text)) == text

    def test_empty(self):
        assert split_graphemes("") == []
        assert grapheme_boundaries("") == [0]

    def test_combining_mark_joins_base(self):
        assert split_graphemes("a\u0303") == ["a\u0303"]
        assert split_graphemes("n\u0329") == ["n\u0329"]
        assert split_graphemes("ɛ\u0303") == ["ɛ\u0303"]

    def test_stacked_marks(self):
        assert split_graphemes("e\u0301\u0303") == ["e\u0301\u0303"]
        assert split_graphemes("ɑ\u0330\u0303") == ["ɑ\u0330\u0303"]

    def test_variation_selector_joins(self):
        assert split_graphemes("\u26f0\ufe0f") == ["\u26f0\ufe0f"]

    def test_emoji_zwj_sequence_is_one_cluster(self):
        fam = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
        assert split_graphemes(fam) == [fam]

    def test_regional_indicator_pairs(self):
        assert split_graphemes("\U0001F1EF\U0001F1F5") == ["\U0001F1EF\U0001F1F5"]
        assert len(split_graphemes("\U0001F1EF\U0001F1F5\U0001F1EF\U0001F1F5")) == 2

    def test_hangul_jamo_compose(self):
        assert split_graphemes("\u1100\u1161\u11a8") == ["\u1100\u1161\u11a8"]

    def test_kirat_rai_vowel_signs_are_gcb_v(self):
        # Unicode 16：U+16D63/U+16D67..16D6A 是 GCB=V，不是 Extend；
        # 不能把前面的任意字符并进来。
        v = "\U00016D63"
        v2 = "\U00016D67"
        assert split_graphemes("a" + v + v2) == ["a", v + v2]
        assert split_graphemes(v2) == [v2]

    def test_future_combining_block_tail_is_extend(self):
        # 组合附加符区块末尾的未来码点按 Extend 容错（不把基字与附加符拆开）
        assert split_graphemes("a\U00001ACF") == ["a\U00001ACF"]
        assert split_graphemes("a\U00001DFF") == ["a\U00001DFF"]
        # 仍是未分配空洞（UCD 17 的 1ADE/1ADF、1AEC–1AFF）不能并簇
        assert split_graphemes("a\U00001ADE") == ["a", "\U00001ADE"]
        assert split_graphemes("a\U00001AEC") == ["a", "\U00001AEC"]

    def test_devanagari_conjunct(self):
        assert split_graphemes("\u0915\u094d\u0937\u093f") == ["\u0915\u094d\u0937\u093f"]


class TestIPATailoring:
    def test_tie_bar_joins_two_bases(self):
        assert split_graphemes("t\u0361\u0283") == ["t\u0361\u0283"]
        assert split_graphemes("k\u035cp") == ["k\u035cp"]
        assert split_graphemes("a\u0361b") == ["a\u0361b"]

    def test_tie_bar_with_extra_marks(self):
        # 连结线后接修饰字母：塞擦音成簇，修饰字母独立（与 ˈ ː 一致）
        assert split_graphemes("t\u0361\u0283\u02b0") == ["t\u0361\u0283", "\u02b0"]
        # 连结线上再叠加组合附加符：同一簇
        assert split_graphemes("a\u0361\u0303") == ["a\u0361\u0303"]

    def test_tie_bar_does_not_join_non_letter(self):
        # 连结线后是非字母（语法字符 / 空格）不并簇
        assert split_graphemes("a\u0361 ")[0] == "a\u0361"

    def test_halfwidth_voiced_mark_keeps_split(self):
        # 兼容裁剪：半角浊点保持独立字符（既有日文流程行为）
        assert split_graphemes("\uff76\uff9e") == ["\uff76", "\uff9e"]

    def test_stress_and_length_are_own_clusters(self):
        assert split_graphemes("\u02c8h") == ["\u02c8", "h"]
        assert split_graphemes("o\u02d0") == ["o", "\u02d0"]

    def test_modifier_letters_are_own_clusters(self):
        assert split_graphemes("t\u02b0") == ["t", "\u02b0"]


class TestHelpers:
    def test_grapheme_count(self):
        assert grapheme_count("ˈt͡ʃaɪ") == 4
        assert grapheme_count("abc") == 3
        assert grapheme_count("") == 0

    def test_is_single_grapheme(self):
        assert is_single_grapheme("ɛ̃")
        assert is_single_grapheme("a")
        assert not is_single_grapheme("ab")
        assert not is_single_grapheme("")

    def test_is_grapheme_extender(self):
        assert is_grapheme_extender("\u0303")
        assert is_grapheme_extender("\u0361")
        assert is_grapheme_extender("\ufe0f")
        assert not is_grapheme_extender("a")
        assert not is_grapheme_extender("\uff9e")  # 兼容裁剪
        assert not is_grapheme_extender("a\u0303")

    def test_is_dependent_text(self):
        assert is_dependent_text("\u0303")
        assert is_dependent_text("\ufe0f\u0301")
        assert not is_dependent_text("a\u0303")
        assert not is_dependent_text("")

    def test_grapheme_base(self):
        assert grapheme_base("ɛ̃") == "ɛ"
        assert grapheme_base("か\u3099") == "が"  # NFC 合成
        assert grapheme_base("t\u0361\u0283") == "t"
        assert grapheme_base("\u0303") == "\u0303"
        assert grapheme_base("") == ""

    def test_next_grapheme_end(self):
        s = "a\u0303b"
        assert next_grapheme_end(s, 0) == 2
        assert next_grapheme_end(s, 2) == 3
        assert next_grapheme_end(s, 3) == 3
        assert next_grapheme_end("t\u0361\u0283x", 0) == 3

    def test_truncate_graphemes(self):
        assert truncate_graphemes("ɛ̃l", 1) == "ɛ̃"
        assert truncate_graphemes("ɛ̃l", 2) == "ɛ̃l"
        assert truncate_graphemes("abc", 2) == "ab"
        assert truncate_graphemes("abc", 0) == ""

    def test_chars_to_offsets_and_lookup(self):
        offsets = chars_to_offsets(["ɛ̃", "l", "o"])
        assert offsets == [0, 2, 3, 4]
        assert offset_to_index(offsets, 0) == 0
        assert offset_to_index(offsets, 1) == 0  # 命中簇内部
        assert offset_to_index(offsets, 2) == 1
        assert offset_to_index(offsets, 3) == 2
        assert offset_to_index(offsets, 4) == 3

    def test_boundaries_shape(self):
        assert grapheme_boundaries("ɛ̃l") == [0, 2, 3]
        assert grapheme_boundaries("ab") == [0, 1, 2]


class TestFastPathEquivalence:
    def test_fast_path_equals_slow_path(self):
        # 快路径（全 < U+0300）与完整算法一致：断言加括号修复运算符优先级
        # （原写法 `a == b if t else c` 恒为恒真式，审查反馈 #9）
        for text in ["hello world", "abc123 !?", ""]:
            assert split_graphemes(text) == (list(text) if text else [])

    def test_slow_path_handwritten_matches_full(self):
        # 慢路径（含 ≥ U+0300 码点）与手工分簇一致
        cases = {
            "あかい": ["あ", "か", "い"],
            "ãb": ["ã", "b"],
            "t͡ʃx": ["t͡ʃ", "x"],
            "é̃!": ["é̃", "!"],
        }
        for text, expected in cases.items():
            assert split_graphemes(text) == expected
