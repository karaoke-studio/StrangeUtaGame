"""纯转换器测试：不依赖 Sentence/Character，只测 romanize_ruby_parts。"""

import pytest

from strange_uta_game.backend.infrastructure.parsers.romaji import (
    RomajiOptions,
    romanize_ruby_parts,
)


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        (["ｶﾞ", "ﾊﾟ", "ﾜﾞ", "ｦﾞ"], ["ga", "pa", "va", "vo"]),
        (["ｷ", "ﾞ", "ｬ"], ["gya", "", ""]),
        (["ｯ", "ｷ", "ﾞ", "ｬ"], ["g", "gya", "", ""]),
        (["ﾋ", "ﾟ", "ｰ"], ["pi", "", "i"]),
        (["ﾝ", "ﾔ", "ｯ", "ﾞ"], ["n'", "ya", "xtsu", "ﾞ"]),
        (["ｶ", "ﾟ", "ｰ"], ["ka", "ﾟ", "-"]),
        (
            ["Ａ①｡｢｣､･(ﾟ∀ﾟ)", "ｷ", "ﾟ", "ｬ"],
            ["Ａ①｡｢｣､･(ﾟ∀ﾟ)", "ki", "ﾟ", "ya"],
        ),
    ],
)
def test_halfwidth_katakana_preserves_reading_part_ownership(parts, expected):
    assert romanize_ruby_parts(parts) == expected


class TestBasicKana:
    def test_hepburn_basic(self):
        assert romanize_ruby_parts(["し", "ち", "つ", "ふ", "じ"]) == ["shi", "chi", "tsu", "fu", "ji"]

    def test_seion(self):
        assert romanize_ruby_parts(["あ", "か", "さ", "た", "な"]) == ["a", "ka", "sa", "ta", "na"]

    def test_dakuon(self):
        assert romanize_ruby_parts(["が", "ざ", "だ", "ば"]) == ["ga", "za", "da", "ba"]

    def test_handakuon(self):
        assert romanize_ruby_parts(["ぱ", "ぴ", "ぷ", "ぺ", "ぽ"]) == ["pa", "pi", "pu", "pe", "po"]

    def test_youon_same_part(self):
        assert romanize_ruby_parts(["きゃ", "きゅ", "きょ"]) == ["kya", "kyu", "kyo"]
        assert romanize_ruby_parts(["しゃ", "しゅ", "しょ"]) == ["sha", "shu", "sho"]
        assert romanize_ruby_parts(["ちゃ", "ちゅ", "ちょ"]) == ["cha", "chu", "cho"]


class TestSokuon:
    """促音独立成拍（FA-Kara sokuon_split 口径）：读后字辅音，不并入后字。"""

    def test_sokuon_gemination(self):
        assert romanize_ruby_parts(["ま", "っ", "て"]) == ["ma", "t", "te"]

    def test_sokuon_before_cha(self):
        assert romanize_ruby_parts(["こ", "っ", "ち"]) == ["ko", "c", "chi"]

    def test_sokuon_before_cha_digraph(self):
        assert romanize_ruby_parts(["ま", "っ", "ち", "ゃ"]) == [
            "ma", "c", "cha", "",
        ]

    def test_sokuon_isolated(self):
        assert romanize_ruby_parts(["っ"]) == ["xtsu"]

    def test_sokuon_alignment_spelling_matches_fakara(self):
        """AI 打轴拼写（FA-Kara 口径）：促音遇ち行写作 t、孤立促音回退 h。"""
        assert romanize_ruby_parts(
            ["ま", "っ", "ち"], for_alignment=True
        ) == ["ma", "t", "chi"]
        assert romanize_ruby_parts(
            ["ま", "っ", "ち", "ゃ"], for_alignment=True
        ) == ["ma", "t", "cha", ""]
        assert romanize_ruby_parts(["っ"], for_alignment=True) == ["h"]
        # 显示/导出口径不受影响
        assert romanize_ruby_parts(["ま", "っ", "ち"]) == ["ma", "c", "chi"]
        assert romanize_ruby_parts(["っ"]) == ["xtsu"]


class TestLongVowel:
    def test_long_vowel_repeats_previous(self):
        assert romanize_ruby_parts(["す", "ー", "ぱ", "ー"]) == ["su", "u", "pa", "a"]

    def test_long_vowel_initial(self):
        assert romanize_ruby_parts(["ー", "あ"]) == ["-", "a"]

    def test_long_vowel_hyphen_is_absorbed_by_leading_part(self):
        options = RomajiOptions(repeat_long_vowels=False)
        assert romanize_ruby_parts(["ぴ", "ー"], options=options) == ["pi-", ""]

    def test_long_vowel_hyphen_uses_digraph_leading_part(self):
        options = RomajiOptions(repeat_long_vowels=False)
        assert romanize_ruby_parts(
            ["き", "ゃ", "ー"], options=options
        ) == ["kya-", "", ""]


class TestN:
    def test_n_before_vowel(self):
        assert romanize_ruby_parts(["ん", "あ"]) == ["n'", "a"]

    def test_n_before_consonant(self):
        assert romanize_ruby_parts(["あ", "ん", "し"]) == ["a", "n", "shi"]

    def test_n_before_y(self):
        assert romanize_ruby_parts(["こ", "ん", "や"]) == ["ko", "n'", "ya"]


class TestCrossPart:
    def test_cross_part_youon_split(self):
        assert romanize_ruby_parts(["き", "ょ", "う"]) == ["kyo", "", "u"]

    def test_cross_part_youon_with_timing(self):
        assert romanize_ruby_parts(["きょ", "う"]) == ["kyo", "u"]

    def test_preserves_part_count_for_sokuon(self):
        result = romanize_ruby_parts(["ま", "っ", "て"])
        assert len(result) == 3


class TestParticles:
    def test_particle_opt_in(self):
        assert romanize_ruby_parts(["は", "へ", "を"]) == ["ha", "he", "o"]

    def test_particle_enabled(self):
        assert romanize_ruby_parts(["は", "へ", "を"], particle_indices={0, 1, 2}) == ["wa", "e", "o"]

    def test_only_wo_default_particle(self):
        assert romanize_ruby_parts(["を"]) == ["o"]


class TestEdgeCases:
    def test_ascii_passthrough(self):
        assert romanize_ruby_parts(["Ro", "me", "o"]) == ["Ro", "me", "o"]

    def test_empty_input(self):
        assert romanize_ruby_parts([]) == []

    def test_katakana(self):
        assert romanize_ruby_parts(["カ", "キ", "ク"]) == ["ka", "ki", "ku"]

    def test_mixed_katakana_youon(self):
        assert romanize_ruby_parts(["キャ", "キュ", "キョ"]) == ["kya", "kyu", "kyo"]

    def test_katakana_sokuon(self):
        assert romanize_ruby_parts(["マ", "ッ", "テ"]) == ["ma", "t", "te"]

    def test_uppercase_applies_after_conversion(self):
        options = RomajiOptions(uppercase=True)
        assert romanize_ruby_parts(
            ["わ", "た", "し", "ん", "あ"], options=options
        ) == ["WA", "TA", "SHI", "N'", "A"]

    def test_small_ka_and_ke_are_romanized(self):
        assert romanize_ruby_parts(["ヵ", "ヶ"]) == ["ka", "ke"]
