"""打开 .sug 时的音频规划（保留/继承/重载）回归测试。

背景：打开 .sug 原本无条件走「清音频 + 按需重载」——即使 .sug 关联的
媒体正是引擎中已加载的那份，也会整轨重解码（波形闪烁、TSM 预渲染
重来）。现按三种情形分流（_plan_audio_keep_for_open）：

1. .sug 的 media_path == 引擎已加载音频（纯音频原样重开）→ 保留；
2. .sug 的 media_path == 已加载视频的原始路径（引擎中是提取音轨）→ 保留；
3. .sug 未关联媒体，但当前已有音频 → 继承（先音频后歌词哲学）；
4. .sug 关联了另一份媒体 → 维持原行为：清音频后重载该媒体。

保留 = 编辑器 _preserve_audio_on_project_load 置位（set_project 不清
音频并同步引擎时长）+ store 继承 audio_path + _apply_project_extras
跳过重载只恢复路径。
"""

from __future__ import annotations

from pathlib import Path

from strange_uta_game.backend.domain import Project
from strange_uta_game.frontend.editor.timing import file_loader as file_loader_mod
from strange_uta_game.frontend.editor.timing.file_loader import FileLoader, _same_file


class _StoreStub:
    def __init__(self, audio_path=None, media_path=None):
        self.audio_path = audio_path
        self.original_media_path = media_path
        self.working_dir = ""
        self.load_calls = []
        self.restored_media = []
        self.working_dirs = []

    def load_project(self, project, save_path=None, audio_path=None):
        self.load_calls.append((project, save_path, audio_path))
        self.audio_path = audio_path
        self.original_media_path = None

    def restore_media_path(self, path):
        self.restored_media.append(path)
        self.original_media_path = path

    def set_working_dir(self, file_path):
        self.working_dirs.append(file_path)


class _EditorStub:
    def __init__(self, store, audio_file=None):
        self._project = None
        self._store = store
        self._audio_file_path = audio_file
        self.load_audio_calls = []

    def tr(self, text):
        return text

    def _get_setting_interface(self):
        return None

    def load_audio(self, file_path):
        self.load_audio_calls.append(file_path)
        return True


def _make_loader(store, audio_file=None):
    editor = _EditorStub(store, audio_file=audio_file)
    loader = FileLoader(editor)
    loader._record_recent_project = lambda file_path: None
    loader._notify_main_window_frameless_refresh = lambda: None
    video_calls = []
    loader._load_video_as_audio = lambda p: video_calls.append(p)
    return loader, editor, video_calls


def _open(loader, project=None, extras=None, file_path=r"C:\proj\song.sug"):
    project = project or Project()
    loader._on_project_loaded(project, file_path, extras or {})
    return project


# ── _same_file ───────────────────────────────────────────────────────────


def test_same_file_is_case_and_separator_insensitive():
    assert _same_file(r"C:\Songs\A.mp3", "c:/songs/a.MP3")
    assert not _same_file(r"C:\Songs\A.mp3", r"C:\Songs\B.mp3")
    assert not _same_file(None, r"C:\a.mp3")
    assert not _same_file("", "")


# ── 情形 1：纯音频原样重开 → 保留，不重载 ────────────────────────────────


def test_open_sug_keeps_audio_when_media_is_loaded_audio(tmp_path):
    media = str(tmp_path / "song.mp3")
    Path(media).write_bytes(b"")
    store = _StoreStub(audio_path=media, media_path=media)
    loader, editor, video_calls = _make_loader(store, audio_file=media)

    _open(loader, extras={"media_path": media})

    assert editor._preserve_audio_on_project_load is True
    assert store.load_calls[0][2] == media  # store 继承音频路径
    assert editor.load_audio_calls == []  # 不重载
    assert video_calls == []
    assert store.restored_media == [media, media]  # 替换后 + extras 各一次
    assert loader._audio_kept_for_open is False  # 一次性标记已消费


# ── 情形 2：视频项目重开（引擎中是提取音轨）→ 保留，不重新提取 ──────────


def test_open_sug_keeps_extracted_audio_when_media_is_source_video(tmp_path):
    video = str(tmp_path / "song.mp4")
    Path(video).write_bytes(b"")
    cache_audio = str(tmp_path / "cache" / "song.m4a")
    store = _StoreStub(audio_path=cache_audio, media_path=video)
    loader, editor, video_calls = _make_loader(store, audio_file=cache_audio)

    _open(loader, extras={"media_path": video})

    assert editor._preserve_audio_on_project_load is True
    assert store.load_calls[0][2] == cache_audio  # 继承提取音轨路径
    assert video_calls == []  # 不重新走 FFmpeg 提取
    assert editor.load_audio_calls == []
    assert store.original_media_path == video


# ── 情形 3：.sug 未关联媒体 + 当前已有音频 → 继承 ─────────────────────────


def test_open_sug_without_media_inherits_current_audio():
    store = _StoreStub(audio_path=r"C:\a.mp3", media_path=r"C:\a.mp3")
    loader, editor, _ = _make_loader(store, audio_file=r"C:\a.mp3")

    _open(loader, extras={})  # 旧版 sug：无 media_path 字段

    assert editor._preserve_audio_on_project_load is True
    assert store.load_calls[0][2] == r"C:\a.mp3"
    assert store.restored_media == [r"C:\a.mp3"]
    assert editor.load_audio_calls == []


