"""ProjectStore 由音频名派生项目名的回归测试。

视频提取的缓存产物名为 ``<原名>_<12 位指纹>.mp3``。未命名项目的建议保存名
与备份名都从音频名派生，指纹后缀不应混进这些用户可见的文件名。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication

from strange_uta_game.backend.infrastructure.audio import video_converter
from strange_uta_game.frontend.project_store import ProjectStore


def _app():
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def extracted_dir(tmp_path, monkeypatch):
    """SUG_CACHE_DIR 重定向到临时目录，返回提取产物所在的 extracted 子目录。"""
    monkeypatch.setenv("SUG_CACHE_DIR", str(tmp_path / "cache"))
    return video_converter._get_cache_dir()


@pytest.fixture
def no_default_dirs(monkeypatch):
    """AppSettings 一律回放默认值（不读真实 config.json）。"""

    class _FakeSettings:
        def get(self, key, default=None):
            return default

    monkeypatch.setattr(
        "strange_uta_game.frontend.settings.app_settings.AppSettings",
        _FakeSettings,
    )


def _store_with_audio(audio_path: Path) -> ProjectStore:
    _ = _app()
    store = ProjectStore()
    store._project = object()
    store._audio_path = str(audio_path)
    return store


def test_suggested_save_name_drops_extracted_fingerprint(
    extracted_dir, no_default_dirs
):
    product = extracted_dir / "my video_abcdef123456.mp3"
    product.write_bytes(b"mp3-data")
    store = _store_with_audio(product)

    assert Path(store.suggested_save_path()).name == "my video.sug"


def test_backup_base_name_drops_extracted_fingerprint(extracted_dir, no_default_dirs):
    product = extracted_dir / "my video_abcdef123456.mp3"
    product.write_bytes(b"mp3-data")
    store = _store_with_audio(product)

    assert store._backup_base_name() == "my video"


def test_user_audio_with_fingerprint_like_name_is_kept(
    extracted_dir, no_default_dirs, tmp_path
):
    """缓存目录之外的用户音频不做去指纹处理。"""
    audio_dir = tmp_path / "music"
    audio_dir.mkdir()
    user_file = audio_dir / "song_0123456789ab.mp3"
    user_file.write_bytes(b"user-audio")
    store = _store_with_audio(user_file)

    assert Path(store.suggested_save_path()).name == "song_0123456789ab.sug"
    assert store._backup_base_name() == "song_0123456789ab"
