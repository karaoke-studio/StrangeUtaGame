import pytest

from strange_uta_game.backend.domain import Character, Ruby, RubyPart, Sentence
from strange_uta_game.backend.infrastructure.parsers.kasugamuki_format import (
    sentence_to_kasugamuki,
    sentence_to_kasugamuki_romaji,
)
from strange_uta_game.backend.infrastructure.parsers.romaji import RomajiOptions


def test_export_uses_editor_sentence_level_romaji_context_without_mutating_source():
    sentence = Sentence(
        singer_id="s1",
        characters=[
            Character(
                char="待",
                ruby=Ruby(parts=[RubyPart(text="ま")]),
                check_count=1,
                singer_id="s1",
            ),
            Character(char="っ", check_count=1, singer_id="s1"),
            Character(char="て", check_count=1, singer_id="s1"),
        ],
    )

    result = sentence_to_kasugamuki_romaji(sentence)

    # Sokuon keeps its own block with its consonant beat; no invented merge
    # with the following kana.
    assert result == "{待|ま>ma}{っ|>t}{て|>te}"
    assert sentence.characters[0].ruby.parts[0].text == "ま"
    assert sentence.characters[1].ruby is None
    assert sentence.characters[2].ruby is None


def test_export_uses_editor_particle_detection_for_bare_kana():
    sentence = Sentence(
        singer_id="s1",
        characters=[
            Character(
                char="私",
                ruby=Ruby(parts=[RubyPart(text="わ"), RubyPart(text="た"), RubyPart(text="し")]),
                check_count=3,
                singer_id="s1",
            ),
            Character(char="は", check_count=1, singer_id="s1"),
        ],
    )

    assert sentence_to_kasugamuki_romaji(sentence) == (
        "{私|わたし>watashi}{は|>wa}"
    )


def test_export_keeps_digraph_in_one_romaji_annotation():
    sentence = Sentence(
        singer_id="s1",
        characters=[
            Character(char="き", check_count=1, singer_id="s1"),
            Character(char="ゃ", check_count=1, singer_id="s1"),
        ],
    )

    assert sentence_to_kasugamuki_romaji(sentence) == "{きゃ|>kya}"


def test_halfwidth_kana_export_uses_original_glyphs_and_romaji_options():
    sentence = Sentence.from_text("ｷﾞｬﾏｯﾁｬﾋﾟｰ(ﾟ∀ﾟ)", "s1")
    options = RomajiOptions(
        repeat_long_vowels=False, link_sokuon=True, uppercase=True
    )

    assert sentence_to_kasugamuki_romaji(sentence, options=options) == (
        "{ｷﾞｬ|>GYA}{ﾏ|>MA}{ｯﾁｬ|>CCHA}{ﾋﾟｰ|>PI-}(ﾟ∀ﾟ)"
    )
    assert not any(ch.ruby or ch.linked_to_next for ch in sentence.characters)


@pytest.mark.parametrize("prelinked", [False, True])
def test_halfwidth_kana_export_keeps_independent_timestamp_on_absorbed_character(
    prelinked,
):
    sentence = Sentence.from_text("ｷﾞｬ", "s1")
    for index, character in enumerate(sentence.characters, start=1):
        character.add_timestamp(index * 100)
    for character in sentence.characters[:-1]:
        character.linked_to_next = prelinked

    assert sentence_to_kasugamuki_romaji(sentence) == (
        "{ｷ|>[00:00:10]gya}[00:00:20]ﾞ[00:00:30]ｬ"
    )
    assert [ch.linked_to_next for ch in sentence.characters] == [
        prelinked, prelinked, False,
    ]


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (
            RomajiOptions(
                repeat_long_vowels=True,
                link_long_vowels=False,
            ),
            "{オ|>o}{ー|>o}{プ|>pu}{ン|>n}{カ|>ka}{ー|>a}",
        ),
        (
            RomajiOptions(
                repeat_long_vowels=True,
                link_long_vowels=True,
            ),
            "{オー|>oo}{プ|>pu}{ン|>n}{カー|>kaa}",
        ),
        (
            RomajiOptions(
                repeat_long_vowels=False,
                link_long_vowels=False,
            ),
            "{オー|>o-}{プ|>pu}{ン|>n}{カー|>ka-}",
        ),
    ],
)
def test_export_applies_long_vowel_options(options, expected):
    sentence = Sentence.from_text("オープンカー", "s1")

    assert sentence_to_kasugamuki_romaji(sentence, options=options) == expected


