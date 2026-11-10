"""ProjectImportService 单元测试。

覆盖：
- 内联格式（``[n|HH:MM:SS]``）
- LRC 格式（``.lrc`` 通过工厂）
- 带 LRC 时间标签的 ``.txt`` 文件（应强制 LRC 解析）
- 不存在文件 / 空文件
- 所有 Sentence 均归属到传入的 default_singer_id
"""

from __future__ import annotations

from pathlib import Path

import pytest

from strange_uta_game.backend.application import (
    ProjectImportError,
    ProjectImportService,
)


SINGER_ID = "singer-default"


def _write(tmp_path: Path, name: str, content: str) -> str:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


class TestLoadLyricsFromFile:
    def test_lrc_file(self, tmp_path: Path):
        content = "[00:00.00]赤い花\n[00:05.00]白い雲\n"
        path = _write(tmp_path, "song.lrc", content)

        sentences = ProjectImportService.load_lyrics_from_file(path, SINGER_ID)

        assert len(sentences) == 2
        assert sentences[0].text == "赤い花"
        assert sentences[1].text == "白い雲"
        assert all(s.singer_id == SINGER_ID for s in sentences)

    def test_txt_with_lrc_tags_forces_lrc_parser(self, tmp_path: Path):
        # 即使扩展名是 .txt，含 [MM:SS.xx] 标签也应走 LRC
        content = "[00:01.00]あ\n[00:02.00]い\n"
        path = _write(tmp_path, "song.txt", content)

        sentences = ProjectImportService.load_lyrics_from_file(path, SINGER_ID)

        assert len(sentences) == 2
        assert sentences[0].text == "あ"
        assert sentences[1].text == "い"

    def test_inline_format_detected(self, tmp_path: Path):
        # 内联格式：[<cp_idx>|HH:MM:SS]字符
        # 取自 sentences_from_inline_text 支持的最小形态
        content = "[0|00:00:00]あ[1|00:00:05]い\n"
        path = _write(tmp_path, "inline.txt", content)

        sentences = ProjectImportService.load_lyrics_from_file(path, SINGER_ID)

        assert len(sentences) >= 1
        # 所有 sentence 归属默认演唱者
        assert all(s.singer_id == SINGER_ID for s in sentences)

    def test_ttml_file_text_and_timings(self, tmp_path: Path):
        fixture = (
            Path(__file__).resolve().parents[2]
            / "fixtures"
            / "ttml"
            / "sug_roundtrip.ttml"
        )
        path = _write(tmp_path, "song.ttml", fixture.read_text(encoding="utf-8"))

        sentences = ProjectImportService.load_lyrics_from_file(path, SINGER_ID)

        assert len(sentences) == 5
        assert sentences[0].text == "夢を見る"
        assert sentences[0].characters[0].timestamps == [1000]
        # 服务没有 Singer 列表：全部先归默认演唱者。
        assert all(s.singer_id == SINGER_ID for s in sentences)

    def test_ttml_with_inline_like_text_is_not_parsed_as_inline(self, tmp_path: Path):
        content = (
            '<tt xmlns="http://www.w3.org/ns/ttml"><body>'
            '<p begin="1.000" end="2.000">[0|00:00:00] 歌词</p>'
            "</body></tt>"
        )
        path = _write(tmp_path, "inline_like.ttml", content)

        sentences, metadata = ProjectImportService.load_lyrics_and_meta_from_file(
            path, SINGER_ID
        )

        assert sentences[0].text == "[0|00:00:00] 歌词"
        assert metadata["format"] == "ttml"

    def test_ttml_metadata(self, tmp_path: Path):
        fixture = (
            Path(__file__).resolve().parents[2]
            / "fixtures"
            / "ttml"
            / "sug_roundtrip.ttml"
        )
        path = _write(tmp_path, "song.ttml", fixture.read_text(encoding="utf-8"))

        sentences, metadata = ProjectImportService.load_lyrics_and_meta_from_file(
            path, SINGER_ID
        )

        assert len(sentences) == 5
        assert metadata["format"] == "ttml"
        assert metadata["title"] == "SUG TTML Roundtrip"
        assert metadata["artist"] == "初音ミク / 鏡音リン"

    def test_missing_file_raises_project_import_error(self, tmp_path: Path):
        missing = str(tmp_path / "nope.lrc")
        with pytest.raises(ProjectImportError):
            ProjectImportService.load_lyrics_from_file(missing, SINGER_ID)

    def test_empty_file_returns_empty_list(self, tmp_path: Path):
        path = _write(tmp_path, "empty.lrc", "")
        sentences = ProjectImportService.load_lyrics_from_file(path, SINGER_ID)
        assert sentences == []


class TestEncodingFallbackE1:
    """E1：导入服务经 decode_lyric_bytes 走完整编码回退链，
    不再用纯 utf-8 预读使回退失效。"""

    def test_gbk_lrc_file(self, tmp_path: Path):
        p = tmp_path / "song.lrc"
        p.write_bytes("[00:01.00]从此我不能\n[00:02.00]听见你的温柔\n".encode("gbk"))

        sentences = ProjectImportService.load_lyrics_from_file(str(p), SINGER_ID)

        assert [s.text for s in sentences] == ["从此我不能", "听见你的温柔"]
        assert all(s.singer_id == SINGER_ID for s in sentences)

    def test_utf16_lrc_file(self, tmp_path: Path):
        p = tmp_path / "song.lrc"
        p.write_bytes("[00:01.00]あ\n".encode("utf-16"))

        sentences = ProjectImportService.load_lyrics_from_file(str(p), SINGER_ID)

        assert len(sentences) == 1
        assert sentences[0].text == "あ"

    def test_undecodable_file_raises_project_import_error(self, tmp_path: Path):
        p = tmp_path / "song.lrc"
        p.write_bytes(b"\x81\x00\x81\x01")

        with pytest.raises(ProjectImportError):
            ProjectImportService.load_lyrics_from_file(str(p), SINGER_ID)
