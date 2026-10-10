"""加载歌词的「空项目直通 + 音频继承」例外（回归）。

背景：加载歌词 = 新建项目 + 装入歌词，会整体替换当前项目并清理音频
（见 file_loader._prepare_fresh_project_for_lyrics / timing_interface.
set_project）。本组测试固定两个例外：

1. 当前项目没有歌词行 → 视为空项目（典型是「先加载音频，再加载歌词」
   的中间态），没有可被覆盖丢失的歌词内容，跳过未保存检测弹窗；
2. 该空项目上已加载音频 → 音频大概率就是为这批新歌词准备的，直接
   继承到新项目：store 音频/原始媒体路径保留，编辑器保留音频标记
   （_preserve_audio_on_project_load）置位，set_project 消费标记后
   不清音频并把引擎时长带入新项目。

有歌词的项目维持原行为：未保存检测照常弹窗，用户取消则中止。
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace

from strange_uta_game.backend.domain import Project, Sentence, Singer
from strange_uta_game.frontend.editor.timing import file_loader as file_loader_mod
from strange_uta_game.frontend.editor.timing.file_loader import FileLoader
from strange_uta_game.frontend.editor.timing_interface import EditorInterface


# ── 桩 ───────────────────────────────────────────────────────────────────


class _StoreStub:
    """记录 load_project/restore_media_path 调用的 store 桩。

    audio_path/original_media_path 语义与真实 ProjectStore 一致：
    load_project 时按参数重置音频上下文。
    """

    def __init__(self, audio_path=None, media_path=None):
        self.audio_path = audio_path
        self.original_media_path = media_path
        self.working_dir = ""
        self.load_calls = []
        self.restored_media = []

    def load_project(self, project, save_path=None, audio_path=None):
        self.load_calls.append((project, save_path, audio_path))
        self.audio_path = audio_path
        self.original_media_path = None

    def restore_media_path(self, path):
        self.restored_media.append(path)
        self.original_media_path = path


class _EditorStub:
    def __init__(self, project, store):
        self._project = project
        self._store = store
        self._audio_file_path = None
        self.calls = []

    def tr(self, text):
        return text

    def _get_setting_interface(self):
        return None


def _make_lyrics_project() -> Project:
    project = Project()
    singer = Singer(name="default")
    project.add_singer(singer)
    project.add_sentence(Sentence(singer_id=singer.id))
    return project


def _make_loader(project, store, *, unsaved_result=True):
    """构造 FileLoader 并桩掉外部副作用（未保存弹窗/nicokara 重置）。"""
    editor = _EditorStub(project, store)
    loader = FileLoader(editor)
    loader.check_unsaved_changes = lambda: unsaved_result
    loader._reset_nicokara_tags_to_defaults = lambda: None
    return loader, editor


def _make_empty_project() -> Project:
    return Project()


# ── _prepare_fresh_project_for_lyrics：空项目直通 + 音频继承 ────────────


def test_prepare_skips_unsaved_dialog_when_project_has_no_lyrics():
    # 无歌词 + 无音频：空项目直接放行，不弹未保存检测
    store = _StoreStub()
    loader, editor = _make_loader(_make_empty_project(), store)
    called = []
    loader.check_unsaved_changes = lambda: called.append(1) or True

    assert loader._prepare_fresh_project_for_lyrics(check_unsaved=True) is True
    assert called == []  # 未保存检测被跳过
    assert store.load_calls, "应以全新项目替换"
    assert not getattr(editor, "_preserve_audio_on_project_load", False)
    assert store.load_calls[0][2] is None  # 无音频可继承


def test_prepare_inherits_audio_when_no_lyrics_and_audio_loaded():
    # 无歌词 + 有音频（先音频后歌词中间态）：免弹窗，音频上下文继承到新项目
    store = _StoreStub(audio_path=r"C:\song.mp3", media_path=r"C:\song.mp3")
    loader, editor = _make_loader(_make_empty_project(), store)
    editor._audio_file_path = r"C:\song.mp3"
    called = []
    loader.check_unsaved_changes = lambda: called.append(1) or True

    assert loader._prepare_fresh_project_for_lyrics(check_unsaved=True) is True
    assert called == []  # 未保存检测被跳过
    # store 音频/原始媒体路径带入新项目
    assert store.load_calls[0][2] == r"C:\song.mp3"
    assert store.restored_media == [r"C:\song.mp3"]
    assert store.audio_path == r"C:\song.mp3"
    assert store.original_media_path == r"C:\song.mp3"
    # 编辑器保留音频标记置位，供 set_project 消费
    assert editor._preserve_audio_on_project_load is True


def test_prepare_detects_audio_from_store_path_only():
    # 引擎路径缺失但 store 有音频路径（如视频提取刚完成）同样继承
    store = _StoreStub(audio_path=r"C:\cache\song.m4a", media_path=r"C:\song.mp4")
    loader, editor = _make_loader(_make_empty_project(), store)

    assert loader._prepare_fresh_project_for_lyrics() is True
    assert editor._preserve_audio_on_project_load is True
    assert store.load_calls[0][2] == r"C:\cache\song.m4a"
    assert store.restored_media == [r"C:\song.mp4"]


def test_prepare_prompts_unsaved_when_project_has_lyrics():
    # 有歌词：未保存检测照常执行；用户取消 → 中止且不替换项目
    store = _StoreStub()
    loader, _ = _make_loader(_make_lyrics_project(), store, unsaved_result=False)

    assert loader._prepare_fresh_project_for_lyrics(check_unsaved=True) is False
    assert store.load_calls == []


def test_prepare_with_lyrics_never_inherits_audio_even_if_check_disabled():
    # 有歌词 + 有音频：即使免检测（调用方已检测过），音频仍不继承——
    # 旧音频属于旧歌，随项目替换一并清理
    store = _StoreStub(audio_path=r"C:\old.mp3", media_path=r"C:\old.mp3")
    loader, editor = _make_loader(_make_lyrics_project(), store)
    editor._audio_file_path = r"C:\old.mp3"
    called = []
    loader.check_unsaved_changes = lambda: called.append(1) or True

    assert loader._prepare_fresh_project_for_lyrics(check_unsaved=False) is True
    assert called == []  # check_unsaved=False 不触发检测
    assert not getattr(editor, "_preserve_audio_on_project_load", False)
    assert store.load_calls[0][2] is None
    assert store.restored_media == []


def test_prepare_no_store_degenerate_path_unchanged():
    # 无 store 退化路径：已有项目时沿用旧行为直通（不新建项目）
    editor = _EditorStub(_make_empty_project(), None)
    loader = FileLoader(editor)
    assert loader._prepare_fresh_project_for_lyrics() is True
    assert editor._project is not None


# ── create_fresh_project：默认仍重置音频上下文 ──────────────────────────


def test_create_fresh_project_default_resets_audio_context():
    store = _StoreStub(audio_path=r"C:\old.mp3", media_path=r"C:\old.mp3")
    loader, _ = _make_loader(_make_lyrics_project(), store)

    loader.create_fresh_project()
    assert store.load_calls[0][2] is None
    assert store.restored_media == []
    assert store.audio_path is None
    assert store.original_media_path is None


# ── prompt_load_lyrics：弹文件框前的检测同样遵循空项目例外 ─────────────


def _run_prompt_load_lyrics(loader, monkeypatch, tmp_path):
    lrc = tmp_path / "lyrics.lrc"
    lrc.write_text("[00:01.00]测试", encoding="utf-8")
    monkeypatch.setattr(
        file_loader_mod.QFileDialog, "getOpenFileName",
        staticmethod(lambda *a, **k: (str(lrc), "")),
    )
    loaded = []
    loader.load_lyrics = lambda path, check_unsaved=True: loaded.append(
        (path, check_unsaved)
    )
    loader._save_last_dir = lambda file_path: None
    loader._notify_main_window_frameless_refresh = lambda: None
    loader.prompt_load_lyrics()
    return loaded


def test_prompt_load_lyrics_skips_unsaved_check_for_empty_project(tmp_path, monkeypatch):
    store = _StoreStub(audio_path=r"C:\song.mp3")
    loader, _ = _make_loader(_make_empty_project(), store)
    called = []
    loader.check_unsaved_changes = lambda: called.append(1) or True

    loaded = _run_prompt_load_lyrics(loader, monkeypatch, tmp_path)
    assert called == []  # 空项目不先弹未保存检测
    assert len(loaded) == 1  # 文件框直通，选完直接加载


def test_prompt_load_lyrics_checks_unsaved_when_project_has_lyrics(tmp_path, monkeypatch):
    store = _StoreStub()
    loader, _ = _make_loader(_make_lyrics_project(), store)
    called = []
    loader.check_unsaved_changes = lambda: called.append(1) or True

    loaded = _run_prompt_load_lyrics(loader, monkeypatch, tmp_path)
    assert called == [1]  # 有歌词照常检测
    assert len(loaded) == 1


def test_prompt_load_lyrics_aborts_when_user_cancels_unsaved(tmp_path, monkeypatch):
    store = _StoreStub()
    loader, _ = _make_loader(_make_lyrics_project(), store, unsaved_result=False)

    loaded = _run_prompt_load_lyrics(loader, monkeypatch, tmp_path)
    assert loaded == []  # 用户取消 → 不弹文件框、不加载


# ── EditorInterface.set_project：保留音频标记的消费 ─────────────────────


def _rec(calls, name):
    def _fn(*args, **kwargs):
        calls.append((name, args, kwargs))
    return _fn


def _make_set_project_stub(previous_project, *, flag, calls):
    """提供 set_project 触达的全部属性/方法的最小桩。"""
    stub = SimpleNamespace(
        _project=previous_project,
        _preserve_audio_on_project_load=flag,
        _timing_service=SimpleNamespace(
            get_duration_ms=lambda: 43210,
            get_current_position=lambda: "cp",
        ),
        _sync_project_audio_duration=_rec(calls, "sync_duration"),
        _get_setting_interface=lambda: None,
        preview=SimpleNamespace(
            set_global_offset=_rec(calls, "preview.set_global_offset"),
            set_project=_rec(calls, "preview.set_project"),
        ),
        toolbar=SimpleNamespace(edit_offset=SimpleNamespace(
            blockSignals=lambda *_: None,
            setText=lambda *_: None,
        )),
        _apply_checkpoint_position=_rec(calls, "apply_checkpoint"),
        _update_time_tags_display=_rec(calls, "update_timetags"),
        _update_status=_rec(calls, "update_status"),
        _apply_settings=_rec(calls, "apply_settings"),
        _clear_audio_state=_rec(calls, "clear_audio_state"),
    )
    return stub


def _names(calls):
    return [name for name, _, _ in calls]


def test_set_project_preserves_audio_when_flag_set():
    # 替换已有项目 + 继承标记：不清音频，引擎时长带入新项目，标记复位
    calls = []
    stub = _make_set_project_stub(
        _make_lyrics_project(), flag=True, calls=calls
    )
    new_project = Project()

    EditorInterface.set_project(stub, new_project)

    assert "clear_audio_state" not in _names(calls)
    sync = [c for c in calls if c[0] == "sync_duration"]
    assert sync and sync[0][1] == (43210,) and sync[0][2] == {"mark_dirty": False}
    assert stub._preserve_audio_on_project_load is False  # 一次性标记已消费
    assert stub._project is new_project


def test_set_project_clears_audio_without_flag():
    # 替换已有项目且无标记：维持原行为，清音频、不同步时长
    calls = []
    stub = _make_set_project_stub(
        _make_lyrics_project(), flag=False, calls=calls
    )

    EditorInterface.set_project(stub, Project())

    assert "clear_audio_state" in _names(calls)
    assert "sync_duration" not in _names(calls)
    assert stub._preserve_audio_on_project_load is False


def test_set_project_first_project_keeps_audio():
    # 首次设置项目（原为 None）：保留音频 + 同步时长（原有工作流，回归）
    calls = []
    stub = _make_set_project_stub(None, flag=False, calls=calls)

    EditorInterface.set_project(stub, Project())

    assert "clear_audio_state" not in _names(calls)
    assert "sync_duration" in _names(calls)


def test_set_project_consumes_stale_flag_safely():
    # 标记是消费即复位：第二次 set_project（无标记）回到清理行为
    calls = []
    stub = _make_set_project_stub(
        _make_empty_project(), flag=True, calls=calls
    )
    EditorInterface.set_project(stub, Project())
    assert "clear_audio_state" not in _names(calls)

    calls.clear()
    EditorInterface.set_project(stub, Project())
    assert "clear_audio_state" in _names(calls)
