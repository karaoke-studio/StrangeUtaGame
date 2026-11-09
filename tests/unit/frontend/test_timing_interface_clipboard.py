from __future__ import annotations

from types import SimpleNamespace

from PyQt6.QtCore import QMimeData
from PyQt6.QtWidgets import QApplication

from strange_uta_game.backend.domain import Character, Sentence
from strange_uta_game.frontend.editor import timing_interface as timing_module
from strange_uta_game.frontend.editor.timing_interface import EditorInterface


class _Clipboard:
    def __init__(self, mime: QMimeData | None = None) -> None:
        self.mime = mime or QMimeData()

    def text(self) -> str:
        return self.mime.text()

    def mimeData(self) -> QMimeData:  # noqa: N802 - mirror QClipboard API
        return self.mime

    def setMimeData(self, mime: QMimeData) -> None:  # noqa: N802
        self.mime = mime


def test_copy_uses_inline_text_and_private_inline_payload(monkeypatch) -> None:
    clipboard = _Clipboard()
    monkeypatch.setattr(
        QApplication, "clipboard", staticmethod(lambda: clipboard)
    )
    monkeypatch.setattr(timing_module.InfoBar, "success", lambda **_kwargs: None)

    character = Character(char="字", timestamps=[1230], singer_id="singer")
    sentence = Sentence(singer_id="singer", characters=[character])
    preview = SimpleNamespace(
        get_normalized_selection=lambda: (0, 0, 0, 0),
        is_multi_line_selection=lambda: False,
    )
    editor = SimpleNamespace(
        _project=SimpleNamespace(
            singers=[], sentences=[sentence], global_offset_ms=0
        ),
        preview=preview,
        _current_line_idx=0,
        tr=lambda text: text,
    )

    EditorInterface._on_copy_chars(editor)

    assert clipboard.text() == "[00:01.23]字"
    assert clipboard.mimeData().hasFormat(
        timing_module._SUG_INLINE_CLIPBOARD_MIME
    )
    payload = bytes(
        clipboard.mimeData().data(timing_module._SUG_INLINE_CLIPBOARD_MIME)
    ).decode("utf-8")
    assert payload == "[00:01.23]字"


def test_paste_ttml_into_existing_project_does_not_insert_xml(monkeypatch) -> None:
    mime = QMimeData()
    mime.setText(
        '<tt xmlns="http://www.w3.org/ns/ttml"><body>'
        '<p begin="1.000" end="2.000">歌</p></body></tt>'
    )
    clipboard = _Clipboard(mime)
    monkeypatch.setattr(
        QApplication, "clipboard", staticmethod(lambda: clipboard)
    )
    warnings: list[dict] = []
    monkeypatch.setattr(
        timing_module.InfoBar, "warning", lambda **kwargs: warnings.append(kwargs)
    )

    inserted: list[str] = []
    loaded: list[str] = []
    editor = SimpleNamespace(
        _INLINE_TS_DETECT_RE=timing_module.EditorInterface._INLINE_TS_DETECT_RE,
        _file_loader=SimpleNamespace(
            can_load_from_clipboard=lambda: False,
            load_lyrics_from_text=lambda text: loaded.append(text),
        ),
        _paste_chars_at_cursor=lambda text: inserted.append(text),
        tr=lambda text: text,
    )

    EditorInterface._on_paste_lyrics(editor)

    assert inserted == []
    assert loaded == []
    assert warnings and "TTML" in warnings[0]["title"]


def test_paste_prefers_private_inline_payload_over_plain_text(monkeypatch) -> None:
    mime = QMimeData()
    mime.setText("[00:01.23]字")
    mime.setData(
        timing_module._SUG_INLINE_CLIPBOARD_MIME,
        "[00:01.23]字".encode(),
    )
    clipboard = _Clipboard(mime)
    monkeypatch.setattr(
        QApplication, "clipboard", staticmethod(lambda: clipboard)
    )

    pasted: list[str] = []
    editor = SimpleNamespace(
        _paste_inline_format=lambda text: pasted.append(text),
        _file_loader=SimpleNamespace(can_load_from_clipboard=lambda: False),
        _paste_chars_at_cursor=lambda _text: None,
    )

    EditorInterface._on_paste_lyrics(editor)

    assert pasted == ["[00:01.23]字"]
