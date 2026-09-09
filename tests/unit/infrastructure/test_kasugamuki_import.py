from pathlib import Path

from strange_uta_game.backend.application.project_import_service import (
    ProjectImportService,
)
from strange_uta_game.backend.infrastructure.parsers.kasugamuki_format import (
    is_kasugamuki_content,
    krl_role_names,
    sentences_from_kasugamuki,
    sentences_to_kasugamuki,
    strip_krl_config,
)
from strange_uta_game.backend.domain import Singer
from strange_uta_game.frontend.editor.timing.lyric_loader import (
    detect_lyric_format,
    parse_lyric_content,
)

SINGER_ID = "singer-1"


def test_strip_foreign_config_block_with_nested_data_and_braces_in_string():
    content = '''config {
        "style": {"font": "Noto {Sans}"},
        "groups": [{"name": "title"}]
    }

{秒|[00:11:70]びょ[00:11:81]う>[00:11:70]byo[00:11:81]u}[00:12:00]
'''

    stripped = strip_krl_config(content)

    assert stripped.startswith("{秒|")
    assert "font" not in stripped
    assert is_kasugamuki_content(content)
    assert detect_lyric_format(content) == "krl"


def test_parse_krl_pronunciation_romaji_timing_and_line_release():
    content = (
        "config {\"unused\": true}\n\n"
        "“{ア|>[00:10:64]a}{知|[00:11:03]し>[00:11:03]shi}”[00:11:65]\n"
        "{秒|[00:11:70]びょ[00:11:81]う>[00:11:70]byo[00:11:81]u}"
        "{で|>[00:12:01]de}[00:12:69]\n"
    )

    sentences = sentences_from_kasugamuki(content, SINGER_ID)

    assert [sentence.text for sentence in sentences] == ["“ア知”", "秒で"]
    assert sentences[0].characters[1].ruby is None
    assert sentences[0].characters[1].timestamps == [10640]
    assert sentences[0].characters[2].ruby.text == "し"
    assert sentences[0].characters[2].timestamps == [11030]
    assert sentences[0].characters[-1].is_sentence_end
    assert sentences[0].characters[-1].sentence_end_ts == 11650

    seconds = sentences[1].characters[0]
    assert [part.text for part in seconds.ruby.parts] == ["びょ", "う"]
    assert seconds.timestamps == [11700, 11810]
    assert sentences[1].characters[-1].sentence_end_ts == 12690


def test_parse_krl_role_tags_stripped_and_switch_singer():
    """【@角色名】 是元数据：剥离出歌词正文，未标注行沿用上一角色。"""
    content = (
        "【@miku】{そ|>[00:13:18]so}{れ|>[00:13:38]re}\n"
        "【@rin】{そ|>[01:27:59]so}\n"
        "{れ|>[01:27:77]re}\n"
    )

    assert krl_role_names(content) == ["miku", "rin"]
    sentences = sentences_from_kasugamuki(
        content, SINGER_ID, name_to_singer_id={"miku": "singer-m", "rin": "singer-r"}
    )

    assert [sentence.text for sentence in sentences] == ["それ", "そ", "れ"]
    assert [sentence.singer_id for sentence in sentences] == [
        "singer-m", "singer-r", "singer-r",
    ]
    # 无映射时标签同样剥离，不泄漏进歌词正文
    fallback = sentences_from_kasugamuki(content, SINGER_ID)
    assert [sentence.text for sentence in fallback] == ["それ", "そ", "れ"]
    assert {sentence.singer_id for sentence in fallback} == {SINGER_ID}


def test_parse_lyric_content_krl_role_tags_create_sug_singers():
    content = (
        "【@miku】{そ|>[00:13:18]so}\n"
        "【@rin】{れ|>[00:13:38]re}\n"
    )

    sentences, is_nicokara, new_singers, metadata = parse_lyric_content(
        content, SINGER_ID
    )

    assert not is_nicokara
    assert metadata["format"] == "krl"
    assert [s.name for s in new_singers] == ["miku", "rin"]
    singer_ids = {s.name: s.id for s in new_singers}
    assert [sentence.singer_id for sentence in sentences] == [
        singer_ids["miku"], singer_ids["rin"],
    ]
    assert [sentence.text for sentence in sentences] == ["そ", "れ"]


def test_parse_lyric_content_krl_role_tag_matches_existing_singer_by_name():
    content = "【@初音ミク】{そ|>[00:13:18]so}\n"
    existing = Singer(name="初音ミク", color="#45B7D1", is_default=False)

    sentences, _, new_singers, _ = parse_lyric_content(
        content, SINGER_ID, project_singers=[existing]
    )

    assert new_singers == []
    assert sentences[0].singer_id == existing.id


