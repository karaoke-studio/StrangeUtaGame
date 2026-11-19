"""罗马音注音集成测试：验证 AutoCheckService 开关全链路行为。"""

import pytest
from strange_uta_game.backend.application import AutoCheckService
from strange_uta_game.backend.domain import Character, Project, Ruby, RubyPart, Sentence
from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import DummyAnalyzer, RubyAnalyzer, RubyResult

ROMAJI_FLAGS = {"romanize_ruby": True, "check_n": True, "check_sokuon": True}


class StaticAnalyzer(RubyAnalyzer):
    def __init__(self, results):
        self._results = results
    def analyze(self, text: str):
        return self._results
    def get_reading(self, text: str) -> str:
        return "".join(result.reading for result in self._results)


def _ruby_parts(sentence: Sentence):
    return [[part.text for part in ch.ruby.parts] if ch.ruby else [] for ch in sentence.characters]


def test_kana_self_reading_gets_ruby_and_romaji():
    service = AutoCheckService(DummyAnalyzer(), auto_check_flags=ROMAJI_FLAGS)
    sentence = Sentence.from_text("あい", "s1")
    service.apply_to_sentence(sentence)
    assert _ruby_parts(sentence) == [["a"], ["i"]]
    assert [ch.check_count for ch in sentence.characters] == [1, 1]


@pytest.mark.parametrize(
    ("text", "reading", "expected"),
    [
        ("ｷﾞｬ", "ぎゃ", [["gya"], [], [""]]),
        ("ﾋﾟｰ", "ぴー", [["pi"], [], ["i"]]),
    ],
)
def test_auto_romaji_of_halfwidth_kana_with_normalized_analyzer_reading(
    text, reading, expected,
):
    analyzer = StaticAnalyzer([RubyResult(text, reading, 0, len(text))])
    service = AutoCheckService(analyzer, auto_check_flags=ROMAJI_FLAGS)
    sentence = Sentence.from_text(text, "s1")

    service.apply_to_sentence(sentence)

    assert sentence.text == text
    assert _ruby_parts(sentence) == expected
    assert sentence.characters[0].linked_to_next
    assert sentence.characters[1].check_count == 0


def test_auto_romaji_does_not_annotate_standalone_halfwidth_voicing_marks():
    service = AutoCheckService(DummyAnalyzer(), auto_check_flags=ROMAJI_FLAGS)
    sentence = Sentence.from_text("(ﾟ∀ﾟ)", "s1")

    service.apply_to_sentence(sentence)

    assert sentence.text == "(ﾟ∀ﾟ)"
    for index in (1, 3):
        assert sentence.characters[index].ruby is None
        assert sentence.characters[index].check_count == 0


def test_kanji_gets_romaji_ruby():
    analyzer = StaticAnalyzer([RubyResult(text="今日", reading="きょう", start_idx=0, end_idx=2)])
    service = AutoCheckService(analyzer, auto_check_flags=ROMAJI_FLAGS)
    sentence = Sentence.from_text("今日", "s1")
    service.apply_to_sentence(sentence)
    parts = _ruby_parts(sentence)
    for p_list in parts:
        for p in p_list:
            assert all(c.isascii() or c == "'" for c in p), f"Expected ASCII, got {p}"


def test_sokuon_cross_part_context():
    analyzer = StaticAnalyzer([
        RubyResult(text="待", reading="ま", start_idx=0, end_idx=1),
        RubyResult(text="っ", reading="っ", start_idx=1, end_idx=2),
        RubyResult(text="て", reading="て", start_idx=2, end_idx=3),
    ])
    service = AutoCheckService(analyzer, auto_check_flags=ROMAJI_FLAGS)
    sentence = Sentence.from_text("待って", "s1")
    service.apply_to_sentence(sentence)
    # 促音独立成拍：っ 自持辅音 t，て 为 te
    assert _ruby_parts(sentence) == [["ma"], ["t"], ["te"]]
    assert [ch.check_count for ch in sentence.characters] == [1, 1, 1]


def test_cross_character_digraph_link_is_created_by_romaji_step():
    analyzer = StaticAnalyzer([
        RubyResult(text=base, reading=reading, start_idx=i, end_idx=i + 1)
        for i, (base, reading) in enumerate(
            zip("キャンセル", ("き", "ゃ", "ん", "せ", "る"))
        )
    ])
    service = AutoCheckService(analyzer, auto_check_flags=ROMAJI_FLAGS)
    sentence = Sentence.from_text("キャンセル", "s1")

    service.apply_to_sentence(sentence)

    assert _ruby_parts(sentence) == [["kya"], [""], ["n"], ["se"], ["ru"]]
    assert [ch.linked_to_next for ch in sentence.characters] == [
        True,
        False,
        False,
        False,
        False,
    ]


def test_romaji_display_options_are_read_from_auto_check_flags():
    flags = {
        **ROMAJI_FLAGS,
        "romaji_repeat_long_vowels": False,
        "romaji_link_long_vowels": False,
        "romaji_uppercase": True,
    }
    service = AutoCheckService(DummyAnalyzer(), auto_check_flags=flags)
    sentence = Sentence.from_text("ピーあぁ", "s1")

    service.apply_to_sentence(sentence)

    assert _ruby_parts(sentence) == [["PI-"], [""], ["A"], ["A"]]
    assert [ch.linked_to_next for ch in sentence.characters] == [
        True,
        False,
        True,
        False,
    ]


