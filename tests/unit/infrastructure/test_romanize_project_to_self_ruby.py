"""「全部转为罗马字注音」一次性操作测试。

覆盖 romanize_project_to_self_ruby：保留现有注音结构转罗马音、无 ruby 单假名补
自注音、助词/促音上下文读音、幂等性、不触碰 check_count 之外的结构。
"""

from strange_uta_game.backend.domain import (
    Character,
    Project,
    Ruby,
    RubyPart,
    Sentence,
)
from strange_uta_game.backend.infrastructure.parsers.romaji import (
    RomajiOptions,
    romanize_project_to_self_ruby,
)


def _parts(sentence: Sentence):
    return [
        [p.text for p in ch.ruby.parts] if ch.ruby else [] for ch in sentence.characters
    ]


def _project(*sentences: Sentence) -> Project:
    proj = Project(sentences=list(sentences))
    return proj


def _kanji_with_ruby(char: str, kana_parts, singer="s1") -> Character:
    return Character(
        char=char,
        ruby=Ruby(parts=[RubyPart(text=p) for p in kana_parts]),
        check_count=len(kana_parts),
        singer_id=singer,
    )


def test_existing_kana_ruby_becomes_romaji_keeping_structure():
    # 今日 → ruby きょ/う（两段）
    sent = Sentence(singer_id="s1", characters=[
        _kanji_with_ruby("今", ["きょ"]),
        _kanji_with_ruby("日", ["う"]),
    ])
    changed = romanize_project_to_self_ruby(_project(sent))
    assert _parts(sent) == [["kyo"], ["u"]]
    assert [ch.check_count for ch in sent.characters] == [1, 1]
    assert changed == 1


def test_bare_kana_gets_self_ruby_romaji():
    sent = Sentence.from_text("あい", "s1")
    assert _parts(sent) == [[], []]
    romanize_project_to_self_ruby(_project(sent))
    assert _parts(sent) == [["a"], ["i"]]
    assert [ch.check_count for ch in sent.characters] == [1, 1]


def test_particle_wa_after_kanji():
    # 私(わたし) は → は 自注音后判定为助词 → wa
    sent = Sentence(singer_id="s1", characters=[
        _kanji_with_ruby("私", ["わ", "た", "し"]),
        Character(char="は", check_count=1, singer_id="s1"),
    ])
    romanize_project_to_self_ruby(_project(sent))
    assert _parts(sent) == [["wa", "ta", "shi"], ["wa"]]


def test_word_initial_ha_not_particle():
    sent = Sentence.from_text("はじめ", "s1")
    romanize_project_to_self_ruby(_project(sent))
    assert _parts(sent)[0] == ["ha"]


def test_wo_always_particle():
    sent = Sentence.from_text("を", "s1")
    romanize_project_to_self_ruby(_project(sent))
    assert _parts(sent) == [["o"]]


def test_sokuon_cross_char_context():
    # 待(ま) + っ(自注音) + て(自注音) → ma / t / te（促音独立成拍）
    sent = Sentence(singer_id="s1", characters=[
        _kanji_with_ruby("待", ["ま"]),
        Character(char="っ", check_count=1, singer_id="s1"),
        Character(char="て", check_count=1, singer_id="s1"),
    ])
    romanize_project_to_self_ruby(_project(sent))
    assert _parts(sent) == [["ma"], ["t"], ["te"]]
    assert [ch.linked_to_next for ch in sent.characters] == [False, False, False]


def test_cross_character_digraph_links_as_part_of_romaji_conversion():
    sent = Sentence(
        singer_id="s1",
        characters=[
            _kanji_with_ruby(base, [reading])
            for base, reading in zip("キャンセル", ("き", "ゃ", "ん", "せ", "る"))
        ],
    )

    changed = romanize_project_to_self_ruby(_project(sent))

    assert _parts(sent) == [["kya"], [""], ["n"], ["se"], ["ru"]]
    assert [ch.linked_to_next for ch in sent.characters] == [
        True,
        False,
        False,
        False,
        False,
    ]
    assert changed == 1


def test_digraph_parts_on_same_character_do_not_link_next_character():
    sent = Sentence(
        singer_id="s1",
        characters=[
            _kanji_with_ruby("響", ["き", "ゃ"]),
            Character(char="く", check_count=1, singer_id="s1"),
        ],
    )

    romanize_project_to_self_ruby(_project(sent))

    assert _parts(sent) == [["kya", ""], ["ku"]]
    assert [ch.linked_to_next for ch in sent.characters] == [False, False]


