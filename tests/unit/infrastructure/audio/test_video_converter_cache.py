"""video_converter 提取缓存修复回归测试（D1 / D13）。

D1：clear_extracted_cache 旧实现整目录清扫，会误删已被项目持久引用的
    其它视频提取音频，且产物按 stem 命名——不同目录同名视频互相覆盖。
    修复：产物名改内容指纹（路径+mtime），clear 只清自己的临时分片。
D13：ffmpeg 命令缺 -nostdin，损坏文件可挂住至 600s 超时。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from strange_uta_game.backend.infrastructure.audio import video_converter
from strange_uta_game.backend.infrastructure.audio.video_converter import (
    _extracted_cache_name,
    audio_display_name,
    audio_display_stem,
    clear_extracted_cache,
    extract_audio,
    strip_extracted_fingerprint,
)


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """SUG_CACHE_DIR 重定向到临时目录（app_dirs.cache_dir() 调用时求值）。

    返回 video_converter 实际使用的 extracted 子目录。
    """
    monkeypatch.setenv("SUG_CACHE_DIR", str(tmp_path / "cache"))
    return video_converter._get_cache_dir()


@pytest.fixture
def tiny_wav(tmp_path) -> str:
    """1s 单声道测试音频。"""
    sr = 44100
    t = np.linspace(0, 1.0, sr, endpoint=False)
    p = tmp_path / "song_a.wav"
    sf.write(str(p), (np.sin(2 * np.pi * 440 * t) * 0.2).astype(np.float32), sr)
    return str(p)


class TestExtractedCacheName:
    def test_same_video_maps_to_stable_name(self, tiny_wav):
        """同一路径（mtime 不变）重复请求得到同一文件名。"""
        n1 = _extracted_cache_name(tiny_wav)
        n2 = _extracted_cache_name(tiny_wav)
        assert n1 == n2
        assert n1.startswith(Path(tiny_wav).stem + "_")
        assert n1.endswith(".mp3")

    def test_same_stem_different_dirs_do_not_collide(self, tmp_path):
        """不同目录的同名视频指纹不同（旧实现按 stem 命名会互相覆盖）。"""
        p1 = tmp_path / "a" / "clip.mp4"
        p2 = tmp_path / "b" / "clip.mp4"
        p1.parent.mkdir()
        p2.parent.mkdir()
        p1.write_bytes(b"x")
        p2.write_bytes(b"x")
        assert _extracted_cache_name(str(p1)) != _extracted_cache_name(str(p2))

    def test_replaced_content_gets_new_name(self, tiny_wav):
        """文件内容被替换（mtime 变化）→ 新指纹，不复用过期音频。"""
        import os

        n1 = _extracted_cache_name(tiny_wav)
        st = os.stat(tiny_wav)
        os.utime(tiny_wav, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        assert _extracted_cache_name(tiny_wav) != n1


class TestAudioDisplayName:
    """提取产物的指纹后缀不应混进用户可见的文件名（保存名/导出名/展示名）。"""

    def test_strip_is_inverse_of_cache_name(self, tiny_wav):
        """去指纹是 _extracted_cache_name 的逆操作：还原原始 stem。"""
        product_stem = Path(_extracted_cache_name(tiny_wav)).stem
        assert product_stem != Path(tiny_wav).stem
        assert strip_extracted_fingerprint(product_stem) == Path(tiny_wav).stem

    @pytest.mark.parametrize(
        "stem",
        [
            "song_a",  # 旧命名产物，本就没有指纹
            "song_abcdef12345",  # 只有 11 位
            "song_ABCDEF123456",  # 指纹恒为小写十六进制
            "song_abcdef12345g",  # 含非十六进制字符
            "songabcdef123456",  # 缺分隔下划线
        ],
    )
    def test_strip_leaves_non_fingerprint_stem_alone(self, stem):
        assert strip_extracted_fingerprint(stem) == stem

    def test_strip_only_removes_trailing_fingerprint(self):
        """原始视频名自身以指纹样式结尾时，只去掉最后追加的那一段。"""
        assert (
            strip_extracted_fingerprint("clip_0123456789ab_abcdef123456")
            == "clip_0123456789ab"
        )

    def test_extracted_product_loses_fingerprint(self, cache_dir):
        product = cache_dir / "my video_abcdef123456.mp3"
        product.write_bytes(b"mp3-data")

        assert audio_display_stem(str(product)) == "my video"
        assert audio_display_name(str(product)) == "my video.mp3"

    def test_legacy_product_name_unchanged(self, cache_dir):
        legacy = cache_dir / "song_a.mp3"
        legacy.write_bytes(b"old-legacy")

        assert audio_display_stem(str(legacy)) == "song_a"
        assert audio_display_name(str(legacy)) == "song_a.mp3"

    def test_user_file_outside_cache_is_untouched(self, cache_dir, tmp_path):
        """用户自己的音频即便以指纹样式结尾也不能被改名。"""
        user_file = tmp_path / "music" / "song_0123456789ab.mp3"
        user_file.parent.mkdir()
        user_file.write_bytes(b"user-audio")

        assert audio_display_stem(str(user_file)) == "song_0123456789ab"
        assert audio_display_name(str(user_file)) == "song_0123456789ab.mp3"


class TestClearExtractedCache:
    def test_clears_only_part_files(self, cache_dir):
        """只清 *.part-* 临时分片，绝不动已完成的提取产物（项目持久音频）。"""
        product = cache_dir / "song_a_abcdef123456.mp3"
        product.write_bytes(b"mp3-data")
        leftover = cache_dir / "song_b_abcdef654321.mp3.part-999"
        leftover.write_bytes(b"partial")
        unrelated = cache_dir / "clip_bass_fallback.wav"
        unrelated.write_bytes(b"wav")

        clear_extracted_cache()

        assert product.is_file(), "提取产物被误删"
        assert unrelated.is_file(), "非提取管线的文件被误删"
        assert not leftover.exists(), "临时分片未被清理"

    def test_missing_dir_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUG_CACHE_DIR", str(tmp_path / "nonexistent"))
        clear_extracted_cache()  # 不应抛错


class TestExtractAudio:
    def test_cmd_has_nostdin_and_part_output(self, tiny_wav, cache_dir, monkeypatch):
        """D13：命令含 -nostdin；输出走 .part- 临时分片再原子改名。"""
        captured = {}

        class _FakeResult:
            returncode = 0
            stderr = b""

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            # 模拟 ffmpeg 成功写出分片文件
            out = Path(cmd[-1])
            out.write_bytes(b"fake-mp3")
            return _FakeResult()

        monkeypatch.setattr(video_converter, "is_ffmpeg_available", lambda: True)
        monkeypatch.setattr(video_converter.subprocess, "run", fake_run)

        result = extract_audio(tiny_wav)

        cmd = captured["cmd"]
        assert "-nostdin" in cmd, "ffmpeg 命令缺少 -nostdin"
        # -nostdin 必须在 -i 之前（ffmpeg 全局参数）
        assert cmd.index("-nostdin") < cmd.index("-i")
        # 分片扩展名无法推断容器，输出前必须显式 -f mp3
        assert cmd[-3] == "-f" and cmd[-2] == "mp3"
        assert ".part-" in cmd[-1]
        # 临时分片已被原子改名为稳定产物名
        assert Path(result).is_file()
        assert result == str(cache_dir / Path(result).name)
        assert not list(cache_dir.glob("*.part-*"))

    def test_failed_extraction_leaves_no_partial_product(
        self, tiny_wav, cache_dir, monkeypatch
    ):
        """提取失败：不留同名截断产物，只有可被下次清理的分片残留。"""

        class _FakeResult:
            returncode = 1
            stderr = b"boom"

        monkeypatch.setattr(video_converter, "is_ffmpeg_available", lambda: True)
        monkeypatch.setattr(
            video_converter.subprocess,
            "run",
            lambda cmd, **kwargs: _FakeResult(),
        )

        with pytest.raises(RuntimeError, match="FFmpeg 提取失败"):
            extract_audio(tiny_wav)

        assert not list(cache_dir.glob("*.mp3")), "失败产物不应占用最终文件名"

    @pytest.mark.skipif(
        not video_converter.is_ffmpeg_available(),
        reason="需要系统可用的 ffmpeg",
    )
    def test_real_extraction_roundtrip(self, tiny_wav, cache_dir):
        """真实 ffmpeg 提取：产物落在指纹名下，重复提取覆盖同一文件。"""
        p1 = extract_audio(tiny_wav)
        assert Path(p1).is_file()
        assert p1 == str(cache_dir / Path(p1).name)
        assert Path(p1).suffix == ".mp3"

        p2 = extract_audio(tiny_wav)
        assert p2 == p1, "同一路径的重复提取应复用同一产物名"

    @pytest.mark.skipif(
        not video_converter.is_ffmpeg_available(),
        reason="需要系统可用的 ffmpeg",
    )
    def test_real_extraction_corrupt_input_fails_cleanly(self, cache_dir):
        """D13 实证：损坏输入在 -nostdin 下快速失败，不挂住至超时。"""
        corrupt = cache_dir / "corrupt.mp4"
        corrupt.write_bytes(b"\x00" * 4096)
        with pytest.raises(RuntimeError):
            extract_audio(str(corrupt))


class TestLegacyAndTtlCleanup:
    """旧命名残留与 extracted/ 只增不减的清理。

    内容指纹改名前产物按 ``<stem>.mp3`` 命名，改名后旧名文件永不再被覆盖；
    指纹产物也不会被同名覆盖（mtime 变化 → 新名）。两者都只会在 extracted/
    里堆积——提取成功后按当前视频 stem 清旧名残留，并对整个目录做 mtime
    30 天的有界清理（单次 listdir）；引擎正在播放的源文件（keep_path）跳过。
    """

    @staticmethod
    def _stub_ffmpeg_ok(monkeypatch):
        class _FakeResult:
            returncode = 0
            stderr = b""

        def fake_run(cmd, **kwargs):
            # 模拟 ffmpeg 成功写出分片文件
            Path(cmd[-1]).write_bytes(b"fake-mp3")
            return _FakeResult()

        monkeypatch.setattr(video_converter, "is_ffmpeg_available", lambda: True)
        monkeypatch.setattr(video_converter.subprocess, "run", fake_run)

    def test_legacy_stem_file_removed_after_extraction(
        self, tiny_wav, cache_dir, monkeypatch
    ):
        legacy = cache_dir / "song_a.mp3"
        legacy.write_bytes(b"old-legacy")
        self._stub_ffmpeg_ok(monkeypatch)

        result = extract_audio(tiny_wav)

        assert not legacy.exists(), "同 stem 的旧命名产物未被清理"
        assert Path(result).is_file(), "新指纹产物缺失"

    def test_legacy_file_kept_when_it_is_keep_path(
        self, tiny_wav, cache_dir, monkeypatch
    ):
        """旧名文件正是引擎正在播放的源文件（keep_path）时不得删除。"""
        legacy = cache_dir / "song_a.mp3"
        legacy.write_bytes(b"old-legacy-in-use")
        self._stub_ffmpeg_ok(monkeypatch)

        extract_audio(tiny_wav, keep_path=str(legacy))

        assert legacy.exists(), "keep_path（正在播放的源文件）被误删"

    def test_ttl_prune_removes_stale_keeps_fresh(
        self, tiny_wav, cache_dir, monkeypatch
    ):
        stale = cache_dir / "stale_abcdef123456.mp3"
        stale.write_bytes(b"stale")
        fresh = cache_dir / "fresh_abcdef123456.mp3"
        fresh.write_bytes(b"fresh")
        past = time.time() - 31 * 86400  # 超过 30 天保留期
        os.utime(stale, (past, past))
        self._stub_ffmpeg_ok(monkeypatch)

        extract_audio(tiny_wav)

        assert not stale.exists(), "超过保留期的产物未被清理"
        assert fresh.exists(), "保留期内的产物被误删"

    def test_ttl_prune_skips_playing_source(
        self, tiny_wav, cache_dir, monkeypatch
    ):
        """超期但正在播放的源文件（keep_path）跳过清理。"""
        playing = cache_dir / "playing_abcdef123456.mp3"
        playing.write_bytes(b"in-use")
        past = time.time() - 40 * 86400
        os.utime(playing, (past, past))
        self._stub_ffmpeg_ok(monkeypatch)

        extract_audio(tiny_wav, keep_path=str(playing))

        assert playing.exists(), "正在播放的源文件被 TTL 清理误删"
