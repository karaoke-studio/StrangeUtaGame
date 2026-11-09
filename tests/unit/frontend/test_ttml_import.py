"""TTML 导入前端管线测试（不构造主窗口）。"""

from __future__ import annotations

from pathlib import Path

from strange_uta_game.backend.domain import Project
from strange_uta_game.backend.application.export_service import ExportService
from strange_uta_game.frontend.editor.timing.file_loader import (
    LYRIC_EXTENSIONS,
    classify_supported_file,
)
from strange_uta_game.frontend.editor.timing.lyric_loader import (
    detect_lyric_format,
    parse_lyric_content,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ttml"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_ttml_is_detected_and_classified(tmp_path):
    content = _read("sug_roundtrip.ttml")
    assert detect_lyric_format(content) == "ttml"
    assert ".ttml" in LYRIC_EXTENSIONS

    path = tmp_path / "sample.ttml"
    path.write_text(content, encoding="utf-8")
    assert classify_supported_file(str(path)) == "lyric"


def test_parse_custom_ttml_maps_singers_ruby_and_metadata():
    content = _read("sug_roundtrip.ttml")
    project = Project()

    sentences, is_nicokara, new_singers, meta = parse_lyric_content(
        content, project.singers[0].id, project.singers
    )

    assert is_nicokara is False
    assert len(sentences) == 5
    assert [singer.name for singer in new_singers] == ["初音ミク", "鏡音リン"]
    assert meta["format"] == "ttml"
    assert meta["title"] == "SUG TTML Roundtrip"
    assert meta["prompt_ruby_choice"] == "1"

    ruby_char = sentences[1].characters[0]
    assert [part.text for part in ruby_char.ruby.parts] == ["ひ", "か", "り"]
    assert ruby_char.timestamps == [3500, 3800, 4100]


def test_parse_real_amll_maps_agents_and_line_end():
    content = _read("amll_101126.ttml")
    project = Project()

    sentences, _is_nicokara, new_singers, meta = parse_lyric_content(
        content, project.singers[0].id, project.singers
    )

    assert len(sentences) == 50
    assert {singer.name for singer in new_singers} == {"河图 (v1)", "河图 (v2)"}
    assert meta["title"] == "第三十八年夏至"
    assert meta["artist"] == "河图"
    assert meta["extra"]["ncmMusicId"] == "101126"
    assert meta["extra"]["isrc"] == "HKG731554003"
    assert sentences[0].text == "衰草连横向晚晴"
    singer_names = {singer.id: singer.name for singer in new_singers}
    assert singer_names[sentences[0].singer_id] == "河图 (v2)"
    assert singer_names[sentences[0].characters[0].singer_id] == "河图 (v2)"
    assert sentences[0].characters[0].timestamps == [2739]
    assert sentences[0].characters[-1].sentence_end_ts == 10_064
    # span 之间的空格是隐式排版空白，不应变成未打轴节奏点。
    assert all(sentence.is_fully_timed() for sentence in sentences)


def test_export_interface_lists_ttml_last(qapp):
    from PyQt6.QtCore import Qt

    from strange_uta_game.frontend.export.export_interface import ExportInterface

    page = ExportInterface()
    names = [
        page.format_list.item(i).data(Qt.ItemDataRole.UserRole)
        for i in range(page.format_list.count())
    ]
    assert names[-1] == "TTML"
    assert page.format_list.item(len(names) - 1).text() == "TTML (.ttml)"


def test_home_interface_detects_ttml():
    from strange_uta_game.frontend.home.home_interface import HomeInterface

    page = HomeInterface.__new__(HomeInterface)
    assert page._detect_format(_read("sug_roundtrip.ttml")) == "ttml"


def _new_home_for_parse():
    from strange_uta_game.frontend.home.home_interface import HomeInterface

    page = HomeInterface.__new__(HomeInterface)
    page._lyric_lines = []
    page._nicokara_singers = []
    page._nicokara_singer_key_to_id = {}
    page._ttml_singers = []
    page._ttml_meta = {}
    return page


def test_home_parse_preserves_ttml_singers_and_metadata():
    page = _new_home_for_parse()
    page._parse_text_content(_read("sug_roundtrip.ttml"))

    assert len(page._lyric_lines) == 5
    assert [singer.name for singer in page._ttml_singers] == ["初音ミク", "鏡音リン"]
    assert page._ttml_meta["title"] == "SUG TTML Roundtrip"
    # 第 3 行是 v2（鏡音リン），不能被重置成默认临时演唱者。
    line3_singers = {
        char.singer_id for char in page._lyric_lines[2].characters
    }
    assert line3_singers == {page._ttml_singers[1].id}


def test_home_imported_singers_map_to_project_and_metadata():
    from strange_uta_game.backend.domain import Project
    from strange_uta_game.frontend.home.home_interface import HomeInterface

    page = _new_home_for_parse()
    page._parse_text_content(_read("sug_roundtrip.ttml"))
    project = Project()
    default = project.get_default_singer()

    HomeInterface._apply_imported_singers(
        project,
        page._lyric_lines,
        page._ttml_singers,
        default,
        reuse_placeholder=True,
    )
    HomeInterface._apply_ttml_metadata(
        project, page._ttml_meta, set_language=True, set_extra=True
    )

    assert [singer.name for singer in project.singers] == [
        "初音ミク",
        "鏡音リン",
    ]
    mapped = {
        project.get_singer(char.singer_id).name
        for char in page._lyric_lines[0].characters
        if project.get_singer(char.singer_id) is not None
    }
    assert mapped == {"初音ミク"}
    assert project.metadata.title == "SUG TTML Roundtrip"
    assert project.metadata.artist == "初音ミク / 鏡音リン"
    assert project.metadata.album == "SUG TTML Fixtures"
    assert project.metadata.language == "ja"
    # 自定义样本没有平台 ID；真实 AMLL 样本的 ID 由另一个测试覆盖。


def test_file_loader_rejects_bad_ttml_before_replacing(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from strange_uta_game.frontend.editor.timing import file_loader as module

    errors: list[dict] = []
    monkeypatch.setattr(
        module.InfoBar, "error", lambda **kwargs: errors.append(kwargs)
    )
    monkeypatch.setattr(module.InfoBar, "warning", lambda **_kwargs: None)

    loader = module.FileLoader.__new__(module.FileLoader)
    loader._editor = SimpleNamespace(tr=lambda text: text)

    bad = tmp_path / "bad.ttml"
    bad.write_text("<tt><body>", encoding="utf-8")
    assert loader._validate_ttml_before_replace(str(bad)) is False
    assert errors

    good = tmp_path / "good.ttml"
    good.write_text(_read("sug_roundtrip.ttml"), encoding="utf-8")
    assert loader._validate_ttml_before_replace(str(good)) is True


def test_ttml_metadata_does_not_overwrite_existing_language():
    from strange_uta_game.backend.domain import Project
    from strange_uta_game.frontend.home.home_interface import HomeInterface

    project = Project()
    project.metadata.language = "zh-Hans"
    HomeInterface._apply_ttml_metadata(
        project,
        {"title": "T", "artist": "A", "album": "B", "language": "ja"},
    )
    assert project.metadata.language == "zh-Hans"
    assert project.metadata.title == "T"


def test_nicokara_import_keeps_default_placeholder_behavior():
    """存量 Nicokara 主页导入不得因 TTML 的占位复用而改变。"""
    from strange_uta_game.backend.domain import Project
    from strange_uta_game.frontend.home.home_interface import HomeInterface

    page = _new_home_for_parse()
    page._parse_text_content(_read("sug_roundtrip.ttml"))
    project = Project()
    default = project.get_default_singer()

    HomeInterface._apply_imported_singers(
        project, page._lyric_lines, page._ttml_singers, default
    )

    assert [singer.name for singer in project.singers] == [
        "未命名",
        "初音ミク",
        "鏡音リン",
    ]


def test_export_service_lists_ttml_last():
    formats = ExportService().get_available_formats()
    assert formats[-1]["name"] == "TTML"
    assert formats[-1]["extension"] == ".ttml"
    # 既有格式顺序不变。
    assert formats[0]["name"] == "LRC (增强型)"