def test_sokuon_keeps_own_beat_but_following_digraph_is_linked():
    sent = Sentence(
        singer_id="s1",
        characters=[
            _kanji_with_ruby(base, [reading])
            for base, reading in zip("マッチャ", ("ま", "っ", "ち", "ゃ"))
        ],
    )

    romanize_project_to_self_ruby(_project(sent))

    assert _parts(sent) == [["ma"], ["c"], ["cha"], [""]]
    assert [ch.linked_to_next for ch in sent.characters] == [
        False,
        False,
        True,
        False,
    ]


def test_long_vowel_repetition_and_link_matrix():
    def convert(options: RomajiOptions):
        sentence = Sentence.from_text("オープンカー", "s1")
        romanize_project_to_self_ruby(_project(sentence), options=options)
        return sentence

    repeated_unlinked = convert(
        RomajiOptions(repeat_long_vowels=True, link_long_vowels=False)
    )
    assert _parts(repeated_unlinked) == [
        ["o"], ["o"], ["pu"], ["n"], ["ka"], ["a"]
    ]
    assert not any(ch.linked_to_next for ch in repeated_unlinked.characters)

    repeated_linked = convert(
        RomajiOptions(repeat_long_vowels=True, link_long_vowels=True)
    )
    assert _parts(repeated_linked) == [
        ["o"], ["o"], ["pu"], ["n"], ["ka"], ["a"]
    ]
    assert [ch.linked_to_next for ch in repeated_linked.characters] == [
        True, False, False, False, True, False,
    ]

    hyphenated = convert(
        RomajiOptions(repeat_long_vowels=False, link_long_vowels=False)
    )
    assert _parts(hyphenated) == [
        ["o-"], [""], ["pu"], ["n"], ["ka-"], [""]
    ]
    assert [ch.linked_to_next for ch in hyphenated.characters] == [
        True, False, False, False, True, False,
    ]


def test_long_vowel_link_extends_existing_digraph_chain():
    sent = Sentence.from_text("キャー", "s1")
    options = RomajiOptions(repeat_long_vowels=True, link_long_vowels=True)

    romanize_project_to_self_ruby(_project(sent), options=options)

    assert _parts(sent) == [["kya"], [""], ["a"]]
    assert [ch.linked_to_next for ch in sent.characters] == [True, True, False]


def test_sokuon_link_only_applies_between_literal_kana_characters():
    kana_sentence = Sentence.from_text("まって", "s1")
    options = RomajiOptions(link_sokuon=True)
    romanize_project_to_self_ruby(_project(kana_sentence), options=options)
    assert _parts(kana_sentence) == [["ma"], ["t"], ["te"]]
    assert [ch.linked_to_next for ch in kana_sentence.characters] == [
        False, True, False,
    ]

    kanji_sentence = Sentence(
        singer_id="s1",
        characters=[
            _kanji_with_ruby("一", ["い", "っ"]),
            _kanji_with_ruby("体", ["た", "い"]),
        ],
    )
    romanize_project_to_self_ruby(_project(kanji_sentence), options=options)
    assert _parts(kanji_sentence) == [["i", "t"], ["ta", "i"]]
    assert not any(ch.linked_to_next for ch in kanji_sentence.characters)

    mixed_sentence = Sentence(
        singer_id="s1",
        characters=[
            _kanji_with_ruby("真", ["ま"]),
            Character(char="っ", check_count=1, singer_id="s1"),
            _kanji_with_ruby("直", ["す", "ぐ"]),
        ],
    )
    romanize_project_to_self_ruby(_project(mixed_sentence), options=options)
    assert _parts(mixed_sentence) == [["ma"], ["s"], ["su", "gu"]]
    assert not any(ch.linked_to_next for ch in mixed_sentence.characters)


def test_small_kana_link_only_applies_between_literal_kana_characters():
    options = RomajiOptions(link_long_vowels=True)

    kana_sentence = Sentence.from_text("あぁアァ", "s1")
    romanize_project_to_self_ruby(_project(kana_sentence), options=options)
    assert _parts(kana_sentence) == [["a"], ["a"], ["a"], ["a"]]
    assert [ch.linked_to_next for ch in kana_sentence.characters] == [
        True, False, True, False,
    ]

    kanji_sentence = Sentence(
        singer_id="s1",
        characters=[
            _kanji_with_ruby("亜", ["あ"]),
            Character(char="ぁ", check_count=1, singer_id="s1"),
        ],
    )
    romanize_project_to_self_ruby(_project(kanji_sentence), options=options)
    assert _parts(kanji_sentence) == [["a"], ["a"]]
    assert not any(ch.linked_to_next for ch in kanji_sentence.characters)

    sokuon_sentence = Sentence.from_text("まって", "s1")
    romanize_project_to_self_ruby(_project(sokuon_sentence), options=options)
    assert not any(ch.linked_to_next for ch in sokuon_sentence.characters)