def test_kana_annotation_without_romaji_does_not_create_digraph_link():
    analyzer = StaticAnalyzer([
        RubyResult(text=base, reading=reading, start_idx=i, end_idx=i + 1)
        for i, (base, reading) in enumerate(
            zip("キャンセル", ("き", "ゃ", "ん", "せ", "る"))
        )
    ])
    service = AutoCheckService(analyzer, auto_check_flags={})
    sentence = Sentence.from_text("キャンセル", "s1")

    service.apply_to_sentence(sentence)

    assert _ruby_parts(sentence) == [["き"], ["ゃ"], ["ん"], ["せ"], ["る"]]
    assert not any(ch.linked_to_next for ch in sentence.characters)


def test_user_dict_romaji_override():
    service = AutoCheckService(DummyAnalyzer(), auto_check_flags=ROMAJI_FLAGS,
        user_dictionary=[{"enabled": True, "word": "今日", "reading": "{今日||きょ,う}"}])
    sentence = Sentence.from_text("今日", "s1")
    service.apply_to_sentence(sentence)
    assert _ruby_parts(sentence) == [["kyo"], ["u"]]
    assert [ch.check_count for ch in sentence.characters] == [1, 1]


def test_user_dict_restores_kana_for_romaji():
    service = AutoCheckService(DummyAnalyzer(), auto_check_flags=ROMAJI_FLAGS,
        user_dictionary=[{"enabled": True, "word": "\u751f\u304d\u69d8",
            "reading": "{\u751f||\u3044}\u304d{\u69d8||\u3056|\u307e}"}])
    sentence = Sentence.from_text("\u751f\u304d\u69d8", "s1")
    service.apply_to_sentence(sentence)
    assert _ruby_parts(sentence) == [["i"], ["ki"], ["za", "ma"]]
    assert [ch.check_count for ch in sentence.characters] == [1, 1, 2]


def test_linked_kanji_no_romaji_self_ruby():
    entries = [{"enabled": True, "word": "\u8cb4\u65b9", "reading": "{\u8cb4\u65b9||\u3042|\u306a|\u305f,}"}]
    service = AutoCheckService(DummyAnalyzer(), auto_check_flags=ROMAJI_FLAGS, user_dictionary=entries)
    sentence = Sentence.from_text("\u8cb4\u65b9", "s1")
    service.apply_to_sentence(sentence)
    chars = sentence.characters
    assert chars[0].linked_to_next is True
    assert chars[1].ruby is None
    assert chars[1].check_count == 0


def test_update_checkpoints_preserves_part_count_in_romaji_mode():
    service = AutoCheckService(DummyAnalyzer(), auto_check_flags=ROMAJI_FLAGS)
    sentence = Sentence(singer_id="s1", characters=[
        Character(char="\u97f3", ruby=Ruby(parts=[RubyPart(text="hi"), RubyPart(text="bi"), RubyPart(text="ki")]),
                  check_count=1, singer_id="s1")])
    service.update_checkpoints_from_rubies(sentence)
    assert sentence.characters[0].check_count == 3


def test_default_off_preserves_kana_ruby():
    service = AutoCheckService(DummyAnalyzer(), auto_check_flags={})
    sentence = Sentence.from_text("あい", "s1")
    service.apply_to_sentence(sentence)
    assert _ruby_parts(sentence) == [[], []]
    assert [ch.check_count for ch in sentence.characters] == [1, 1]


def test_particle_wa_detection():
    service = AutoCheckService(DummyAnalyzer(), auto_check_flags=ROMAJI_FLAGS)
    sentence = Sentence.from_text("\u79c1\u306f", "s1")
    service.apply_to_sentence(sentence)
    parts = _ruby_parts(sentence)
    assert parts[1] == ["wa"]


def test_word_initial_ha_not_particle():
    service = AutoCheckService(DummyAnalyzer(), auto_check_flags=ROMAJI_FLAGS)
    sentence = Sentence.from_text("\u306f\u3058\u3081", "s1")
    service.apply_to_sentence(sentence)
    parts = _ruby_parts(sentence)
    assert parts[0] == ["ha"]


def test_wo_always_particle():
    service = AutoCheckService(DummyAnalyzer(), auto_check_flags=ROMAJI_FLAGS)
    sentence = Sentence.from_text("\u3092", "s1")
    service.apply_to_sentence(sentence)
    assert _ruby_parts(sentence) == [["o"]]


