"""审查反馈回归测试（第二轮 subagent 审查后的修复验证）。

覆盖：
- 后端 #1：纯修饰符串（˥˥ / 孤立 ˈ）不构成 IPA 词、不获节奏点/停顿点
- 后端 #2：² ³ 1 ⁿ 与正字法撇号不再误归 MODIFIER（ハート² 等保住节奏点）
- 后端 #3：find_ipa_words / find_english_words 对纯 CJK 文本快速返回
- 后端 #4：音节点 ``.`` 被后随修饰符并入同单位后仍开新音节
- 前端 #4：查找对话框过滤标签模式的簇映射偏移换算
- 前端 #5：多码点簇判定助手（替代 len(c)>1 死条件）
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from strange_uta_game.backend.application.auto_check_service import (  # noqa: E402
    AutoCheckService,
)
from strange_uta_game.backend.domain.entities import Sentence  # noqa: E402
from strange_uta_game.backend.infrastructure.parsers import ipa as ipa_mod  # noqa: E402
from strange_uta_game.backend.infrastructure.parsers.english_ruby import (  # noqa: E402
    find_english_words,
)
from strange_uta_game.backend.infrastructure.parsers.text_splitter import (  # noqa: E402
    get_char_type,
    CharType,
    split_text,
)


def _pipeline(text: str):
    svc = AutoCheckService()
    sent = Sentence.from_text(text, singer_id="s")
    svc.analyze_and_apply_sentence_pipeline(sent)
    return sent


class TestModifierOnlyRuns:
    def test_tone_letters_alone_not_a_word(self):
        assert ipa_mod.find_ipa_words("˥˥") == []
        assert ipa_mod.find_ipa_words("ˈ") == []
        assert ipa_mod.find_ipa_words("⁵⁵") == []

    def test_modifier_plus_mark_not_a_word(self):
        assert ipa_mod.find_ipa_words("˥\u0303") == []
        assert ipa_mod.find_ipa_words("\u0361") == []
        assert ipa_mod.find_ipa_words("˥.") == []
        assert ipa_mod.find_ipa_words("ˈ\u203f") == []

    def test_latin_extended_modifier_letters_are_modifiers(self):
        # Latin Extended-F/G 的 IPA 修饰字母不能当作词基/音节首
        mod = "\U00010780"
        assert ipa_mod.find_ipa_words(mod) == []
        assert ipa_mod.ipa_syllable_onset_offsets(mod + "t") == {1}
        assert ipa_mod.find_ipa_words("\U00010780t"), "modifier+t 应是 IPA 词"
        assert get_char_type(mod) == CharType.MODIFIER

    def test_modifier_mark_onset_skips_to_base(self):
        # ˈ̃ 是重音符+组合附加符簇；onset 必须落在 a（偏移 2），不是附加符
        assert ipa_mod.ipa_syllable_onset_offsets("ˈ\u0303a") == {2}

    def test_onset_for_modifier_only_is_zero_not_none(self):
        # 公共 API 不得向调用方泄漏 {None}（_english_word_check_counts 会按
        # 码点下标使用）。
        assert ipa_mod.ipa_syllable_onset_offsets("ˈ") == {0}
        assert ipa_mod.ipa_syllable_onset_offsets("˥˥") == {0}
        assert None not in ipa_mod.ipa_syllable_onset_offsets("˥\u0303")

    def test_superscript_modifier_letters_trigger_ipa(self):
        # ʰ ʷ ʲ 等是 IPA 上标修饰字母；只含 ASCII + 它们也应识别为 IPA，
        # 否则主文字会退回 CJK 字体、自动打轴按英文处理。
        for text in ("tʰa", "kʷi", "pʲa", "aˀ", "a˪"):
            assert ipa_mod.has_ipa_text(text) is True, text
            assert ipa_mod.is_ipa_word(text) is True, text
            assert ipa_mod.find_ipa_words(text), text
        assert ipa_mod.find_ipa_words("ʰ") == []

    def test_tone_letters_no_checkpoint_no_pause(self):
        # 注：空格前的 se=True 来自通用「空格视为停顿点」规则（对所有字符
        # 生效，非 IPA 特有），因此用例不含空格，只验证词级节奏点/停顿点
        sent = _pipeline("˥˥ne x")
        tones = [
            (c.char, c.check_count, c.is_sentence_end)
            for c in sent.characters
            if c.char == "˥"
        ]
        assert all(cc == 0 and not se for _, cc, se in tones), tones

    def test_lone_stress_no_checkpoint(self):
        sent = _pipeline("ˈ")
        assert sent.characters[0].check_count == 0

    def test_tone_letters_still_attach_inside_words(self):
        # 词内的声调字母仍按修饰符处理（不破坏 ma⁵⁵ / ɕjɛn˥˥ 行为）
        assert ipa_mod.find_ipa_words("ma⁵⁵") == [(0, 4, "ma⁵⁵")]
        sent = _pipeline("ɕjɛn˥˥")
        assert [c.char for c in sent.characters if c.check_count > 0] == ["ɕ"]


class TestOrthographicSuperscriptsKeepBehavior:
    def test_squared_keeps_checkpoint(self):
        _, counts = split_text("ハート²")
        assert counts == [1, 1, 1, 1], counts

    def test_superscript_n_is_alphabet(self):
        assert get_char_type("ⁿ") == CharType.ALPHABET

    def test_superscript_digits_are_number(self):
        for ch in "²³¹":
            assert get_char_type(ch) == CharType.NUMBER, ch

    def test_okina_is_alphabet(self):
        # 夏威夷 ʻokina / 土耳其 ʼ —— 正字法实义字符
        assert get_char_type("ʻ") == CharType.ALPHABET
        assert get_char_type("ʼ") == CharType.ALPHABET

    def test_dot_does_not_pull_ascii_word_into_ipa(self):
        # 英文词 + 点号 + IPA：点号不能把 Nice 拖成 IPA 词
        assert ipa_mod.find_ipa_words("Nice.tʰa") == [(5, 8, "tʰa")]
        assert ipa_mod.find_ipa_words(".tʰa") == [(1, 4, "tʰa")]
        # 左侧本身是 IPA/非 ASCII 片段时，点号仍作为音节边界保留
        assert ipa_mod.find_ipa_words("æk.tʃən") == [(0, 7, "æk.tʃən")]
        assert ipa_mod.find_ipa_words("k.ʰa") == [(0, 4, "k.ʰa")]
        # 英文词不能跨进 IPA 范围；只保留点号前的 ASCII 词
        assert find_english_words("Nice.tʰa") == [
            (0, 4, "Nice"),
            (5, 8, "tʰa"),
        ]

    def test_ipa_core_modifiers_still_modifier(self):
        for ch in ("ˈ", "ˌ", "ː", "ʰ", "ʷ", "˥", "ˤ", "ˀ", "ˁ", "˪", "˫"):
            assert get_char_type(ch) == CharType.MODIFIER, ch


class TestFastPathPerformance:
    def test_cjk_line_find_english_words_fast(self):
        # 纯 CJK 行无 IPA 字符 → find_ipa_words 一次扫描返回 []
        line = "赤い花咲く風の中で歩き出す明日へと向かって歩いていくさよならの" * 2
        assert ipa_mod.has_ipa_text(line) is False
        t0 = time.perf_counter()
        for _ in range(200):
            find_english_words(line)
        elapsed = time.perf_counter() - t0
        # 回归阈值：修复前 ~360ms（逐簇 next_grapheme_end），修复后一个数量级以内
        assert elapsed < 0.35, f"find_english_words on CJK too slow: {elapsed*1000:.1f}ms/200"

    def test_find_ipa_words_empty_on_plain(self):
        assert ipa_mod.find_ipa_words("赤い花") == []
        assert ipa_mod.find_ipa_words("hello world") == []


class TestSyllableBreakWithModifier:
    def test_dot_before_modifier_still_breaks(self):
        assert ipa_mod.ipa_syllables("æk.ʰə") == ["æk.", "ʰə"]
        assert ipa_mod.ipa_syllables("æk.ə") == ["æk.", "ə"]


class TestFindDialogClusterMapping:
    """前端 #4：视觉串码点偏移 → 簇下标换算（_build_stripped_mapping 按簇）。"""

    def _mapping(self, text):
        # 直接复用前端模块的映射构造（不依赖 Qt 事件循环）
        from strange_uta_game.frontend.editor.find_dialog import _build_stripped_mapping

        return _build_stripped_mapping(text)

    def test_mapping_is_per_cluster(self):
        visual, mapping = self._mapping("ɛ̃ab")
        assert visual == "ɛ̃ab"
        # 码点偏移 0..4，簇只有 3 个 → mapping 长度按簇
        assert mapping == [0, 2, 3], mapping

    def test_match_after_cluster_maps_correctly(self):
        from strange_uta_game.backend.domain.graphemes import (
            chars_to_offsets,
            offset_to_index,
            split_graphemes,
        )

        visual, mapping = self._mapping("ɛ̃ab")
        offsets = chars_to_offsets(split_graphemes(visual))
        # 查 "ab"：find 得到码点偏移 2 → 簇 1..2 → mapping[1..2] = [2, 3]
        idx = visual.find("ab")
        c0 = offset_to_index(offsets, idx)
        c1 = offset_to_index(offsets, idx + len("ab") - 1)
        assert mapping[c0 : c1 + 1] == [2, 3]

    def test_replace_all_with_filter_after_cluster(self):
        from strange_uta_game.frontend.editor.find_dialog import FindDialog

        text = "ɛ̃ab cx"
        new_text, count = FindDialog._replace_all_with_filter(None, text, "ab", "zz")
        assert new_text == "ɛ̃zz cx", new_text
        assert count == 1


class TestMultiClusterPredicate:
    def test_has_multicodepoint_cluster(self):
        # 前端 #5：len(c)>1 死条件的正确替代
        from strange_uta_game.frontend.editor.timing.karaoke_preview import (
            _has_multicodepoint_cluster,
        )

        assert _has_multicodepoint_cluster("ɛ̃")
        assert _has_multicodepoint_cluster("a\u0301")
        assert _has_multicodepoint_cluster("⛰️")
        assert not _has_multicodepoint_cluster("ab")
        assert not _has_multicodepoint_cluster("hello")
        assert not _has_multicodepoint_cluster("")
