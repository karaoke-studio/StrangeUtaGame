"""TTML / AMLL TTML 解析器与导出器回归测试。

覆盖：
* TTML 时间字符串；
* 自建 TTML（逐词 span / 多字 span / ruby / 双演唱者 / 句中停顿 / 连词）；
* 真实 AMLL TTML DB 文件（``ncm-lyrics/101126.ttml``）；
* export → import → export 的模型级签名回环与文本幂等；
* 新格式只追加注册，不改变既有导出格式。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from xml.etree import ElementTree as ET

import pytest

from strange_uta_game.backend.domain import (
    Project,
    Ruby,
    RubyPart,
    Sentence,
    Singer,
)
from strange_uta_game.backend.infrastructure.exporters import (
    ExportError,
    get_all_exporters,
    get_exporter_by_extension,
    get_exporter_by_name,
)
from strange_uta_game.backend.infrastructure.exporters.ttml_exporter import (
    TTMLExporter,
)
from strange_uta_game.backend.infrastructure.parsers.ttml_parser import (
    AMLL_NS,
    ITUNES_NS,
    ROLE_SUG_CHECKPOINT,
    ROLE_SUG_EMPTY,
    ROLE_SUG_LINK,
    ROLE_SUG_NO_CP,
    ROLE_SUG_RELEASE,
    TTML_NS,
    TTM_NS,
    TTMLParser,
    build_aux_singer_specs,
    build_ttml_sentences,
    detect_time_style,
    format_time,
    format_time_style,
    is_ttml_content,
    parse_time,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ttml"
SUG_FIXTURE = FIXTURES / "sug_roundtrip.ttml"
AMLL_FIXTURE = FIXTURES / "amll_101126.ttml"
SECTION_FIXTURES = [
    FIXTURES / "amll_ncm_2020905278.ttml",
    FIXTURES / "amll_qq_002dB4xY3S3gCr.ttml",
    FIXTURES / "amll_am_1019650261.ttml",
    FIXTURES / "amll_spotify_6nFn8VpgIi1KJbowtM8JRs.ttml",
    FIXTURES / "amll_raw_1763204456985-50747104-2f658529.ttml",
]

_SINGER_COLORS = ["#FF6B6B", "#4ECDC4", "#45B7D1", "#FFA07A"]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _build_project(document) -> Project:
    """按导入层同样的规则把 TTMLDocument 变成 Project（不依赖 Qt）。"""
    project = Project()
    default = project.singers[0]
    name_to_id: Dict[str, str] = {}

    for index, (_agent_id, agent) in enumerate(document.agents.items()):
        name = agent.name or agent.id
        if index == 0:
            default.rename(name)
            default.ttml_agent_id = agent.id
            default.ttml_agent_type = agent.type
            default.ttml_agent_named = agent.named
            default.ttml_agent_display_name = agent.name
            default.ttml_agent_source_name = agent.source_name
            name_to_id[name] = default.id
        else:
            singer = Singer(
                name=name,
                color=_SINGER_COLORS[index % len(_SINGER_COLORS)],
                is_default=False,
                ttml_agent_id=agent.id,
                ttml_agent_type=agent.type,
                ttml_agent_named=agent.named,
                ttml_agent_display_name=agent.name,
                ttml_agent_source_name=agent.source_name,
            )
            project.add_singer(singer)
            name_to_id[name] = singer.id

    aux_specs, aux_name_by_key = build_aux_singer_specs(document)
    for offset, (name, role) in enumerate(aux_specs):
        if name in name_to_id:
            continue
        singer = Singer(
            name=name,
            color=_SINGER_COLORS[
                (len(document.agents) + offset) % len(_SINGER_COLORS)
            ],
            is_default=False,
            ttml_role=role,
        )
        project.add_singer(singer)
        name_to_id[name] = singer.id

    project.sentences = build_ttml_sentences(
        document,
        default.id,
        name_to_id,
        aux_name_by_key=aux_name_by_key,
    )
    if project.metadata is not None:
        project.metadata.title = document.title
        project.metadata.artist = document.artist
        project.metadata.album = document.album
        project.metadata.language = document.language or "ja"
        project.metadata.extra.update(document.extra_meta())
    return project


def _singer_name(project: Project, singer_id: str) -> str:
    for singer in project.singers:
        if singer.id == singer_id:
            return singer.name
    return ""


def _signature(project: Project) -> Dict[str, Any]:
    sentences: List[Dict[str, Any]] = []
    for sentence in project.sentences:
        chars = []
        for char in sentence.characters:
            chars.append(
                {
                    "char": char.char,
                    "timestamps": list(char.timestamps),
                    "global": list(char.global_timestamps),
                    "end_ts": char.sentence_end_ts,
                    "is_end": char.is_sentence_end,
                    "is_line_end": char.is_line_end,
                    "linked": char.linked_to_next,
                    "check_count": char.check_count,
                    "ruby": (
                        [part.text for part in char.ruby.parts]
                        if char.ruby
                        else []
                    ),
                    "singer": _singer_name(
                        project, char.singer_id or sentence.singer_id
                    ),
                }
            )
        # 副轴行按“文本 + role + 时间范围”比较：导入/导出会把和声词之间的
        # tail 空格并到相邻词 span，内部字符切分不必逐字等同，但文本、
        # 角色、时间轴必须一致。
        chars_payload: List[Dict[str, Any]] = (
            [] if sentence.aux_role else chars
        )
        sentences.append(
            {
                "text": sentence.text,
                "singer": _singer_name(project, sentence.singer_id),
                "aux_role": sentence.aux_role,
                "aux_lang": sentence.aux_lang,
                "aux_agent": sentence.aux_agent,
                "aux_begin": sentence.aux_begin,
                "aux_end": sentence.aux_end,
                "song_part": sentence.song_part,
                "chars": chars_payload,
            }
        )

    metadata = project.metadata
    return {
        "title": metadata.title if metadata else "",
        "artist": metadata.artist if metadata else "",
        "album": metadata.album if metadata else "",
        "language": metadata.language if metadata else "",
        "extra": {
            key: value
            for key, value in (dict(metadata.extra) if metadata else {}).items()
            if not str(key).startswith("_ttml")
        },
        "singers": [singer.name for singer in project.singers],
        "sentences": sentences,
    }


def _agent_signature(project: Project) -> List[Dict[str, Any]]:
    return [
        {
            "name": singer.name,
            "agent_id": singer.ttml_agent_id,
            "agent_type": singer.ttml_agent_type,
            "agent_named": singer.ttml_agent_named,
            "agent_source_name": singer.ttml_agent_source_name,
            "agent_display_name": singer.ttml_agent_display_name,
            "ttml_role": singer.ttml_role,
        }
        for singer in project.singers
    ]


def _roundtrip(content: str):
    first_doc = TTMLParser().parse_document(content)
    first_project = _build_project(first_doc)
    first_xml = TTMLExporter().export_to_text(first_project)

    second_doc = TTMLParser().parse_document(first_xml)
    second_project = _build_project(second_doc)
    second_xml = TTMLExporter().export_to_text(second_project)
    return first_doc, first_project, first_xml, second_project, second_xml


class TestTtmlTime:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("45", 45_000),
            ("12.5", 12_500),
            ("01:30", 90_000),
            ("2:45.500", 165_500),
            ("01:02:03", 3_723_000),
            ("00:00:00.000", 0),
            ("4:13.781", 253_781),
            ("15.8s", 15_800),
            ("90s", 90_000),
        ],
    )
    def test_parse_time(self, raw, expected):
        assert parse_time(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["", "abc", "1:2:3:4", "-1", "1:xx", "01:75.000", "1:75:00", "9" * 400],
    )
    def test_parse_time_rejects_invalid(self, raw):
        assert parse_time(raw) is None

    def test_format_time(self):
        assert format_time(0) == "00:00:00.000"
        assert format_time(3_723_456) == "01:02:03.456"


class TestTtmlDetection:
    def test_detects_ttml_and_amll(self):
        assert is_ttml_content(_read(SUG_FIXTURE))
        assert is_ttml_content(_read(AMLL_FIXTURE))
        assert is_ttml_content('<?xml version="1.0"?><tt xmlns="http://www.w3.org/ns/ttml"/>')

    def test_rejects_plain_text(self):
        assert not is_ttml_content("ただの歌詞\n二行目")
        assert not is_ttml_content("")
        assert not is_ttml_content("普通文本里出现 <tt> 标签不算 TTML")
        assert not is_ttml_content("<!-- comment --> just text <tt>")

    def test_rejects_doctype(self):
        with pytest.raises(Exception):
            TTMLParser().parse_document(
                '<!DOCTYPE tt [<!ENTITY x "boom">]><tt>&x;</tt>'
            )


class TestSugTtmlRoundtrip:
    @pytest.fixture(scope="class")
    def result(self):
        return _roundtrip(_read(SUG_FIXTURE))

    def test_document_shape(self, result):
        document = result[0]
        assert document.title == "SUG TTML Roundtrip"
        assert document.artist == "初音ミク / 鏡音リン"
        assert document.album == "SUG TTML Fixtures"
        assert document.language == "ja"
        assert len(document.lines) == 5
        assert {agent.name for agent in document.agents.values()} == {
            "初音ミク",
            "鏡音リン",
        }
        # 翻译等辅助轨不再被忽略，而是挂在对应行上。
        line2 = document.lines[1]
        assert [track.role for track in line2.aux_tracks] == ["x-translation"]
        assert line2.aux_tracks[0].text == "向着光"
        assert document.warnings == []

    def test_ruby_and_roles_are_preserved(self, result):
        project = result[1]
        main_lines = [s for s in project.sentences if not s.aux_role]
        line2 = main_lines[1]
        assert line2.text == "光へ"
        light = line2.characters[0]
        assert light.check_count == 3
        assert [part.text for part in light.ruby.parts] == ["ひ", "か", "り"]
        assert light.timestamps == [3500, 3800, 4100]
        assert light.is_fully_timed

        line5 = main_lines[4]
        assert line5.characters[0].linked_to_next is True
        assert line5.characters[1].is_sentence_end is True
        assert line5.characters[1].sentence_end_ts == 9000

    def test_model_signature_roundtrips(self, result):
        _, first_project, _, second_project, _ = result
        assert _signature(first_project) == _signature(second_project)

    def test_second_export_is_stable(self, result):
        _, _, first_xml, _, second_xml = result
        assert first_xml == second_xml

    def test_translation_is_a_separate_role_line(self, result):
        project = result[1]
        main_lines = [s for s in project.sentences if not s.aux_role]
        aux_lines = [s for s in project.sentences if s.aux_role]
        assert "向着光" not in "".join(s.text for s in main_lines)
        translations = [
            s for s in aux_lines if s.aux_role == "x-translation"
        ]
        assert translations and translations[0].text == "向着光"
        assert translations[0].aux_lang == "zh-CN"
        # 翻译行紧跟主行，并保留父行关联。
        parent_index = next(
            i
            for i, s in enumerate(project.sentences)
            if s.id == translations[0].aux_parent_id
        )
        assert project.sentences[parent_index + 1] is translations[0]


class TestAmllTtmlRoundtrip:
    @pytest.fixture(scope="class")
    def result(self):
        return _roundtrip(_read(AMLL_FIXTURE))

    def test_real_amll_header(self, result):
        document = result[0]
        assert document.title == "第三十八年夏至"
        assert document.artist == "河图"
        assert document.album == "第三十八年夏至"
        assert len(document.lines) == 50
        assert document.warnings == []
        assert {agent.name for agent in document.agents.values()} == {
            "河图 (v1)",
            "河图 (v2)",
        }
        # 无名 agent 的显示名只是占位；导出时不应写成 ttm:name。
        assert all(not agent.named for agent in document.agents.values())

    def test_first_line_timing(self, result):
        line = result[1].sentences[0]
        assert line.text == "衰草连横向晚晴"
        assert [char.timestamps[0] for char in line.characters] == [
            2739,
            3202,
            3689,
            5150,
            5572,
            6778,
            7581,
        ]
        assert line.characters[-1].sentence_end_ts == 10_064

    def test_model_signature_roundtrips(self, result):
        _, first_project, _, second_project, _ = result
        assert _signature(first_project) == _signature(second_project)

    def test_second_export_is_stable(self, result):
        _, _, first_xml, _, second_xml = result
        assert first_xml == second_xml

    def test_platform_ids_are_written_back(self, result):
        first_project = result[1]
        first_xml = result[2]
        assert first_project.metadata.extra["ncmMusicId"] == "101126"
        assert 'key="ncmMusicId"' in first_xml
        assert 'value="101126"' in first_xml


class TestAmllSectionFixtures:
    """AMLL TTML DB 各平台分区真实样本的辅助轨/agent 回环。"""

    @pytest.fixture(scope="class", params=SECTION_FIXTURES, ids=lambda p: p.name)
    def result(self, request):
        return request.param, _roundtrip(_read(request.param))

    def test_fixture_has_aux_or_named_agents(self, result):
        path, parsed = result
        document = parsed[0]
        assert document.lines
        # 五个分区样本至少覆盖一种辅助轨或具名 agent，避免样本退化后
        # 测试仍“通过但什么都没测”。
        has_aux = any(line.aux_tracks for line in document.lines)
        has_named = any(agent.named for agent in document.agents.values())
        assert has_aux or has_named, path.name

    def test_model_signature_roundtrips(self, result):
        path, (_doc, first_project, _first_xml, second_project, _second_xml) = result
        assert _signature(first_project) == _signature(second_project), path.name
        assert _agent_signature(first_project) == _agent_signature(
            second_project
        ), path.name

    def test_warnings_do_not_mention_ignored_aux(self, result):
        _path, (document, *_rest) = result
        joined = " ".join(document.warnings)
        assert "已忽略" not in joined
        assert "x-roman" not in joined
        assert "x-bg" not in joined

    def test_export_is_stable(self, result):
        _path, (_doc, _p1, first_xml, _p2, second_xml) = result
        assert first_xml == second_xml


def test_unknown_aux_role_is_preserved():
    content = (
        '<tt xmlns="http://www.w3.org/ns/ttml" '
        'xmlns:ttm="http://www.w3.org/ns/ttml#metadata">'
        '<body><p begin="1.000" end="2.000">'
        '<span begin="1.000" end="2.000">主</span>'
        '<span ttm:role="x-artist-note">注释</span>'
        "</p></body></tt>"
    )
    document = TTMLParser().parse_document(content)
    assert document.warnings == []
    assert document.lines[0].aux_tracks[0].role == "x-artist-note"
    assert document.lines[0].aux_tracks[0].text == "注释"

    project = _build_project(document)
    xml = TTMLExporter().export_to_text(project)
    assert 'ttm:role="x-artist-note"' in xml
    assert "注释" in xml


def test_song_part_is_preserved_on_sentences_and_divs():
    content = (
        '<tt xmlns="http://www.w3.org/ns/ttml" '
        'xmlns:ttm="http://www.w3.org/ns/ttml#metadata" '
        'xmlns:itunes="http://music.apple.com/lyric-ttml-internal">'
        "<body>"
        '<div itunes:songPart="Verse">'
        '<p begin="1.000" end="2.000"><span begin="1.000" end="2.000">甲</span>'
        '<span ttm:role="x-translation" xml:lang="zh-CN">译</span></p>'
        "</div>"
        '<div itunes:song-part="Chorus">'
        '<p begin="3.000" end="4.000"><span begin="3.000" end="4.000">乙</span></p>'
        "</div>"
        "</body></tt>"
    )
    document = TTMLParser().parse_document(content)
    assert [line.song_part for line in document.lines] == ["Verse", "Chorus"]
    project = _build_project(document)
    mains = [s for s in project.sentences if not s.aux_role]
    aux = [s for s in project.sentences if s.aux_role]
    assert [s.song_part for s in mains] == ["Verse", "Chorus"]
    assert [s.song_part for s in aux] == ["Verse"]

    xml = TTMLExporter().export_to_text(project)
    root = ET.fromstring(xml)
    divs = [
        elem
        for elem in root.iter(f"{{{TTML_NS}}}div")
        if elem.get(f"{{{ITUNES_NS}}}song-part")
    ]
    assert [div.get(f"{{{ITUNES_NS}}}song-part") for div in divs] == [
        "Verse",
        "Chorus",
    ]
    rebuilt = _build_project(TTMLParser().parse_document(xml))
    assert [s.song_part for s in rebuilt.sentences if not s.aux_role] == [
        "Verse",
        "Chorus",
    ]
    assert [s.song_part for s in rebuilt.sentences if s.aux_role] == ["Verse"]


def test_time_style_detection_and_formatting():
    assert detect_time_style(["3:14.734", "3:15.000", "03:16.000"]) == {
        "kind": "minutes",
        "precision": 3,
        "pad_first": False,
    }
    assert format_time_style(
        194_734, {"kind": "minutes", "precision": 3, "pad_first": False}
    ) == "3:14.734"
    assert format_time_style(
        194_734, {"kind": "hms", "precision": 3, "pad_first": True}
    ) == "00:03:14.734"
    assert format_time_style(
        194_734, {"kind": "seconds_suffix", "precision": 2, "pad_first": True}
    ) == "194.73s"


def test_source_time_style_is_used_for_export():
    content = (
        '<tt xmlns="http://www.w3.org/ns/ttml"><body>'
        '<p begin="3:14.734" end="3:15.734">'
        '<span begin="3:14.734" end="3:15.734">あ</span>'
        "</p></body></tt>"
    )
    document = TTMLParser().parse_document(content)
    project = _build_project(document)
    xml = TTMLExporter().export_to_text(project)
    assert 'begin="3:14.734"' in xml
    assert 'begin="00:03:14.734"' not in xml
    assert "_ttml" not in xml


def test_low_precision_time_style_does_not_emit_zero_length_spans():
    content = (
        '<tt xmlns="http://www.w3.org/ns/ttml"><body>'
        '<p begin="0:31.5" end="0:31.5">'
        '<span begin="0:31.5" end="0:31.5">あ</span>'
        "</p></body></tt>"
    )
    document = TTMLParser().parse_document(content)
    assert document.time_style["precision"] == 1
    project = _build_project(document)
    xml = TTMLExporter().export_to_text(project)
    root = ET.fromstring(xml)
    max_end = 0
    for elem in root.iter():
        begin = elem.get("begin")
        end = elem.get("end")
        if begin is not None and end is not None:
            assert begin != end, ET.tostring(elem, encoding="unicode")
            max_end = max(max_end, parse_time(end) or 0)
    body = next(elem for elem in root.iter(f"{{{TTML_NS}}}body"))
    assert parse_time(body.get("dur")) >= max_end


def test_dropped_features_are_reported():
    content = (
        '<tt xmlns="http://www.w3.org/ns/ttml" '
        'xmlns:ttm="http://www.w3.org/ns/ttml#metadata" '
        'xmlns:tts="http://www.w3.org/ns/ttml#styling" '
        'xmlns:itunes="http://music.apple.com/lyric-ttml-internal" '
        'xmlns:amll="http://www.example.com/ns/amll">'
        "<head><metadata>"
        "<itunes:iTunesMetadata><itunes:translations/></itunes:iTunesMetadata>"
        '<amll:meta key="obscene" value="true"/>'
        "</metadata></head><body>"
        '<div><p begin="1.000" end="2.000" tts:fontFamily="Serif">'
        '<span begin="1.000" end="2.000">あ</span>'
        "</p></div></body></tt>"
    )
    document = TTMLParser().parse_document(content)
    joined = " ".join(document.dropped_features)
    assert "tts 样式" in joined
    assert "Apple Music 头部元数据" in joined
    assert "obscene" in joined


def test_non_ttml_export_skips_auxiliary_lines(tmp_path):
    from strange_uta_game.backend.application.export_service import ExportService

    project = Project()
    singer = project.singers[0]
    main = Sentence.from_text("あ", singer.id)
    main.characters[0].add_timestamp(1000)
    main.characters[0].is_sentence_end = True
    main.characters[0].set_sentence_end_ts(1500)
    main.characters[0].set_offset(0)
    project.add_sentence(main)

    translator = Singer(
        name="翻译", color="#4ECDC4", ttml_role="x-translation"
    )
    project.add_singer(translator)
    aux = Sentence.from_text("译文", translator.id)
    aux.aux_role = "x-translation"
    aux.aux_parent_id = main.id
    project.add_sentence(aux)

    path = tmp_path / "out.lrc"
    result = ExportService().export(project, "LRC (增强型)", str(path))
    assert result.success
    text = path.read_text(encoding="utf-8")
    assert "あ" in text
    assert "译文" not in text


def test_background_container_end_fills_missing_child_end():
    content = (
        '<tt xmlns="http://www.w3.org/ns/ttml" '
        'xmlns:ttm="http://www.w3.org/ns/ttml#metadata"><body>'
        '<p begin="2.000" end="4.000">'
        '<span begin="2.000" end="2.500">主</span>'
        '<span ttm:role="x-bg" begin="2.000" end="4.000">'
        '<span begin="2.000">(word</span>'
        '<span begin="3.000"> two)</span>'
        "</span></p></body></tt>"
    )
    document = TTMLParser().parse_document(content)
    project = _build_project(document)
    bg = [s for s in project.sentences if s.aux_role == "x-bg"][0]
    assert bg.text == "(word two)"
    assert bg.aux_begin == 2000
    assert bg.aux_end == 4000
    assert bg.characters[0].timestamps == [2000]
    assert bg.characters[-1].sentence_end_ts == 4000


def test_empty_main_line_with_aux_roundtrips():
    content = (
        '<tt xmlns="http://www.w3.org/ns/ttml" '
        'xmlns:ttm="http://www.w3.org/ns/ttml#metadata"><body>'
        '<p begin="1.000" end="2.000" ttm:role="x-sug-empty">'
        '<span ttm:role="x-translation" xml:lang="zh-CN">空行翻译</span>'
        "</p></body></tt>"
    )
    document = TTMLParser().parse_document(content)
    project = _build_project(document)
    assert [s.text for s in project.sentences if not s.aux_role] == [""]
    assert [s.text for s in project.sentences if s.aux_role] == ["空行翻译"]

    xml = TTMLExporter().export_to_text(project)
    rebuilt = _build_project(TTMLParser().parse_document(xml))
    assert [s.text for s in rebuilt.sentences if not s.aux_role] == [""]
    assert [s.text for s in rebuilt.sentences if s.aux_role] == ["空行翻译"]


def test_time_style_falls_back_to_precision_when_needed():
    content = (
        '<tt xmlns="http://www.w3.org/ns/ttml"><body>'
        '<p begin="0:31.5" end="0:32.5">'
        '<span begin="0:31.5" end="0:32.5">あ</span>'
        "</p></body></tt>"
    )
    project = _build_project(TTMLParser().parse_document(content))
    project.sentences[0].characters[0].timestamps = [31550]
    project.sentences[0].characters[0].set_offset(0)
    xml = TTMLExporter().export_to_text(project)
    assert 'begin="00:00:31.550"' in xml


def test_duplicate_sentence_ids_do_not_drop_lyrics_on_export():
    project = Project()
    singer = project.singers[0]
    for text, timestamp in (("あ", 1000), ("い", 2000)):
        sentence = Sentence.from_text(text, singer.id)
        sentence.id = "dup"
        sentence.characters[0].add_timestamp(timestamp)
        sentence.characters[-1].is_sentence_end = True
        sentence.characters[-1].set_sentence_end_ts(timestamp + 500)
        sentence.characters[0].set_offset(0)
        project.add_sentence(sentence)

    xml = TTMLExporter().export_to_text(project)
    rebuilt = _build_project(TTMLParser().parse_document(xml))
    assert [s.text for s in rebuilt.sentences if not s.aux_role] == ["あ", "い"]


def test_aux_only_paragraph_keeps_p_level_times():
    content = (
        '<tt xmlns="http://www.w3.org/ns/ttml" '
        'xmlns:ttm="http://www.w3.org/ns/ttml#metadata"><body>'
        '<p begin="1.000" end="2.000">'
        '<span ttm:role="x-translation" xml:lang="zh-CN">译文</span>'
        "</p></body></tt>"
    )
    project = _build_project(TTMLParser().parse_document(content))
    aux = [s for s in project.sentences if s.aux_role][0]
    assert aux.aux_begin == 1000
    assert aux.aux_end == 2000

    xml = TTMLExporter().export_to_text(project)
    root = ET.fromstring(xml)
    p = next(elem for elem in root.iter(f"{{{TTML_NS}}}p"))
    assert parse_time(p.get("begin")) == 1000
    assert parse_time(p.get("end")) == 2000


def test_sentence_shift_times_moves_aux_container():
    sentence = Sentence.from_text("(bg)", "s1")
    sentence.characters[0].add_timestamp(1000)
    sentence.characters[-1].set_sentence_end_ts(2000)
    sentence.aux_begin = 1000
    sentence.aux_end = 2000
    sentence.shift_times(500)
    assert sentence.characters[0].timestamps == [1500]
    assert sentence.characters[-1].sentence_end_ts == 2500
    assert sentence.aux_begin == 1500
    assert sentence.aux_end == 2500


def test_multi_checkpoint_spans_do_not_overlap():
    """同一演唱者的主 span 与 cp span 不能重叠（AMLL 结构约束）。"""
    project = Project()
    project.metadata.title = "overlap"
    sentence = Sentence.from_text("あい", project.singers[0].id)
    first = sentence.characters[0]
    first.set_check_count(3, ruby_split_mode="direct")
    first.add_timestamp(1000, 0)
    first.add_timestamp(1200, 1)
    first.add_timestamp(1400, 2)
    first.is_sentence_end = True
    first.set_sentence_end_ts(1500)
    sentence.characters[1].add_timestamp(1600)
    sentence.characters[1].is_sentence_end = True
    sentence.characters[1].set_sentence_end_ts(2000)
    for char in sentence.characters:
        char.set_offset(0)
    project.add_sentence(sentence)

    xml = TTMLExporter().export_to_text(project)
    root = ET.fromstring(xml)
    p = next(root.iter(f"{{{TTML_NS}}}p"))
    intervals = []
    for span in p.findall(f"{{{TTML_NS}}}span"):
        begin = parse_time(span.get("begin") or "")
        end = parse_time(span.get("end") or "")
        if begin is not None and end is not None:
            intervals.append((begin, end, span.get(f"{{{TTM_NS}}}role") or ""))
    intervals.sort()
    for prev, cur in zip(intervals, intervals[1:]):
        assert prev[1] <= cur[0], (prev, cur)
    # 主文本 span 到第二拍结束，而不是整行末尾。
    assert intervals[0][:2] == (1000, 1200)


def test_untimed_midline_release_roundtrip():
    project = Project()
    project.metadata.title = "untimed-release"
    sentence = Sentence.from_text("あい", project.singers[0].id)
    first = sentence.characters[0]
    first.is_sentence_end = True
    first.set_sentence_end_ts(1200)
    sentence.characters[1].add_timestamp(1500)
    sentence.characters[1].is_sentence_end = True
    sentence.characters[1].set_sentence_end_ts(2000)
    for char in sentence.characters:
        char.set_offset(0)
    project.add_sentence(sentence)

    xml = TTMLExporter().export_to_text(project)
    rebuilt = _build_project(TTMLParser().parse_document(xml))
    assert rebuilt.sentences[0].characters[0].sentence_end_ts == 1200
    assert _signature(project) == _signature(rebuilt)


def test_partial_ruby_checkpoints_roundtrip():
    project = Project()
    project.metadata.title = "partial-ruby"
    sentence = Sentence.from_text("あ", project.singers[0].id)
    char = sentence.characters[0]
    char.set_check_count(3, ruby_split_mode="direct")
    char.set_ruby(
        Ruby(parts=[RubyPart(text="あ"), RubyPart(text="い"), RubyPart(text="う")])
    )
    char.add_timestamp(1000, 0)
    char.is_sentence_end = True
    char.set_sentence_end_ts(2000)
    for item in sentence.characters:
        item.set_offset(0)
    project.add_sentence(sentence)

    xml = TTMLExporter().export_to_text(project)
    assert xml.count('tts:ruby="text"') == 3
    rebuilt = _build_project(TTMLParser().parse_document(xml))
    rebuilt_char = rebuilt.sentences[0].characters[0]
    assert rebuilt_char.check_count == 3
    assert rebuilt_char.timestamps == [1000]
    assert _signature(project) == _signature(rebuilt)


def test_empty_sentence_roundtrip():
    project = Project()
    project.metadata.title = "empty"
    singer = project.singers[0]
    first = Sentence.from_text("あ", singer.id)
    first.characters[0].add_timestamp(1000)
    first.characters[0].is_sentence_end = True
    first.characters[0].set_sentence_end_ts(1500)
    first.characters[0].set_offset(0)
    project.add_sentence(first)
    project.add_sentence(Sentence(singer_id=singer.id, characters=[]))
    last = Sentence.from_text("い", singer.id)
    last.characters[0].add_timestamp(2000)
    last.characters[0].is_sentence_end = True
    last.characters[0].set_sentence_end_ts(2500)
    last.characters[0].set_offset(0)
    project.add_sentence(last)

    xml = TTMLExporter().export_to_text(project)
    assert ROLE_SUG_EMPTY in xml
    rebuilt = _build_project(TTMLParser().parse_document(xml))
    assert [sentence.text for sentence in rebuilt.sentences] == ["あ", "", "い"]
    assert _signature(project) == _signature(rebuilt)


def test_zero_checkpoint_ruby_roundtrip():
    project = Project()
    project.metadata.title = "zero-ruby"
    sentence = Sentence.from_text("あ", project.singers[0].id)
    char = sentence.characters[0]
    char.set_ruby(Ruby(parts=[RubyPart(text="あ")]))
    char.set_check_count(0, force=True)
    char.is_sentence_end = True
    char.set_sentence_end_ts(2000)
    for item in sentence.characters:
        item.set_offset(0)
    project.add_sentence(sentence)

    xml = TTMLExporter().export_to_text(project)
    assert ROLE_SUG_NO_CP in xml
    rebuilt = _build_project(TTMLParser().parse_document(xml))
    rebuilt_char = rebuilt.sentences[0].characters[0]
    assert rebuilt_char.check_count == 0
    assert [part.text for part in rebuilt_char.ruby.parts] == ["あ"]
    assert _signature(project) == _signature(rebuilt)


def test_partially_timed_checkpoints_roundtrip():
    """check_count 已建但只打了一部分轴时，未打轴 cp 也要保留。"""
    project = Project()
    project.metadata.title = "partial"
    sentence = Sentence.from_text("あい", project.singers[0].id)
    first = sentence.characters[0]
    first.set_check_count(3, ruby_split_mode="direct")
    first.add_timestamp(1000, 0)
    sentence.characters[1].add_timestamp(1500)
    sentence.characters[1].is_sentence_end = True
    sentence.characters[1].set_sentence_end_ts(2000)
    for char in sentence.characters:
        char.set_offset(0)
    project.add_sentence(sentence)

    xml = TTMLExporter().export_to_text(project)
    rebuilt = _build_project(TTMLParser().parse_document(xml))
    rebuilt_char = rebuilt.sentences[0].characters[0]
    assert rebuilt_char.check_count == 3
    assert rebuilt_char.timestamps == [1000]
    assert _signature(project) == _signature(rebuilt)


def test_zero_checkpoint_character_roundtrip():
    project = Project()
    project.metadata.title = "nocp"
    sentence = Sentence.from_text("あ、", project.singers[0].id)
    sentence.characters[0].add_timestamp(1000)
    punctuation = sentence.characters[1]
    punctuation.set_check_count(0, force=True)
    punctuation.is_sentence_end = True
    punctuation.set_sentence_end_ts(2000)
    for char in sentence.characters:
        char.set_offset(0)
    project.add_sentence(sentence)

    xml = TTMLExporter().export_to_text(project)
    assert ROLE_SUG_NO_CP in xml
    rebuilt = _build_project(TTMLParser().parse_document(xml))
    assert rebuilt.sentences[0].characters[1].check_count == 0
    assert _signature(project) == _signature(rebuilt)


def test_per_char_singer_roundtrip():
    project = Project()
    project.metadata.title = "singers"
    first = project.singers[0]
    first.rename("A")
    second = Singer(name="B", color="#4ECDC4")
    project.add_singer(second)

    sentence = Sentence.from_text("あい", first.id)
    sentence.characters[0].add_timestamp(1000)
    sentence.characters[0].singer_id = first.id
    sentence.characters[1].add_timestamp(1500)
    sentence.characters[1].singer_id = second.id
    sentence.characters[1].is_sentence_end = True
    sentence.characters[1].set_sentence_end_ts(2000)
    for char in sentence.characters:
        char.set_offset(0)
    project.add_sentence(sentence)

    xml = TTMLExporter().export_to_text(project)
    rebuilt = _build_project(TTMLParser().parse_document(xml))
    assert _signature(project) == _signature(rebuilt)
    assert [char.singer_id for char in rebuilt.sentences[0].characters] == [
        rebuilt.singers[0].id,
        rebuilt.singers[1].id,
    ]


def test_background_vocal_is_preserved_and_roundtrips():
    content = (
        '<tt xmlns="http://www.w3.org/ns/ttml" '
        'xmlns:ttm="http://www.w3.org/ns/ttml#metadata">'
        '<body><p begin="1.000" end="3.000">'
        '<span begin="1.000" end="2.000">主</span>'
        '<span ttm:role="x-bg" begin="1.500" end="2.500">'
        '<span begin="1.500" end="2.000">(和</span>'
        '<span begin="2.000" end="2.500">声)</span>'
        "</span></p></body></tt>"
    )
    document = TTMLParser().parse_document(content)

    assert document.lines[0].text == "主"
    assert document.warnings == []
    tracks = document.lines[0].aux_tracks
    assert len(tracks) == 1
    assert tracks[0].role == "x-bg"
    assert tracks[0].begin == 1500
    assert tracks[0].end == 2500
    assert [child.text for child in tracks[0].children] == ["(和", "声)"]
    assert [child.begin for child in tracks[0].children] == [1500, 2000]

    project = _build_project(document)
    aux_lines = [s for s in project.sentences if s.aux_role == "x-bg"]
    assert len(aux_lines) == 1
    bg = aux_lines[0]
    assert bg.text == "(和声)"
    assert bg.aux_begin == 1500 and bg.aux_end == 2500
    assert bg.characters[0].timestamps == [1500]
    assert bg.characters[-1].sentence_end_ts == 2500

    xml = TTMLExporter().export_to_text(project)
    rebuilt = _build_project(TTMLParser().parse_document(xml))
    rebuilt_bg = [s for s in rebuilt.sentences if s.aux_role == "x-bg"]
    assert [s.text for s in rebuilt_bg] == ["(和声)"]
    assert rebuilt_bg[0].aux_begin == 1500
    assert rebuilt_bg[0].aux_end == 2500

    # 二次导出稳定（不会因 p 时间被和声撑大而漂移）。
    second_xml = TTMLExporter().export_to_text(rebuilt)
    assert xml == second_xml


def test_translation_and_romanization_are_preserved_with_lang():
    content = (
        '<tt xmlns="http://www.w3.org/ns/ttml" '
        'xmlns:ttm="http://www.w3.org/ns/ttml#metadata">'
        '<head><metadata><ttm:agent type="person" xml:id="v1"/></metadata></head>'
        '<body><p begin="1.000" end="2.000" ttm:agent="v1">'
        '<span begin="1.000" end="2.000">歌</span>'
        '<span ttm:role="x-translation" xml:lang="zh-CN">歌词翻译</span>'
        '<span ttm:role="x-roman">uta</span>'
        "</p></body></tt>"
    )
    document = TTMLParser().parse_document(content)
    project = _build_project(document)
    main = [s for s in project.sentences if not s.aux_role][0]
    aux = [s for s in project.sentences if s.aux_role]
    translations = [s for s in aux if s.aux_role == "x-translation"]
    romans = [s for s in aux if s.aux_role == "x-roman"]
    assert main.text == "歌"
    assert translations[0].text == "歌词翻译"
    assert translations[0].aux_lang == "zh-CN"
    assert romans[0].text == "uta"

    xml = TTMLExporter().export_to_text(project)
    assert 'ttm:role="x-translation"' in xml
    assert 'xml:lang="zh-CN"' in xml
    rebuilt = _build_project(TTMLParser().parse_document(xml))
    rebuilt_aux = [s for s in rebuilt.sentences if s.aux_role]
    assert [s.text for s in rebuilt_aux] == ["歌词翻译", "uta"]
    assert rebuilt_aux[0].aux_lang == "zh-CN"


class TestTtmlAgentsAndAuxExport:
    def test_named_and_unnamed_agents_roundtrip(self):
        content = (
            '<tt xmlns="http://www.w3.org/ns/ttml" '
            'xmlns:ttm="http://www.w3.org/ns/ttml#metadata" '
            'xmlns:amll="http://www.example.com/ns/amll">'
            "<head><metadata>"
            '<ttm:agent type="group" xml:id="v1000">'
            '<ttm:name type="full">合唱</ttm:name></ttm:agent>'
            '<ttm:agent type="other" xml:id="v2"/>'
            '<amll:meta key="artists" value="歌手"/>'
            "</metadata></head><body>"
            '<p begin="1.000" end="2.000" ttm:agent="v1000">'
            '<span begin="1.000" end="2.000">合</span></p>'
            '<p begin="3.000" end="4.000" ttm:agent="v2">'
            '<span begin="3.000" end="4.000">声</span></p>'
            "</body></tt>"
        )
        document = TTMLParser().parse_document(content)
        assert document.warnings == []
        named = document.agents["v1000"]
        unnamed = document.agents["v2"]
        assert named.named and named.name == "合唱"
        assert not unnamed.named

        project = _build_project(document)
        xml = TTMLExporter().export_to_text(project)
        root = ET.fromstring(xml)
        agents = {
            agent.get("{http://www.w3.org/XML/1998/namespace}id"): agent
            for agent in root.iter(f"{{{TTM_NS}}}agent")
        }
        assert agents["v1000"].get("type") == "group"
        assert agents["v1000"].find(f"{{{TTM_NS}}}name").text == "合唱"
        assert agents["v2"].get("type") == "other"
        assert agents["v2"].find(f"{{{TTM_NS}}}name") is None

        rebuilt = _build_project(TTMLParser().parse_document(xml))
        assert _signature(project) == _signature(rebuilt)
        assert _agent_signature(project) == _agent_signature(rebuilt)

    def test_renaming_unnamed_agent_writes_name(self):
        content = (
            '<tt xmlns="http://www.w3.org/ns/ttml" '
            'xmlns:ttm="http://www.w3.org/ns/ttml#metadata">'
            '<head><metadata><ttm:agent type="person" xml:id="v1"/>'
            "</metadata></head><body>"
            '<p begin="1.000" end="2.000" ttm:agent="v1">'
            '<span begin="1.000" end="2.000">歌</span></p>'
            "</body></tt>"
        )
        project = _build_project(TTMLParser().parse_document(content))
        singer = project.singers[0]
        assert not singer.ttml_agent_named
        singer.rename("主唱")
        xml = TTMLExporter().export_to_text(project)
        assert "<ttm:name" in xml
        assert "主唱" in xml
        assert 'xml:id="v1"' in xml

    def test_aux_only_paragraph_roundtrips(self):
        content = (
            '<tt xmlns="http://www.w3.org/ns/ttml" '
            'xmlns:ttm="http://www.w3.org/ns/ttml#metadata">'
            '<head><metadata><ttm:agent type="person" xml:id="v1"/>'
            "</metadata></head><body>"
            '<p begin="1.000" end="2.000" ttm:agent="v1">'
            '<span ttm:role="x-translation" xml:lang="zh-CN">只有翻译</span>'
            "</p></body></tt>"
        )
        document = TTMLParser().parse_document(content)
        assert document.lines[0].text == ""
        assert document.lines[0].aux_tracks[0].text == "只有翻译"

        project = _build_project(document)
        aux_lines = [s for s in project.sentences if s.aux_role]
        assert aux_lines and aux_lines[0].text == "只有翻译"
        assert not [s for s in project.sentences if not s.aux_role]
        xml = TTMLExporter().export_to_text(project)
        rebuilt = _build_project(TTMLParser().parse_document(xml))
        rebuilt_aux = [s for s in rebuilt.sentences if s.aux_role]
        assert rebuilt_aux and rebuilt_aux[0].text == "只有翻译"
        assert rebuilt_aux[0].aux_lang == "zh-CN"
        assert not [s for s in rebuilt.sentences if not s.aux_role]

    def test_singer_filter_and_aux_merge(self):
        project = Project()
        first = project.singers[0]
        first.rename("A")
        first.ttml_agent_id = "v1"
        first.ttml_agent_named = True
        second = Singer(name="B", color="#4ECDC4", ttml_agent_id="v2")
        project.add_singer(second)
        translator = Singer(
            name="翻译", color="#45B7D1", ttml_role="x-translation"
        )
        romanizer = Singer(name="音译", color="#FFA07A", ttml_role="x-roman")
        harmony = Singer(name="和声", color="#98D8C8", ttml_role="x-bg")
        for singer in (translator, romanizer, harmony):
            project.add_singer(singer)
        project.metadata.title = "filter"

        line_a = Sentence.from_text("あ", first.id)
        line_a.characters[0].add_timestamp(1000)
        line_a.characters[0].is_sentence_end = True
        line_a.characters[0].set_sentence_end_ts(1500)
        line_a.characters[0].set_offset(0)
        project.add_sentence(line_a)

        def aux_line(
            text, singer, role, parent, order=0, begin=None, end=None
        ):
            sentence = Sentence.from_text(text, singer.id)
            sentence.aux_role = role
            sentence.aux_parent_id = parent.id
            sentence.aux_order = order
            sentence.aux_begin = begin
            sentence.aux_end = end
            for char in sentence.characters:
                char.singer_id = singer.id
                char.set_offset(0)
            return sentence

        project.add_sentence(
            aux_line(
                "A 的翻译",
                translator,
                "x-translation",
                line_a,
                begin=1000,
                end=1500,
            )
        )

        line_b = Sentence.from_text("い", second.id)
        line_b.characters[0].add_timestamp(2000)
        line_b.characters[0].is_sentence_end = True
        line_b.characters[0].set_sentence_end_ts(2500)
        line_b.characters[0].set_offset(0)
        project.add_sentence(line_b)
        project.add_sentence(
            aux_line("i", romanizer, "x-roman", line_b, order=0)
        )
        project.add_sentence(
            aux_line(
                "(bg)",
                harmony,
                "x-bg",
                line_b,
                order=1,
                begin=2000,
                end=2200,
            )
        )

        # 只选 B：A 行与所有副轴行都不在组内。
        xml = TTMLExporter().export_to_text(project, singer_ids={second.id})
        root = ET.fromstring(xml)
        agents = list(root.iter(f"{{{TTM_NS}}}agent"))
        assert len(agents) == 1
        assert agents[0].get("{http://www.w3.org/XML/1998/namespace}id") == "v2"
        paragraphs = list(root.iter(f"{{{TTML_NS}}}p"))
        assert len(paragraphs) == 1
        assert "".join(paragraphs[0].itertext()) == "い"
        assert "x-roman" not in xml and "x-bg" not in xml

        # B + 音译：音译副轴合并回 B 的主行。
        xml = TTMLExporter().export_to_text(
            project, singer_ids={second.id, romanizer.id}
        )
        assert 'ttm:role="x-roman"' in xml
        assert 'ttm:role="x-bg"' not in xml
        assert "A 的翻译" not in xml

        # B + 和声：和声副轴合并回 B 的主行，并保留逐词/时间。
        xml = TTMLExporter().export_to_text(
            project, singer_ids={second.id, harmony.id}
        )
        assert 'ttm:role="x-bg"' in xml
        assert "(bg)" in xml
        assert 'ttm:role="x-roman"' not in xml

        # 只选音译（父行不在组内）：副轴单独成 p，仍写 x-roman。
        xml = TTMLExporter().export_to_text(
            project, singer_ids={romanizer.id}
        )
        assert 'ttm:role="x-roman"' in xml
        assert "い" not in xml


class TestTtmlExporter:
    def test_registered_without_changing_existing_formats(self):
        names = [exporter.name for exporter in get_all_exporters()]
        assert names[-1] == "TTML"
        # 既有格式一个都不能少/改名（本功能只追加）。
        assert names[:-1] == [
            "LRC (增强型)",
            "LRC (逐行)",
            "LRC (逐字)",
            "KRA",
            "TXT",
            "SRT",
            "txt2ass",
            "ASS",
            "Nicokara",
            "Nicokara (带注音)",
            "RL 编辑模式",
            "春日向注音",
            "Kirakara",
        ]
        assert get_exporter_by_name("TTML").file_extension == ".ttml"
        assert get_exporter_by_extension(".ttml").name == "TTML"

    def test_export_xml_contains_amll_namespaces_and_ruby(self):
        document = TTMLParser().parse_document(_read(SUG_FIXTURE))
        project = _build_project(document)
        xml = TTMLExporter().export_to_text(project)
        root = ET.fromstring(xml)

        assert root.tag == f"{{{TTML_NS}}}tt"
        assert root.get(f"{{{ITUNES_NS}}}timing") == "Word"
        assert root.find(f".//{{{AMLL_NS}}}meta") is not None
        assert root.find(f".//{{{TTM_NS}}}agent") is not None
        assert xml.count('tts:ruby="text"') >= 3

        roles = {
            elem.get(f"{{{TTM_NS}}}role")
            for elem in root.iter()
            if elem.get(f"{{{TTM_NS}}}role")
        }
        assert ROLE_SUG_LINK in " ".join(roles)
        assert ROLE_SUG_RELEASE in " ".join(roles)
        assert ROLE_SUG_CHECKPOINT not in " ".join(roles)

    def test_namespace_registration_is_restored(self):
        document = TTMLParser().parse_document(_read(SUG_FIXTURE))
        project = _build_project(document)
        before = dict(getattr(ET, "_namespace_map", {}))
        TTMLExporter().export_to_text(project)
        after = dict(getattr(ET, "_namespace_map", {}))
        assert before == after

    def test_export_writes_utf8_file_and_extension(self, tmp_path):
        document = TTMLParser().parse_document(_read(SUG_FIXTURE))
        project = _build_project(document)
        output = tmp_path / "roundtrip"
        TTMLExporter().export(project, str(output))

        target = tmp_path / "roundtrip.ttml"
        assert target.is_file()
        raw = target.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8")
        assert text.startswith("<?xml")
        assert "SUG TTML Roundtrip" in text

    def test_empty_project_raises(self):
        with pytest.raises(ExportError):
            TTMLExporter().export_to_text(Project())


def test_non_ruby_extra_checkpoints_roundtrip():
    """非 ruby 多 checkpoint 用空 span 保存，导入后 check_count 必须还原。"""
    project = Project()
    project.metadata.title = "checkpoint"
    sentence = Sentence.from_text("あい", project.singers[0].id)
    first = sentence.characters[0]
    first.set_check_count(3, ruby_split_mode="direct")
    first.add_timestamp(1000, 0)
    first.add_timestamp(1200, 1)
    first.add_timestamp(1400, 2)
    sentence.characters[1].add_timestamp(1600)
    sentence.characters[1].is_sentence_end = True
    sentence.characters[1].set_sentence_end_ts(2000)
    for char in sentence.characters:
        char.set_offset(0)
    project.add_sentence(sentence)

    xml = TTMLExporter().export_to_text(project)
    assert ROLE_SUG_CHECKPOINT in xml
    rebuilt = _build_project(TTMLParser().parse_document(xml))
    assert _signature(project) == _signature(rebuilt)
    assert rebuilt.sentences[0].characters[0].check_count == 3


class TestTtmlRobustness:
    def test_deep_nesting_raises_parse_error(self):
        from strange_uta_game.backend.infrastructure.parsers.lyric_parser import (
            ParseError,
        )

        content = (
            '<tt xmlns="http://www.w3.org/ns/ttml"><body>'
            '<p begin="1.000" end="2.000">'
            + "<span>" * 3000
            + "歌"
            + "</span>" * 3000
            + "</p></body></tt>"
        )
        with pytest.raises(ParseError):
            TTMLParser().parse_document(content)

    def test_ttm_title_fallback(self):
        content = (
            '<tt xmlns="http://www.w3.org/ns/ttml" '
            'xmlns:ttm="http://www.w3.org/ns/ttml#metadata"><head><metadata>'
            "<ttm:title>Fallback Title</ttm:title>"
            "</metadata></head><body>"
            '<p begin="1.000" end="2.000">歌</p>'
            "</body></tt>"
        )
        document = TTMLParser().parse_document(content)
        assert document.title == "Fallback Title"

    def test_line_level_p_with_untimed_span_inherits_p_timing(self):
        content = (
            '<tt xmlns="http://www.w3.org/ns/ttml"><body>'
            '<p begin="1.000" end="2.000"><span>歌</span></p>'
            "</body></tt>"
        )
        line = TTMLParser().parse_document(content).lines[0]
        assert line.text == "歌"
        assert line.timetags == [(0, 1000)]
        assert line.line_end_ts == 2000

    def test_dur_attribute_is_supported(self):
        content = (
            '<tt xmlns="http://www.w3.org/ns/ttml"><body>'
            '<p begin="1.000" dur="2.000">'
            '<span begin="1.000" dur="0.500">あ</span>'
            '<span begin="1.500" dur="1.500">い</span>'
            "</p></body></tt>"
        )
        line = TTMLParser().parse_document(content).lines[0]
        assert line.timetags == [(0, 1000), (1, 1500)]
        assert line.line_end_ts == 3000

    def test_unknown_agent_is_created_from_p(self):
        content = (
            '<tt xmlns="http://www.w3.org/ns/ttml" '
            'xmlns:ttm="http://www.w3.org/ns/ttml#metadata"><body>'
            '<p begin="1.000" end="2.000" ttm:agent="v9">'
            '<span begin="1.000" end="2.000">あ</span>'
            "</p></body></tt>"
        )
        document = TTMLParser().parse_document(content)
        assert document.agents["v9"].name == "v9"
        assert document.lines[0].char_singer_map == {0: "v9"}

    def test_unknown_agent_with_artist_keeps_line_singer(self):
        content = (
            '<tt xmlns="http://www.w3.org/ns/ttml" '
            'xmlns:ttm="http://www.w3.org/ns/ttml#metadata" '
            'xmlns:amll="http://www.example.com/ns/amll">'
            '<head><metadata><amll:meta key="artists" value="A"/>'
            "</metadata></head><body>"
            '<p begin="1.000" end="2.000" ttm:agent="v9">'
            '<span begin="1.000" end="2.000">あ</span>'
            "</p></body></tt>"
        )
        document = TTMLParser().parse_document(content)
        assert document.agents["v9"].name == "A (v9)"
        assert document.lines[0].line_singer_name == "A (v9)"
        assert document.lines[0].char_singer_map == {0: "A (v9)"}
        project = _build_project(document)
        singer_names = {singer.id: singer.name for singer in project.singers}
        assert singer_names[project.sentences[0].singer_id] == "A (v9)"


class TestLineLevelTtml:
    def test_pure_text_paragraph(self):
        content = (
            '<tt xmlns="http://www.w3.org/ns/ttml"><body>'
            '<p begin="1.000" end="3.000"> ABC </p>'
            "</body></tt>"
        )
        document = TTMLParser().parse_document(content)
        assert len(document.lines) == 1
        line = document.lines[0]
        assert line.text == " ABC "
        assert line.timetags
        assert line.line_end_ts == 3000

        project = _build_project(document)
        chars = project.sentences[0].characters
        assert chars[0].timestamps == [1000]
        assert chars[-1].timestamps[0] < 3000
        assert chars[-1].sentence_end_ts == 3000


def test_aux_role_sentences_and_agent_metadata_survive_sug_save(tmp_path):
    """.sug 必须能保存/恢复副轴 Sentence 和 agent 来源，供稍后导出 TTML。"""
    from strange_uta_game.backend.infrastructure.persistence.sug_io import (
        SugProjectParser,
    )

    project = Project()
    singer = project.singers[0]
    singer.rename("主唱")
    singer.ttml_agent_id = "v1"
    singer.ttml_agent_type = "other"
    singer.ttml_agent_named = False
    singer.ttml_agent_display_name = "主唱 (v1)"
    singer.ttml_agent_source_name = ""
    translator = Singer(
        name="翻译", color="#4ECDC4", ttml_role="x-translation"
    )
    harmony = Singer(name="和声", color="#45B7D1", ttml_role="x-bg")
    project.add_singer(translator)
    project.add_singer(harmony)
    project.metadata.title = "persist"

    sentence = Sentence.from_text("あ", singer.id)
    sentence.song_part = "Verse"
    sentence.characters[0].add_timestamp(1000)
    sentence.characters[0].is_sentence_end = True
    sentence.characters[0].set_sentence_end_ts(1500)
    sentence.characters[0].set_offset(0)
    project.add_sentence(sentence)

    translation = Sentence.from_text("翻译", translator.id)
    translation.aux_role = "x-translation"
    translation.aux_parent_id = sentence.id
    translation.aux_lang = "zh-CN"
    translation.aux_order = 0
    project.add_sentence(translation)

    background = Sentence.from_text("(和声)", harmony.id)
    background.aux_role = "x-bg"
    background.aux_parent_id = sentence.id
    background.aux_begin = 1100
    background.aux_end = 1400
    background.aux_order = 1
    background.characters[0].add_timestamp(1100)
    background.characters[-1].is_sentence_end = True
    background.characters[-1].set_sentence_end_ts(1400)
    for char in background.characters:
        char.set_offset(0)
    project.add_sentence(background)

    path = tmp_path / "aux.sug"
    SugProjectParser.save(project, str(path))
    loaded = SugProjectParser.load(str(path))

    assert loaded.singers[0].ttml_agent_id == "v1"
    assert loaded.singers[0].ttml_agent_type == "other"
    assert loaded.singers[0].ttml_agent_named is False
    assert loaded.singers[0].ttml_agent_display_name == "主唱 (v1)"
    assert loaded.singers[1].ttml_role == "x-translation"
    assert loaded.singers[2].ttml_role == "x-bg"
    assert loaded.sentences[0].song_part == "Verse"
    aux = [s for s in loaded.sentences if s.aux_role]
    assert aux[0].text == "翻译" and aux[0].aux_lang == "zh-CN"
    assert aux[0].aux_parent_id == loaded.sentences[0].id
    assert aux[1].text == "(和声)"
    assert aux[1].aux_begin == 1100 and aux[1].aux_end == 1400
    assert aux[1].characters[-1].sentence_end_ts == 1400


def test_fixture_paths_exist():
    assert SUG_FIXTURE.is_file()
    assert AMLL_FIXTURE.is_file()
    for fixture in SECTION_FIXTURES:
        assert fixture.is_file(), fixture
