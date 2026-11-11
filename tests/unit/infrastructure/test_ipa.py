"""ipa 模块单元测试：触发判定 / 语音单位 / 音节切分。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from strange_uta_game.backend.infrastructure.parsers.ipa import (  # noqa: E402
    find_ipa_words,
    has_ipa_text,
    ipa_syllable_onset_offsets,
    ipa_syllables,
    is_ipa_word,
    is_phonetic_modifier,
    is_stress_mark,
    split_phonetic_units,
)


class TestTriggers:
    def test_ipa_extensions_trigger(self):
        assert has_ipa_text("ɛ")
        assert has_ipa_text("ʃ")
        assert has_ipa_text("ɕ")

    def test_modifier_letters_trigger(self):
        assert has_ipa_text("ˈa")
        assert has_ipa_text("aː")

    def test_orthographic_latin_not_triggered(self):
        assert not has_ipa_text("café")
        assert not has_ipa_text("naïve")
        assert not has_ipa_text("hello world")

    def test_cjk_not_triggered(self):
        assert not has_ipa_text("赤い花")
        assert not has_ipa_text("사랑해")

    def test_tone_letters_trigger(self):
        assert has_ipa_text("n˥˥")

    def test_superscript_tone_digits_trigger(self):
        assert has_ipa_text("ma\u2075\u2075")  # ma⁵⁵
        assert not has_ipa_text("mc²")  # 常见序号上标不触发


class TestFindIPAWords:
    def test_simple(self):
        assert find_ipa_words("ˈhɛləʊ wɜːld") == [
            (0, 6, "ˈhɛləʊ"),
            (7, 12, "wɜːld"),
        ]

    def test_mixed_with_cjk(self):
        words = find_ipa_words("红色ˈhɛləʊ world wɜːld cafe")
        assert [w[2] for w in words] == ["ˈhɛləʊ", "wɜːld"]
        assert [w[:2] for w in words] == [(2, 8), (15, 20)]

    def test_english_words_untouched(self):
        assert find_ipa_words("hello world") == []

    def test_word_includes_diacritics_and_tie(self):
        words = find_ipa_words("ˈt͡ʃaɪnə")
        assert len(words) == 1
        assert words[0][2] == "ˈt͡ʃaɪnə"


class TestPhoneticUnits:
    def test_modifiers_attach_back(self):
        assert split_phonetic_units("tʰɪŋ") == ["tʰ", "ɪ", "ŋ"]
        assert split_phonetic_units("kʷɔɪ̯t".replace("̯", "\u032f")) == [
            "kʷ",
            "ɔ",
            "ɪ\u032f",
            "t",
        ]

    def test_stress_attaches_forward(self):
        assert split_phonetic_units("həˈləʊ") == ["h", "ə", "ˈl", "ə", "ʊ"]

    def test_syllable_break_attaches_back(self):
        assert split_phonetic_units("æk.tʃən") == ["æ", "k.", "t", "ʃ", "ə", "n"]

    def test_tone_letters_attach_back(self):
        assert split_phonetic_units("ɕjɛn˥˥") == ["ɕ", "j", "ɛ", "n˥˥"]

    def test_lossless(self):
        for text in ["ˈt͡ʃaɪnə", "n̩d̥ tʰɪŋ", "həˈləʊ", "æktʃən", "ɕjɛn˥˥"]:
            assert "".join(split_phonetic_units(text)) == text


class TestSyllables:
    def test_two_syllable_word(self):
        assert ipa_syllables("həˈləʊ") == ["hə", "ˈləʊ"]

    def test_explicit_break(self):
        assert ipa_syllables("æk.tʃən") == ["æk.", "tʃən"]

    def test_single_syllable(self):
        assert ipa_syllables("tʰɪŋ") == ["tʰɪŋ"]
        assert ipa_syllables("wɜːld") == ["wɜːld"]

    def test_syllabic_consonant_is_nucleus(self):
        assert ipa_syllables("n̩d̥") == ["n̩d̥"]

    def test_mandarin_tone(self):
        assert ipa_syllables("ɕjɛn˥˥") == ["ɕjɛn˥˥"]

    def test_words_split_by_space(self):
        sylls = ipa_syllables("həˈləʊ wɜːld")
        assert "".join(sylls) == "həˈləʊ wɜːld"
        assert len(sylls) == 3

    def test_lossless(self):
        for text in ["zəˈdɛnˌtɛst ɪt", "ˈt͡ʃiːnz", "təˈmɑːtəʊ"]:
            assert "".join(ipa_syllables(text)) == text


class TestSyllableOnsetOffsets:
    def test_onsets_point_at_segments_not_stress(self):
        # 音节首落在实音段（h/l），重音符 ˈ 不承载节奏点
        assert ipa_syllable_onset_offsets("ˈhɛləʊ") == {1, 3}

    def test_affricate_onset(self):
        # t͡ʃ 是一个音段（1–3 码点）；第二音节 n 起在 6
        assert ipa_syllable_onset_offsets("ˈt͡ʃaɪnə") == {1, 6}

    def test_single_syllable(self):
        assert ipa_syllable_onset_offsets("tʰɪŋ") == {0}

    def test_explicit_dot(self):
        assert ipa_syllable_onset_offsets("æk.tʃən") == {0, 3}

    def test_always_contains_zero(self):
        assert 0 in ipa_syllable_onset_offsets("xyz")


class TestPredicates:
    def test_is_ipa_word(self):
        assert is_ipa_word("ˈhɛləʊ")
        assert not is_ipa_word("hello")

    def test_is_stress_mark(self):
        assert is_stress_mark("ˈ")
        assert is_stress_mark("ˌ")
        assert not is_stress_mark("ː")

    def test_is_phonetic_modifier(self):
        assert is_phonetic_modifier("ʰ")
        assert is_phonetic_modifier("ː")
        assert is_phonetic_modifier("˥")
        assert not is_phonetic_modifier("a")