def test_parse_lyric_content_krl_duet_label_kept_as_single_name():
    content = "【@miku+rin】{お|>[02:46:59]o}\n"

    sentences, _, new_singers, _ = parse_lyric_content(content, SINGER_ID)

    assert [s.name for s in new_singers] == ["miku+rin"]
    assert sentences[0].singer_id == new_singers[0].id


def test_parse_lyric_content_routes_krl_before_generic_inline_parser():
    content = (
        'config {"characterProfiles": {}}\n'
        "{利|[00:12:74]り>[00:12:74]ri}[00:12:90]"
    )

    sentences, is_nicokara, singers, metadata = parse_lyric_content(
        content, SINGER_ID
    )

    assert not is_nicokara
    assert singers == []
    # KRL 自带注音与逐字时间轴 → 要求上层弹「保留原有注音」三选一
    assert metadata == {"format": "krl", "prompt_ruby_choice": True}
    assert sentences[0].text == "利"
    assert sentences[0].characters[0].ruby.text == "り"
    assert sentences[0].characters[0].timestamps == [12740]
    assert sentences[0].characters[0].sentence_end_ts == 12900


def test_parse_lyric_content_ass_meta_prompts_only_for_karaoke_or_ruby():
    r"""含 \k 卡拉OK（或注音语法）的 ASS 才要求弹三选一；普通 ASS 不弹。"""
    events_header = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    karaoke_content = (
        events_header
        + "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,"
        "{\\k50}い{\\k50}つ\n"
    )
    plain_content = (
        events_header
        + "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,いつ\n"
    )

    _, _, _, karaoke_meta = parse_lyric_content(karaoke_content, SINGER_ID)
    assert karaoke_meta.get("format") == "ass"
    assert karaoke_meta.get("prompt_ruby_choice") is True

    _, _, _, plain_meta = parse_lyric_content(plain_content, SINGER_ID)
    assert plain_meta.get("format") == "ass"
    assert "prompt_ruby_choice" not in plain_meta


def test_project_import_service_accepts_krl_extension(monkeypatch):
    content = (
        'config {"unused": {"nested": true}}\n'
        "{秒|[00:11:70]びょ[00:11:81]う>[00:11:70]byo[00:11:81]u}[00:12:00]"
    )
    monkeypatch.setattr(Path, "read_text", lambda self, encoding: content)

    sentences, metadata = ProjectImportService.load_lyrics_and_meta_from_file(
        "lyrics.krl", SINGER_ID
    )

    assert metadata == {"format": "krl"}
    assert sentences[0].text == "秒"
    assert sentences[0].characters[0].timestamps == [11700, 11810]


def test_linked_group_primary_subtitle_and_ruby_round_trip():
    source = (
        "前{明日|[00:01:00]あ[00:01:20]し[00:01:40]た>"
        "[00:01:00]a[00:01:20]shi[00:01:40]ta}後[00:02:00]"
    )

    sentences = sentences_from_kasugamuki(source, SINGER_ID)
    sentence = sentences[0]

    assert sentence.text == "前明日後"
    assert [character.char for character in sentence.characters] == [
        "前", "明", "日", "後",
    ]
    assert [character.linked_to_next for character in sentence.characters] == [
        False, True, False, False,
    ]
    assert sentence.characters[1].ruby.text == "あした"
    assert sentence.characters[2].ruby is None
    exported = sentences_to_kasugamuki(sentences)
    assert exported == (
        "前{明日|[00:01:00]あ[00:01:20]し[00:01:40]た}後[00:02:00]"
    )

    reparsed = sentences_from_kasugamuki(exported, SINGER_ID)[0]
    assert [character.char for character in reparsed.characters] == [
        "前", "明", "日", "後",
    ]
    assert [character.linked_to_next for character in reparsed.characters] == [
        False, True, False, False,
    ]
    assert reparsed.characters[1].ruby.text == "あした"


def test_primary_round_trip_preserves_empty_and_space_only_subtitle_lines():
    source = (
        "{空|[00:01:00]そら}\n"
        "\n"
        " \n"
        "{白|[00:02:00]しろ}[00:02:50]"
    )

    sentences = sentences_from_kasugamuki(source, SINGER_ID)

    assert [sentence.text for sentence in sentences] == ["空", "", " ", "白"]
    assert sentences_to_kasugamuki(sentences) == source