# ── 情形 4：.sug 关联另一份媒体 → 原行为：清音频 + 重载 ──────────────────


def test_open_sug_with_different_media_clears_and_reloads(tmp_path):
    new_media = str(tmp_path / "new.mp3")
    Path(new_media).write_bytes(b"")
    store = _StoreStub(audio_path=r"C:\old.mp3", media_path=r"C:\old.mp3")
    loader, editor, _ = _make_loader(store, audio_file=r"C:\old.mp3")

    _open(loader, extras={"media_path": new_media})

    assert not getattr(editor, "_preserve_audio_on_project_load", False)
    assert store.load_calls[0][2] is None  # 音频上下文随项目替换重置
    assert editor.load_audio_calls == [new_media]  # 重载 .sug 关联的媒体


def test_open_sug_without_media_and_without_audio_keeps_nothing():
    store = _StoreStub()
    loader, editor, _ = _make_loader(store)

    _open(loader, extras={})

    assert not getattr(editor, "_preserve_audio_on_project_load", False)
    assert store.load_calls[0][2] is None
    assert editor.load_audio_calls == []


def test_open_sug_missing_media_file_warns_and_skips_load(tmp_path, monkeypatch):
    gone = str(tmp_path / "gone.mp3")  # 不创建，模拟文件被移动/删除
    store = _StoreStub()
    loader, editor, _ = _make_loader(store)

    bars = []
    monkeypatch.setattr(
        file_loader_mod, "InfoBar",
        type("InfoBarStub", (), {
            "warning": staticmethod(lambda **kw: bars.append(kw)),
            "error": staticmethod(lambda **kw: bars.append(kw)),
            "success": staticmethod(lambda **kw: bars.append(kw)),
        }),
    )

    _open(loader, extras={"media_path": gone})

    assert len(bars) == 1  # 「媒体文件未找到」警告
    assert editor.load_audio_calls == []
    assert not getattr(editor, "_preserve_audio_on_project_load", False)


# ── 已保留音频时磁盘文件缺失：引擎仍持有音频，不弹未找到 ────────────────


def test_open_sug_kept_audio_skips_missing_file_check(tmp_path, monkeypatch):
    # 媒体路径与已加载音频一致但磁盘文件已被移走：引擎内仍有该音频，
    # 应保留并跳过「未找到」告警与重载
    media = str(tmp_path / "song.mp3")  # 故意不创建文件
    store = _StoreStub(audio_path=media, media_path=media)
    loader, editor, _ = _make_loader(store, audio_file=media)

    bars = []
    monkeypatch.setattr(
        file_loader_mod, "InfoBar",
        type("InfoBarStub", (), {
            "warning": staticmethod(lambda **kw: bars.append(kw)),
            "error": staticmethod(lambda **kw: bars.append(kw)),
            "success": staticmethod(lambda **kw: bars.append(kw)),
        }),
    )

    _open(loader, extras={"media_path": media})

    assert bars == []
    assert editor._preserve_audio_on_project_load is True
    assert editor.load_audio_calls == []


# ── 媒体存在性探测：后台结果优先 / UNC 免主线程 stat ────────────────────


def _stub_infobars(monkeypatch):
    bars = []
    monkeypatch.setattr(
        file_loader_mod, "InfoBar",
        type("InfoBarStub", (), {
            "warning": staticmethod(lambda **kw: bars.append(kw)),
            "error": staticmethod(lambda **kw: bars.append(kw)),
            "success": staticmethod(lambda **kw: bars.append(kw)),
        }),
    )
    return bars


def test_open_sug_uses_background_media_exists_verdict(tmp_path, monkeypatch):
    # 后台线程带回 media_exists=False：即使主线程 stat 该文件其实存在
    # （模拟探测与消费之间存在竞态的保守面），也必须直接消费布尔值，
    # 不做任何主线程探测、不发起加载。
    media = str(tmp_path / "song.mp3")
    Path(media).write_bytes(b"x")  # 故意创建：若消费侧错误地 stat 会走到加载
    store = _StoreStub()
    loader, editor, _ = _make_loader(store)
    bars = _stub_infobars(monkeypatch)

    _open(loader, extras={"media_path": media, "media_exists": False})

    assert len(bars) == 1  # 「媒体文件未找到」警告
    assert editor.load_audio_calls == []


def test_open_sug_unc_media_without_verdict_skips_stat(monkeypatch):
    # UNC 网络路径且无后台探测结果：跳过主线程 exists()（不可达 SMB 宿主
    # 机会阻塞二十余秒），按「尝试加载」交给异步链，不弹未找到。
    unc = "//192.168.0.99/share/song.mp3"
    store = _StoreStub()
    loader, editor, _ = _make_loader(store)
    bars = _stub_infobars(monkeypatch)

    _open(loader, extras={"media_path": unc})

    assert bars == []
    assert editor.load_audio_calls == [unc]


def test_open_sug_local_media_without_verdict_stats(tmp_path, monkeypatch):
    # 本地路径且无探测结果：维持旧行为——同步探测（本地 ~0ms），
    # 存在则加载。
    media = str(tmp_path / "song.mp3")
    Path(media).write_bytes(b"x")
    store = _StoreStub()
    loader, editor, _ = _make_loader(store)
    bars = _stub_infobars(monkeypatch)

    _open(loader, extras={"media_path": media})

    assert bars == []
    assert editor.load_audio_calls == [media]
