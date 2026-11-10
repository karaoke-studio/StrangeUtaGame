"""embedded TTML 交接接口契约测试。

不构造真实 MainWindow / QApplication：只调用 MainWindow 的公开实例方法，
用 SimpleNamespace 模拟宿主与 store，守护 docs/EMBEDDING.md §2.2 的加法契约。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from strange_uta_game.backend.domain import Project, Sentence
from strange_uta_game.backend.infrastructure.parsers.ttml_parser import TTMLParser
from strange_uta_game.frontend.main_window import MainWindow

VALID_TTML = (
    '<tt xmlns="http://www.w3.org/ns/ttml" '
    'xmlns:ttm="http://www.w3.org/ns/ttml#metadata" '
    'xmlns:itunes="http://music.apple.com/lyric-ttml-internal">'
    "<head><metadata>"
    '<ttm:agent type="person" xml:id="v1">'
    '<ttm:name type="full">初音ミク</ttm:name></ttm:agent>'
    "</metadata></head><body>"
    '<p begin="1.000" end="2.000" ttm:agent="v1">'
    '<span begin="1.000" end="1.500">歌</span>'
    '<span begin="1.500" end="2.000">う</span>'
    "</p></body></tt>"
)


def _project() -> Project:
    project = Project()
    project.metadata.title = "交接测试"
    project.metadata.artist = "初音ミク"
    project.metadata.album = "TTML"
    project.metadata.extra = {"ncmMusicId": "1001", "isrc": "TEST-ISRC"}
    sentence = Sentence.from_text("歌う", project.singers[0].id)
    sentence.characters[0].add_timestamp(1000)
    sentence.characters[1].add_timestamp(1500)
    sentence.characters[-1].is_sentence_end = True
    sentence.characters[-1].set_sentence_end_ts(2000)
    for char in sentence.characters:
        char.set_offset(0)
    project.add_sentence(sentence)
    return project


def _host(project: Project):
    store = SimpleNamespace(
        project=project,
        save_path="D:/songs/song.sug",
        original_media_path=None,
        audio_path=None,
        get_saveable_media_path=lambda: None,
    )
    settings = SimpleNamespace(get=lambda _key: {})
    return SimpleNamespace(
        _store=store,
        settingInterface=SimpleNamespace(get_settings=lambda: settings),
    )


class TestExportHandoff:
    def test_export_to_next_payload_adds_ttml(self):
        payload = MainWindow.export_to_next_payload(_host(_project()))

        assert payload["ttml"].startswith("<?xml")
        assert "<tt " in payload["ttml"]
        assert 'itunes:timing="Word"' in payload["ttml"]
        assert "交接测试" in payload["ttml"]
        assert payload["ttml_meta"]["extra"]["ncmMusicId"] == "1001"
        assert 'key="ncmMusicId"' in payload["ttml"]
        # 既有键保持原样（加法契约）。
        assert payload["axis_plan"]["mode"] == "single"
        assert payload["project"] is not None

    def test_export_ttml_text_returns_none_for_empty_project(self):
        host = SimpleNamespace(_store=SimpleNamespace(project=Project()))
        assert MainWindow.export_ttml_text(host) is None

    def test_export_ttml_text_is_parseable(self):
        host = SimpleNamespace(_store=SimpleNamespace(project=_project()))
        xml = MainWindow.export_ttml_text(host)
        document = TTMLParser().parse_document(xml)
        assert len(document.lines) == 1
        assert document.lines[0].text == "歌う"
        assert document.lines[0].line_end_ts == 2000


class TestImportNotifications:
    def test_file_loader_notifies_pending_main_window(self):
        from strange_uta_game.frontend.editor.timing.file_loader import FileLoader

        class Signal:
            def __init__(self):
                self.calls = []

            def emit(self, success, message):
                self.calls.append((success, message))

        class Widget:
            _pending_ttml_import = True
            ttml_import_finished = Signal()

            def parentWidget(self):  # noqa: N802 - mirror Qt API
                return None

        editor = Widget()
        loader = FileLoader.__new__(FileLoader)
        loader._editor = editor

        loader._notify_ttml_import_finished(True, "3 lines")
        assert editor.ttml_import_finished.calls == [(True, "3 lines")]
        assert editor._pending_ttml_import is False

        # 非 TTML 受理的导入不会重复发信号。
        loader._notify_ttml_import_finished(True, "again")
        assert editor.ttml_import_finished.calls == [(True, "3 lines")]

    def test_import_ttml_from_file_reads_and_delegates(self, tmp_path):
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "ttml"
            / "sug_roundtrip.ttml"
        )
        content = fixture.read_text(encoding="utf-8")
        calls = []
        host = SimpleNamespace(
            editorInterface=SimpleNamespace(
                _file_loader=SimpleNamespace(
                    load_lyrics_from_text=lambda text: calls.append(text)
                )
            ),
            switchTo=lambda _page: None,
        )

        assert (
            MainWindow.import_ttml_from_file(
                host, str(fixture), switch_to_editor=False
            )
            is True
        )
        # 工作树在 core.autocrlf=true 的 Windows 上会检出 CRLF；导入语义
        # 按文本内容比较（XML 对换行不敏感），不应因换行风格失败。
        assert [text.replace("\r\n", "\n") for text in calls] == [content]

    def test_import_ttml_from_file_rejects_missing_file(self):
        errors = []
        host = SimpleNamespace(
            _show_ttml_import_error=lambda message: errors.append(message),
        )

        assert (
            MainWindow.import_ttml_from_file(
                host, "Z:/does/not/exist.ttml", switch_to_editor=False
            )
            is False
        )
        assert errors


class TestImportHandoff:
    def test_accepts_valid_ttml(self):
        calls = []
        loader = SimpleNamespace(
            load_lyrics_from_text=lambda content: calls.append(content)
        )
        host = SimpleNamespace(
            editorInterface=SimpleNamespace(_file_loader=loader),
            switchTo=lambda page: calls.append(page),
        )

        ok = MainWindow.import_ttml_from_text(host, VALID_TTML)

        assert ok is True
        assert calls[0] is host.editorInterface
        assert calls[1] == VALID_TTML

    def test_rejects_malformed_xml(self):
        errors = []
        host = SimpleNamespace(
            _show_ttml_import_error=lambda message: errors.append(message),
        )

        ok = MainWindow.import_ttml_from_text(host, "<tt><body>")

        assert ok is False
        assert errors and "TTML" in errors[0]

    def test_rejects_document_without_lines(self):
        errors = []
        host = SimpleNamespace(
            _show_ttml_import_error=lambda message: errors.append(message),
        )

        ok = MainWindow.import_ttml_from_text(
            host, '<tt xmlns="http://www.w3.org/ns/ttml"><body/></tt>'
        )

        assert ok is False
        assert errors

    def test_rejects_unsaved_project_without_force(self):
        errors = []
        calls = []
        host = SimpleNamespace(
            _store=SimpleNamespace(
                project=SimpleNamespace(sentences=[object()])
            ),
            has_unsaved_changes=lambda: True,
            editorInterface=SimpleNamespace(
                _file_loader=SimpleNamespace(
                    load_lyrics_from_text=lambda content: calls.append(content)
                )
            ),
            switchTo=lambda _page: None,
            _show_ttml_import_error=lambda message: errors.append(message),
        )

        assert MainWindow.import_ttml_from_text(host, VALID_TTML) is False
        assert calls == []
        assert errors and "未保存" in errors[0]

    def test_force_allows_unsaved_project_replace(self):
        calls = []
        host = SimpleNamespace(
            _store=SimpleNamespace(
                project=SimpleNamespace(sentences=[object()])
            ),
            has_unsaved_changes=lambda: True,
            editorInterface=SimpleNamespace(
                _file_loader=SimpleNamespace(
                    load_lyrics_from_text=lambda content: calls.append(content)
                )
            ),
            switchTo=lambda _page: None,
        )

        assert (
            MainWindow.import_ttml_from_text(host, VALID_TTML, force=True)
            is True
        )
        assert calls == [VALID_TTML]

    def test_rejects_while_another_lyric_parse_is_running(self):
        errors = []
        loader = SimpleNamespace(
            _lyric_thread=object(),
            load_lyrics_from_text=lambda _content: None,
        )
        host = SimpleNamespace(
            editorInterface=SimpleNamespace(_file_loader=loader),
            switchTo=lambda _page: None,
            _show_ttml_import_error=lambda message: errors.append(message),
        )

        assert MainWindow.import_ttml_from_text(host, VALID_TTML) is False
        assert errors and "解析" in errors[0]

    def test_loader_exception_returns_false(self):
        errors = []

        def _boom(_content):
            raise RuntimeError("loader-boom")

        host = SimpleNamespace(
            editorInterface=SimpleNamespace(
                _file_loader=SimpleNamespace(load_lyrics_from_text=_boom)
            ),
            switchTo=lambda _page: None,
            _show_ttml_import_error=lambda message: errors.append(message),
        )

        assert MainWindow.import_ttml_from_text(host, VALID_TTML) is False
        assert errors == ["loader-boom"]

    def test_rejects_empty_text(self):
        errors = []
        host = SimpleNamespace(
            _show_ttml_import_error=lambda message: errors.append(message),
        )

        assert MainWindow.import_ttml_from_text(host, "   ") is False
        assert errors


def test_ttml_import_warnings_signal_exists():
    """宿主可订阅导入时“无法修改/将丢弃”的内容列表。"""
    assert hasattr(MainWindow, "ttml_import_warnings")