def test_discarded_romaji_only_group_does_not_invent_linked_word():
    sentences = sentences_from_kasugamuki(
        "{きゃ|>[00:01:00]kya}", SINGER_ID
    )

    assert [character.char for character in sentences[0].characters] == ["き", "ゃ"]
    assert not any(
        character.linked_to_next for character in sentences[0].characters
    )


def test_linked_group_line_key_up_is_restored_on_group_tail():
    source = "{明日|[00:01:00]あ[00:01:20]し[00:01:40]た}[00:02:00]"

    sentences = sentences_from_kasugamuki(source, SINGER_ID)
    characters = sentences[0].characters

    assert [character.char for character in characters] == ["明", "日"]
    assert characters[0].linked_to_next
    assert not characters[0].is_sentence_end
    assert characters[1].is_sentence_end
    assert characters[1].sentence_end_ts == 2000
    assert sentences_to_kasugamuki(sentences) == source


def _placeholder_sentence():
    from strange_uta_game.backend.domain.entities import Sentence
    from strange_uta_game.backend.domain.models import Character, Ruby, RubyPart

    return Sentence(
        singer_id=SINGER_ID,
        characters=[
            Character(
                char="寿",
                check_count=3,
                timestamps=[5000, 5150, 5300],
                ruby=Ruby(
                    parts=[
                        RubyPart(text="す"),
                        RubyPart(text="^"),
                        RubyPart(text="^"),
                    ]
                ),
                singer_id=SINGER_ID,
            )
        ],
    )


def test_placeholder_beats_stripped_and_round_trip():
    """占位拍（停顿符）导出剥离为裸时间标签，导入按位置还原。

    导出契约：停顿符是应用内部占位，不得泄漏给消费方——假名注音层
    剥离后该拍只留 `[ts]` 裸标签；导入把空段按位置还原为占位符，
    ruby parts 与 check_count / timestamps 保持对齐。
    """
    exported = sentences_to_kasugamuki([_placeholder_sentence()])
    assert exported == "{寿|[00:05:00]す[00:05:15][00:05:30]}"
    assert "^" not in exported

    reparsed = sentences_from_kasugamuki(exported, SINGER_ID)[0]
    ch = reparsed.characters[0]
    assert ch.check_count == 3
    assert [p.text for p in ch.ruby.parts] == ["す", "^", "^"]
    assert ch.timestamps == [5000, 5150, 5300]


def test_placeholder_beat_in_head_keeps_position_on_reparse():
    """占位拍在开头时按位置还原，不后移到段尾（否则读音/时间轴错位）。"""
    from strange_uta_game.backend.domain.entities import Sentence
    from strange_uta_game.backend.domain.models import Character, Ruby, RubyPart

    sentence = Sentence(
        singer_id=SINGER_ID,
        characters=[
            Character(
                char="漢",
                check_count=2,
                timestamps=[5000, 5150],
                ruby=Ruby(parts=[RubyPart(text="^"), RubyPart(text="か")]),
                singer_id=SINGER_ID,
            )
        ],
    )

    exported = sentences_to_kasugamuki([sentence])
    assert exported == "{漢|[00:05:00][00:05:15]か}"

    reparsed = sentences_from_kasugamuki(exported, SINGER_ID)[0]
    ch = reparsed.characters[0]
    assert ch.check_count == 2
    assert [p.text for p in ch.ruby.parts] == ["^", "か"]
    assert ch.timestamps == [5000, 5150]


def test_kirakara_romaji_export_strips_placeholder_beats():
    """Kirakara 双层导出：假名层占位拍留裸时间标签，罗马音层整拍跳过。"""
    from strange_uta_game.backend.infrastructure.parsers.kasugamuki_format import (
        sentences_to_kasugamuki_romaji,
    )

    exported = sentences_to_kasugamuki_romaji([_placeholder_sentence()])
    # 罗马音层的时间轴以假名层为准，"^"（罗马音化原样透传）不输出
    assert exported == "{寿|[00:05:00]す[00:05:15][00:05:30]>[00:05:00]su}"
    assert "^" not in exported


def test_all_placeholder_untimed_ruby_degrades_to_plain_char():
    """注音全是停顿占位且未打轴：退化为普通字符，不输出空注音块。"""
    from strange_uta_game.backend.domain.entities import Sentence
    from strange_uta_game.backend.domain.models import Character, Ruby, RubyPart

    sentence = Sentence(
        singer_id=SINGER_ID,
        characters=[
            Character(
                char="寿",
                check_count=2,
                ruby=Ruby(parts=[RubyPart(text="^"), RubyPart(text="^")]),
                singer_id=SINGER_ID,
            )
        ],
    )

    assert sentences_to_kasugamuki([sentence]) == "寿"
