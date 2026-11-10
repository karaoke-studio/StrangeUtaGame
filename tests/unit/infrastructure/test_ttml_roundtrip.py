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
from strange_uta_game.backend.infrastructure.parsers.lyric_parser import (
    parse_to_sentences,
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
    format_time,
    is_ttml_content,
    parse_time,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ttml"
SUG_FIXTURE = FIXTURES / "sug_roundtrip.ttml"
AMLL_FIXTURE = FIXTURES / "amll_101126.ttml"

_SINGER_COLORS = ["#FF6B6B", "#4ECDC4", "#45B7D1", "#FFA07A"]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _build_project(document) -> Project:
    """按导入层同样的规则把 TTMLDocument 变成 Project（不依赖 Qt）。"""
    project = Project()
    default = project.singers[0]
    name_to_id: Dict[str, str] = {}

    for index, (_agent_id, agent) in enumerate(sorted(document.agents.items())):
        name = agent.name or agent.id
        if index == 0:
            default.rename(name)
            name_to_id[name] = default.id
        else:
            singer = Singer(
                name=name,
                color=_SINGER_COLORS[index % len(_SINGER_COLORS)],
                is_default=False,
            )
            project.add_singer(singer)
            name_to_id[name] = singer.id

    project.sentences = parse_to_sentences(
        document.lines,
        default.id,
        singer_name_to_id=name_to_id,
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
        sentences.append(
            {
                "text": sentence.text,
                "singer": _singer_name(project, sentence.singer_id),
                "chars": chars,
            }
        )

    metadata = project.metadata
    return {
        "title": metadata.title if metadata else "",
        "artist": metadata.artist if metadata else "",
        "album": metadata.album if metadata else "",
        "language": metadata.language if metadata else "",
        "extra": dict(metadata.extra) if metadata else {},
        "singers": [singer.name for singer in project.singers],
        "sentences": sentences,
    }


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
        assert any("x-translation" in warning for warning in document.warnings)

    def test_ruby_and_roles_are_preserved(self, result):
        project = result[1]
        line2 = project.sentences[1]
        assert line2.text == "光へ"
        light = line2.characters[0]
        assert light.check_count == 3
        assert [part.text for part in light.ruby.parts] == ["ひ", "か", "り"]
        assert light.timestamps == [3500, 3800, 4100]
        assert light.is_fully_timed

        line5 = project.sentences[4]
        assert line5.characters[0].linked_to_next is True
        assert line5.characters[1].is_sentence_end is True
        assert line5.characters[1].sentence_end_ts == 9000

    def test_model_signature_roundtrips(self, result):
        _, first_project, _, second_project, _ = result
        assert _signature(first_project) == _signature(second_project)

    def test_second_export_is_stable(self, result):
        _, _, first_xml, _, second_xml = result
        assert first_xml == second_xml

    def test_unsupported_translation_is_not_merged(self, result):
        project = result[1]
        assert "向着光" not in "".join(
            sentence.text for sentence in project.sentences
        )


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
        assert any("ttm:agent" in warning for warning in document.warnings)
        assert {agent.name for agent in document.agents.values()} == {
            "河图 (v1)",
            "河图 (v2)",
        }

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


def test_background_vocal_is_ignored_and_warned():
    content = (
        '<tt xmlns="http://www.w3.org/ns/ttml" '
        'xmlns:ttm="http://www.w3.org/ns/ttml#metadata">'
        '<body><p begin="1.000" end="3.000">'
        '<span begin="1.000" end="2.000">主</span>'
        '<span ttm:role="x-bg"><span begin="1.500" end="2.500">(和声)</span></span>'
        "</p></body></tt>"
    )
    document = TTMLParser().parse_document(content)

    assert document.lines[0].text == "主"
    assert "x-bg" in "".join(document.warnings)


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


def test_fixture_paths_exist():
    assert SUG_FIXTURE.is_file()
    assert AMLL_FIXTURE.is_file()