def test_small_kana_link_follows_forced_long_link_when_repetition_is_off():
    sentence = Sentence.from_text("あぁ", "s1")
    options = RomajiOptions(
        repeat_long_vowels=False,
        link_long_vowels=False,
    )

    romanize_project_to_self_ruby(_project(sentence), options=options)

    # 小假名不参与长音复写，仅与 ``ー`` 共用链接策略。
    assert _parts(sentence) == [["a"], ["a"]]
    assert [ch.linked_to_next for ch in sentence.characters] == [True, False]


def test_uppercase_project_conversion():
    sent = Sentence.from_text("わたし", "s1")
    romanize_project_to_self_ruby(
        _project(sent), options=RomajiOptions(uppercase=True)
    )
    assert _parts(sent) == [["WA"], ["TA"], ["SHI"]]


def test_idempotent_second_run_no_change():
    sent = Sentence.from_text("あい", "s1")
    proj = _project(sent)
    romanize_project_to_self_ruby(proj)
    first = _parts(sent)
    changed = romanize_project_to_self_ruby(proj)
    assert _parts(sent) == first
    assert changed == 0


def test_no_kana_no_ruby_returns_zero():
    # 纯汉字无 ruby → 不创建自注音、不变化
    sent = Sentence(singer_id="s1", characters=[
        Character(char="水", check_count=0, singer_id="s1"),
    ])
    changed = romanize_project_to_self_ruby(_project(sent))
    assert changed == 0
    assert sent.characters[0].ruby is None


def test_katakana_self_ruby():
    sent = Sentence.from_text("カナ", "s1")
    romanize_project_to_self_ruby(_project(sent))
    assert _parts(sent) == [["ka"], ["na"]]


def test_halfwidth_voiced_mora_owns_the_reading_and_keeps_original_checkpoints():
    sentence = Sentence.from_text("ｷﾞｬ(ﾟ∀ﾟ)", "s1")
    sentence.characters[1].add_timestamp(200)
    for index in (4, 6):
        sentence.characters[index].set_check_count(0)
    counts = [c.check_count for c in sentence.characters]
    project = _project(sentence)

    assert romanize_project_to_self_ruby(project) == 1

    assert sentence.text == "ｷﾞｬ(ﾟ∀ﾟ)"
    assert _parts(sentence) == [["gya"], [], [""], [], [], [], [], []]
    assert [c.linked_to_next for c in sentence.characters] == [
        True, True, False, False, False, False, False, False,
    ]
    assert [c.check_count for c in sentence.characters] == counts
    assert sentence.characters[1].timestamps == [200]
    assert romanize_project_to_self_ruby(project) == 0


def test_halfwidth_kana_do_not_combine_across_unannotated_symbols():
    sentence = Sentence.from_text("ｷ(ｬ)ｶ(ﾞ)", "s1")

    romanize_project_to_self_ruby(_project(sentence))

    assert _parts(sentence) == [["ki"], [], ["ya"], [], ["ka"], [], [], []]
    assert not any(c.linked_to_next for c in sentence.characters)


def test_halfwidth_kana_with_precomposed_ruby_does_not_repeat_small_kana():
    sentence = Sentence.from_text("ｷﾞｬ", "s1")
    sentence.characters[0].set_ruby(Ruby(parts=[RubyPart(text="ぎゃ")]))

    romanize_project_to_self_ruby(_project(sentence))

    assert _parts(sentence) == [["gya"], [], [""]]
    assert [c.linked_to_next for c in sentence.characters] == [True, True, False]


def test_halfwidth_long_vowel_with_precomposed_ruby_is_repeated_once():
    sentence = Sentence.from_text("ﾋﾟｰ", "s1")
    sentence.characters[0].set_ruby(Ruby(parts=[RubyPart(text="ぴー")]))

    romanize_project_to_self_ruby(_project(sentence))

    assert _parts(sentence) == [["pi"], [], ["i"]]


def test_unannotated_halfwidth_mark_does_not_change_custom_ruby():
    sentence = Sentence.from_text("ｶﾞ", "s1")
    sentence.characters[0].set_ruby(Ruby(parts=[RubyPart(text="かた")]))

    romanize_project_to_self_ruby(_project(sentence))

    assert _parts(sentence) == [["kata"], []]
    assert not any(c.linked_to_next for c in sentence.characters)