def test_export_long_vowel_link_also_groups_small_kana():
    sentence = Sentence.from_text("あぁ", "s1")

    assert sentence_to_kasugamuki_romaji(
        sentence,
        options=RomajiOptions(link_long_vowels=True),
    ) == "{あぁ|>aa}"


def test_export_applies_sokuon_link_and_uppercase_options():
    sentence = Sentence.from_text("まって", "s1")

    assert sentence_to_kasugamuki_romaji(
        sentence,
        options=RomajiOptions(link_sokuon=True, uppercase=True),
    ) == "{ま|>MA}{って|>TTE}"


def test_export_groups_per_character_kana_ruby_digraph_without_mutating_source():
    sentence = Sentence(
        singer_id="s1",
        characters=[
            Character(
                char=base,
                ruby=Ruby(parts=[RubyPart(text=reading)]),
                check_count=1,
                singer_id="s1",
            )
            for base, reading in zip("キャンセル", ("き", "ゃ", "ん", "せ", "る"))
        ],
    )

    assert sentence_to_kasugamuki_romaji(sentence) == (
        "{キャ|きゃ>kya}{ン|ん>n}{セ|せ>se}{ル|る>ru}"
    )
    assert [ch.ruby.text for ch in sentence.characters if ch.ruby] == [
        "き",
        "ゃ",
        "ん",
        "せ",
        "る",
    ]
    assert [ch.linked_to_next for ch in sentence.characters] == [
        False,
        False,
        False,
        False,
        False,
    ]


def test_sokuon_keeps_own_block_and_own_timestamp():
    """促音独立成块，用自身时间戳，不并入后续假名、不借用其时间戳。"""
    sentence = Sentence(
        singer_id="s1",
        characters=[
            Character(
                char="っ", check_count=1, timestamps=[100], singer_id="s1"
            ),
            Character(
                char="て", check_count=1, timestamps=[200], singer_id="s1"
            ),
        ],
    )

    assert (
        sentence_to_kasugamuki_romaji(sentence)
        == "{っ|>[00:00:10]t}{て|>[00:00:20]te}"
    )


def test_katakana_sokuon_keeps_own_block_before_digraph():
    sentence = Sentence(
        singer_id="s1",
        characters=[
            Character(char="マ", check_count=1, singer_id="s1"),
            Character(char="ッ", check_count=1, singer_id="s1"),
            Character(char="チ", check_count=1, singer_id="s1"),
            Character(char="ャ", check_count=1, singer_id="s1"),
        ],
    )

    assert (
        sentence_to_kasugamuki_romaji(sentence)
        == "{マ|>ma}{ッ|>c}{チャ|>cha}"
    )


def test_linked_word_is_wrapped_as_one_kasugamuki_block():
    sentence = Sentence(
        singer_id="s1",
        characters=[
            Character(
                char="明",
                ruby=Ruby(
                    parts=[
                        RubyPart(text="あ"),
                        RubyPart(text="し"),
                        RubyPart(text="た"),
                    ]
                ),
                check_count=3,
                singer_id="s1",
                linked_to_next=True,
            ),
            Character(char="日", check_count=0, singer_id="s1"),
        ],
    )

    assert sentence_to_kasugamuki(sentence) == "{明日|あした}"
    assert sentence_to_kasugamuki_romaji(sentence) == "{明日|あした>ashita}"


def test_linked_words_without_ruby_are_not_wrapped_in_export_blocks():
    sentence = Sentence.from_text("Over the rainbow", "s1")
    for start, end in ((1, 4), (6, 8), (10, 17)):
        for index in range(start, end - 1):
            sentence.characters[index].linked_to_next = True

    assert sentence_to_kasugamuki(sentence) == "Over the rainbow"
    assert sentence_to_kasugamuki_romaji(sentence) == "Over the rainbow"


def test_plain_linked_group_keeps_each_character_timestamp():
    sentence = Sentence.from_text("abc", "s1")
    for index, char in enumerate(sentence.characters):
        char.timestamps = [100 + index * 100]
    sentence.characters[0].linked_to_next = True
    sentence.characters[1].linked_to_next = True

    expected = "[00:00:10]a[00:00:20]b[00:00:30]c"
    assert sentence_to_kasugamuki(sentence) == expected
    assert sentence_to_kasugamuki_romaji(sentence) == expected
