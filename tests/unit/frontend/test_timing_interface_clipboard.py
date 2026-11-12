from __future__ import annotations

from types import MethodType, SimpleNamespace

from PyQt6.QtCore import QMimeData, Qt
from PyQt6.QtWidgets import QApplication

from strange_uta_game.backend.domain import Character, Sentence
from strange_uta_game.frontend.editor import timing_interface as timing_module
from strange_uta_game.frontend.editor.timing_interface import EditorInterface


class _Clipboard:
    def __init__(self, mime: QMimeData | None = None) -> None:
        self.mime = mime or QMimeData()

    def text(self) -> str:
        return self.mime.text()

    def mimeData(self) -> QMimeData:  # noqa: N801 - mirror QClipboard API
        return self.mime

    def setMimeData(self, mime: QMimeData) -> None:  # noqa: N801
        self.mime = mime

    def setText(self, text: str) -> None:  # noqa: N801 - 整体替换（含清除富 MIME）
        mime = QMimeData()
        mime.setText(text)
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
    editor = _bind_copy_helpers(
        SimpleNamespace(
            _project=SimpleNamespace(
                singers=[], sentences=[sentence], global_offset_ms=0
            ),
            preview=preview,
            _current_line_idx=0,
            tr=lambda text: text,
        )
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


# ── Ctrl+Shift+C 纯文本复制（正文，不含注音/节奏点/时间码） ──


def _bind_copy_helpers(editor):
    """把真实实例方法绑定到 SimpleNamespace 替身上（_resolve_copy_ranges
    被 _on_copy_chars / _on_copy_plain_text 以 self.xxx 调用）。"""
    editor._resolve_copy_ranges = MethodType(
        EditorInterface._resolve_copy_ranges, editor
    )
    return editor


def _plain_copy_editor(sentences, selection, multi_line=False):
    preview = SimpleNamespace(
        get_normalized_selection=lambda: selection,
        is_multi_line_selection=lambda: multi_line,
        _current_char_idx=0,
    )
    return _bind_copy_helpers(
        SimpleNamespace(
            _project=SimpleNamespace(
                singers=[], sentences=sentences, global_offset_ms=0
            ),
            preview=preview,
            _current_line_idx=0,
            tr=lambda text: text,
        )
    )


def test_copy_plain_text_current_char_strips_timestamp(monkeypatch) -> None:
    clipboard = _Clipboard()
    monkeypatch.setattr(
        QApplication, "clipboard", staticmethod(lambda: clipboard)
    )
    monkeypatch.setattr(timing_module.InfoBar, "success", lambda **_kwargs: None)

    character = Character(char="字", timestamps=[1230], check_count=1)
    sentence = Sentence(singer_id="singer", characters=[character])

    EditorInterface._on_copy_plain_text(
        _plain_copy_editor([sentence], selection=None)
    )

    # 正文纯文本：不带时间码，与富复制的 "[00:01.23]字" 相对
    assert clipboard.text() == "字"
    assert not clipboard.mimeData().hasFormat(
        timing_module._SUG_INLINE_CLIPBOARD_MIME
    )


def test_copy_plain_text_single_line_selection(monkeypatch) -> None:
    clipboard = _Clipboard()
    monkeypatch.setattr(
        QApplication, "clipboard", staticmethod(lambda: clipboard)
    )
    monkeypatch.setattr(timing_module.InfoBar, "success", lambda **_kwargs: None)

    sentence = Sentence(
        singer_id="singer",
        characters=[
            Character(char="歌", timestamps=[100], check_count=1),
            Character(char="词", timestamps=[200], check_count=1),
            Character(char="内", timestamps=[300], check_count=1),
        ],
    )

    # 划选 [0, 1] 两个字符
    EditorInterface._on_copy_plain_text(
        _plain_copy_editor([sentence], selection=(0, 0, 0, 1))
    )

    assert clipboard.text() == "歌词"
    assert not clipboard.mimeData().hasFormat(
        timing_module._SUG_INLINE_CLIPBOARD_MIME
    )


def test_copy_plain_text_multi_line_selection_joins_newline(monkeypatch) -> None:
    clipboard = _Clipboard()
    monkeypatch.setattr(
        QApplication, "clipboard", staticmethod(lambda: clipboard)
    )
    monkeypatch.setattr(timing_module.InfoBar, "success", lambda **_kwargs: None)

    line1 = Sentence(
        singer_id="singer",
        characters=[
            Character(char="甲", timestamps=[100], check_count=1),
            Character(char="乙", timestamps=[200], check_count=1),
        ],
    )
    line2 = Sentence(
        singer_id="singer",
        characters=[
            Character(char="丙", timestamps=[300], check_count=1),
            Character(char="丁", timestamps=[400], check_count=1),
        ],
    )

    # 跨行划选：第 0 行 [0,1] 到第 1 行 [0,1]
    EditorInterface._on_copy_plain_text(
        _plain_copy_editor([line1, line2], selection=(0, 0, 1, 1), multi_line=True)
    )

    assert clipboard.text() == "甲乙\n丙丁"


def test_copy_plain_text_replaces_stale_rich_mime(monkeypatch) -> None:
    """纯文本复制整体替换剪贴板，先前富复制的自定义 MIME 一并清除。

    Ctrl+V 靠 hasFormat(SUG MIME) 识别富内容；若纯文本复制不清掉旧
    MIME，下一次粘贴会把旧富内容误当本次复制的内容还原。
    """
    rich = QMimeData()
    rich.setText("[00:01.23]旧")
    rich.setData(
        timing_module._SUG_INLINE_CLIPBOARD_MIME, "[00:01.23]旧".encode()
    )
    clipboard = _Clipboard(rich)
    monkeypatch.setattr(
        QApplication, "clipboard", staticmethod(lambda: clipboard)
    )
    monkeypatch.setattr(timing_module.InfoBar, "success", lambda **_kwargs: None)

    character = Character(char="新", timestamps=[500], check_count=1)
    sentence = Sentence(singer_id="singer", characters=[character])

    EditorInterface._on_copy_plain_text(
        _plain_copy_editor([sentence], selection=None)
    )

    assert clipboard.text() == "新"
    assert not clipboard.mimeData().hasFormat(
        timing_module._SUG_INLINE_CLIPBOARD_MIME
    )


class _CopyKeyEvent:
    """Ctrl(+Shift)+C 按键事件替身（分发层测试用）。"""

    def __init__(self, *, shift: bool):
        self._key = Qt.Key.Key_C
        self._modifiers = Qt.KeyboardModifier.ControlModifier | (
            Qt.KeyboardModifier.ShiftModifier if shift
            else Qt.KeyboardModifier.NoModifier
        )
        self.accepted = False

    def key(self):
        return self._key

    def modifiers(self):
        return self._modifiers

    def nativeVirtualKey(self):
        return 0

    def nativeScanCode(self):
        return 0

    def isAutoRepeat(self):
        return False

    def accept(self):
        self.accepted = True


def test_ctrl_shift_c_dispatches_to_plain_copy() -> None:
    """硬编码分发：Ctrl+Shift+C → 纯文本复制，不落入 Ctrl+C 富复制。"""
    calls = []
    editor = SimpleNamespace(
        _timing_service=None,
        _on_copy_plain_text=lambda: calls.append("plain"),
        _on_copy_chars=lambda: calls.append("rich"),
    )

    event = _CopyKeyEvent(shift=True)
    EditorInterface._keyPressEvent_impl(editor, event)

    assert event.accepted is True
    assert calls == ["plain"]

    event = _CopyKeyEvent(shift=False)
    EditorInterface._keyPressEvent_impl(editor, event)

    assert event.accepted is True
    assert calls == ["plain", "rich"]