def test_particle_wa_before_hiragana():
    """\u6c49\u5b57\u524d\u7f00\u65f6 \u306f \u540e\u63a5\u5e73\u5047\u540d\u4e5f\u5e94\u5224\u5b9a\u4e3a\u52a9\u8bcd\uff08\u5982\u300c\u79c1\u306f\u304d\u308c\u3044\u300d\uff09\u3002"""
    service = AutoCheckService(DummyAnalyzer(), auto_check_flags=ROMAJI_FLAGS)
    # \u79c1 \u662f\u6c49\u5b57\uff08KANJI prev\uff09\uff0c\u65e0\u8bba\u540e\u5b57\u5982\u4f55\u90fd\u76f4\u63a5\u5224\u4e3a\u52a9\u8bcd
    sentence = Sentence.from_text("\u79c1\u306f\u304d\u308c\u3044", "s1")  # \u79c1\u306f\u304d\u308c\u3044
    service.apply_to_sentence(sentence)
    parts = _ruby_parts(sentence)
    assert parts[1] == ["wa"], f"\u306f should be 'wa' (particle), got {parts[1]}"


def test_particle_ha_after_kanji_before_kana():
    """\u6c49\u5b57+\u306f+\u5047\u540d\uff1a\u306f \u5e94\u4e3a\u52a9\u8bcd\uff08\u5982\u300c\u541b\u306f\u3084\u3055\u3057\u3044\u300d\uff09\u3002"""
    service = AutoCheckService(DummyAnalyzer(), auto_check_flags=ROMAJI_FLAGS)
    sentence = Sentence.from_text("\u541b\u306f\u3084\u3055\u3057\u3044", "s1")  # \u541b\u306f\u3084\u3055\u3057\u3044
    service.apply_to_sentence(sentence)
    parts = _ruby_parts(sentence)
    assert parts[1] == ["wa"], f"\u306f should be 'wa' (particle), got {parts[1]}"


def test_particle_ha_all_kana_is_ambiguous():
    """\u5168\u5047\u540d\u53e5\u4e2d\u300c\u5047\u540d+\u306f+\u5047\u540d\u300d\u65e0\u6cd5\u4e0e\u8bcd\u5185 \u306f \u533a\u5206\uff0c\u4fdd\u5b88\u5904\u7406\u4e3a 'ha'\u3002
    \u8fd9\u662f\u65e0\u5f62\u6001\u7d20\u5206\u6790\u65f6\u7684\u5df2\u77e5\u5c40\u9650\uff08\u5982\u300c\u304a\u306f\u306a\u3057\u300d\u4e0e\u300c\u304d\u307f\u306f\u3084\u3055\u3057\u3044\u300d\u5b57\u7b26\u7ea7\u522b\u65e0\u6cd5\u533a\u5206\uff09\u3002
    \u5b9e\u9645\u6b4c\u8bcd\u901a\u5e38\u6df7\u5199\u6c49\u5b57\uff08\u541b\u306f\u3001\u79c1\u306f\u7b49\uff09\uff0c\u6b64\u573a\u666f\u8f83\u5c11\u51fa\u73b0\u3002
    """
    service = AutoCheckService(DummyAnalyzer(), auto_check_flags=ROMAJI_FLAGS)
    sentence = Sentence.from_text("\u304d\u307f\u306f\u3084\u3055\u3057\u3044", "s1")  # \u304d\u307f\u306f\u3084\u3055\u3057\u3044
    service.apply_to_sentence(sentence)
    parts = _ruby_parts(sentence)
    # \u4fdd\u5b88\u884c\u4e3a\uff1a\u5047\u540d+\u306f+\u5047\u540d \u2192 \u4e0d\u5224\u5b9a\u4e3a\u52a9\u8bcd
    assert parts[2] == ["ha"], f"\u306f should be 'ha' (ambiguous, conservative), got {parts[2]}"


def test_particle_e_before_kanji():
    """\u3078 \u540e\u63a5\u6c49\u5b57\u65f6\u5e94\u5224\u5b9a\u4e3a\u52a9\u8bcd\uff08\u5982\u300c\u6d77\u3078\u884c\u304f\u300d\uff09\u3002"""
    service = AutoCheckService(DummyAnalyzer(), auto_check_flags=ROMAJI_FLAGS)
    sentence = Sentence.from_text("\u6d77\u3078\u884c\u304f", "s1")  # \u6d77\u3078\u884c\u304f
    service.apply_to_sentence(sentence)
    parts = _ruby_parts(sentence)
    # \u6d77 \u662f\u6c49\u5b57 prev \u2192 \u76f4\u63a5\u5224\u4e3a\u52a9\u8bcd
    assert parts[1] == ["e"], f"\u3078 should be 'e' (particle), got {parts[1]}"


def test_kanji_ha_hiragana_is_particle():
    """\u6c49\u5b57+\u306f+\u5047\u540d\uff08\u5982\u300c\u541b\u306f\u3068\u3066\u3082\u300d\uff09\uff0c\u306f \u5e94\u8bc6\u522b\u4e3a\u52a9\u8bcd\u8bfb\u4f5c wa\u3002"""
    service = AutoCheckService(DummyAnalyzer(), auto_check_flags=ROMAJI_FLAGS)
    sentence = Sentence.from_text("\u541b\u306f\u3068\u3066\u3082", "s1")  # \u541b\u306f\u3068\u3066\u3082
    service.apply_to_sentence(sentence)
    parts = _ruby_parts(sentence)
    assert parts[1] == ["wa"], f"Expected ['wa'] for \u306f after kanji, got {parts[1]}"
