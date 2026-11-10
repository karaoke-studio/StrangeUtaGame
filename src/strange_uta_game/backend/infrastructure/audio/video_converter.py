"""视频音频提取模块。

使用 FFmpeg 命令行从视频/音频文件中提取音频，压缩为 MP3 临时文件。
FFmpeg 路径优先使用用户在「设置-关于/语言」中配置的路径，未配置则使用环境变量。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from strange_uta_game import app_dirs

LoadProgressCallback = Callable[[str, float], None]  # (stage, 0.0~1.0)

VIDEO_EXTENSIONS = {
    # 常见视频容器
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".ts", ".3gp", ".vob", ".mts", ".m2ts",
    ".rm", ".rmvb", ".asf", ".f4v", ".ogv",
    # 仍需 FFmpeg 的音频容器（BASS 无对应插件）
    ".dts",
}

_MP3_QUALITY = 128  # kbps
_TARGET_SAMPLE_RATE = 44100  # Hz
_CACHE_DIR_NAME = ".cache"
_EXTRACTED_TTL_DAYS = 30  # 提取产物保留期（天）：mtime 超过即按陈旧缓存清理


def _get_cache_dir() -> Path:
    """获取提取音频的存放目录（缓存根目录下的 extracted 子文件夹）。

    缓存根目录解析见 :mod:`strange_uta_game.app_dirs`（与 tsm_cache / project_store
    同源：``SUG_CACHE_DIR`` 最高优先，macOS 用 ``~/Library/Caches``，其余用程序目录
    下的 ``.cache``）。

    注意：必须放在缓存根目录的 extracted 子目录里，而不是根目录。TSM 引擎加载时会调用
    clear_cache() 用 glob("*.mp3") 非递归删除缓存根目录下的所有 mp3——
    若提取音频直接放在根目录，加载视频后切换引擎重载时该文件已被删除，导致
    "找不到 ffmpeg 提取的音频文件"。放到子目录可避开这次清理，使其在切换引擎/重载时
    仍然有效。
    """
    cache_dir = app_dirs.cache_dir() / "extracted"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def is_video_file(file_path: str) -> bool:
    """判断文件扩展名是否为视频/音频容器格式。"""
    return Path(file_path).suffix.lower() in VIDEO_EXTENSIONS


def is_network_path(file_path: "str | Path") -> bool:
    """是否为 UNC 网络路径（``//host/share`` 或 ``\\\\host\\share`` 开头）。

    这类路径的存在性探测（``Path.exists`` / ``is_file``）走 SMB——宿主机
    不可达时会同步等待网络超时重试（实测可达二十余秒）。UI 线程上的
    任何同步探测都必须先过本判断；网络路径的存在性应在后台线程探测，
    或交给异步加载链以失败回调呈现错误。

    盘符映射到网络位置（如 ``Z:\\...``）无法从字符串识别，不在覆盖范围。
    """
    s = str(file_path)
    return s.startswith("//") or s.startswith("\\\\")


def get_ffmpeg_path() -> str:
    """获取 FFmpeg 可执行文件路径。

    优先使用用户在「设置-关于/语言」中配置的路径，若未配置则返回 'ffmpeg'（依赖环境变量）。
    """
    try:
        from strange_uta_game.frontend.settings.app_settings import AppSettings
        path = AppSettings().get("tools.ffmpeg_path", "")
        return path if path else "ffmpeg"
    except Exception:
        return "ffmpeg"


def is_embedded() -> bool:
    """是否处于宿主接管（embedded）运行模式。

    宿主接管时 MainWindow 会把宿主桥注册为进程级默认 provider
    （AppSettings.set_default_provider），裸实例经共享缓存拿到现役
    实例，其 ``_provider`` 即宿主桥——与设置页各隐藏点（about.py 等）
    的判定口径一致。embedded 下 SUG 自身的 ffmpeg 设置入口被隐藏
    （EMBEDDING §5），缺失提示必须引导到工作台设置。
    """
    try:
        from strange_uta_game.frontend.settings.app_settings import AppSettings

        return AppSettings()._provider is not None
    except Exception:
        return False


def is_ffmpeg_available() -> bool:
    """检测 FFmpeg 是否可用。

    若用户配置了路径，检测该文件是否存在；否则检测环境变量中的 ffmpeg。
    """
    ffmpeg = get_ffmpeg_path()
    if ffmpeg != "ffmpeg":
        return Path(ffmpeg).is_file()
    return shutil.which("ffmpeg") is not None


def _extracted_cache_name(video_path: str) -> str:
    """提取产物的稳定文件名：``{stem}_{内容指纹}.mp3``。

    指纹取（规范化路径 + mtime_ns）的 sha1 前 12 位：
    - 不同目录的同名视频指纹不同，不会再按 stem 互相覆盖串音；
    - 同一路径重复提取得到同一文件名（覆盖写，不产生副本）；
    - 文件内容被替换（mtime 变化）后指纹改变，不会复用过期音频。
    """
    p = Path(video_path)
    try:
        mtime_ns = p.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    try:
        key = f"{p.resolve()}|{mtime_ns}"
    except OSError:
        key = f"{p}|{mtime_ns}"
    # surrogatepass：Windows 路径可能含无法按严格 UTF-8 编码的字符
    digest = hashlib.sha1(key.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return f"{p.stem}_{digest}.mp3"


# _extracted_cache_name 的指纹段：``_<12 位十六进制>`` 结尾
_EXTRACTED_FINGERPRINT_RE = re.compile(r"_[0-9a-f]{12}$")


def strip_extracted_fingerprint(stem: str) -> str:
    """去掉 stem 末尾的内容指纹段（:func:`_extracted_cache_name` 的逆操作）。

    ``video_1a2b3c4d5e6f`` → ``video``；结尾不是指纹段的 stem 原样返回
    （含旧命名 ``<stem>.mp3`` 时代的产物，本就没有指纹可去）。
    """
    return _EXTRACTED_FINGERPRINT_RE.sub("", stem)


def _is_extracted_product(file_path: "str | Path") -> bool:
    """路径是否位于提取缓存目录（即本模块的产物，而非用户自己的音频）。"""
    try:
        return Path(file_path).resolve().is_relative_to(_get_cache_dir().resolve())
    except (ValueError, OSError):
        return False


def audio_display_stem(file_path: "str | Path") -> str:
    """从音频路径派生名称用的 stem：提取产物去掉指纹后缀，其余原样。

    建议/默认保存名、备份名、导出文件名等由音频名派生的用户可见名字都应
    经此函数取 stem，避免 ``video_1a2b3c4d5e6f`` 这样的缓存指纹混进文件名。
    只对缓存目录内的产物生效：用户自己的 ``song_0123456789ab.mp3`` 不动。
    """
    stem = Path(file_path).stem
    if _is_extracted_product(file_path):
        stem = strip_extracted_fingerprint(stem)
    return stem


def audio_display_name(file_path: "str | Path") -> str:
    """音频展示文件名（含扩展名）：提取产物去掉指纹后缀，其余原样。

    时间轴音频名 / 加载提示等直接展示给用户的场合使用，与
    :func:`audio_display_stem` 同一口径。
    """
    p = Path(file_path)
    if not _is_extracted_product(p):
        return p.name
    return strip_extracted_fingerprint(p.stem) + p.suffix


def clear_extracted_cache() -> None:
    """清理提取管线的临时残留（``*.part-*`` 分片文件）。

    注意：**不能**整目录清扫。提取产物会被 file_loader 存进项目当持久
    音频，清掉其它视频的产物会破坏已保存的视频项目（数据丢失）。旧命名
    残留与超期产物的有界清理在每次提取时进行（见 _remove_legacy_extracted
    / _prune_extracted_cache）；这里唯一要清的是 ffmpeg 写到一半（崩溃/
    断电/超时被杀）残留的临时分片。
    """
    cache_dir = _get_cache_dir()
    if not cache_dir.exists():
        return
    for f in cache_dir.glob("*.part-*"):
        try:
            if f.is_file():
                f.unlink()
        except Exception:
            pass


def _remove_legacy_extracted(video_path: str, cache_dir: Path, keep_path: Optional[str]) -> None:
    """清理旧命名的提取产物 ``<stem>.mp3``。

    产物改名内容指纹（``<stem>_<指纹>.mp3``）后，旧名残留永不再被覆盖，
    只会在 extracted/ 里静静堆积。新产物已就位，按当前视频 stem 清掉对应
    旧名文件。``keep_path``（引擎正在播放的源文件，若 accessible）是旧名
    文件时跳过，避免删掉使用中的音频。
    """
    legacy = cache_dir / f"{Path(video_path).stem}.mp3"
    if keep_path:
        try:
            if legacy.resolve() == Path(keep_path).resolve():
                return
        except OSError:
            if str(legacy) == str(keep_path):
                return
    _silent_unlink(legacy)


def _prune_extracted_cache(cache_dir: Path, keep: tuple) -> None:
    """有界清理 extracted/：mtime 超过保留期的条目删除（单次 listdir，廉价）。

    内容指纹命名后旧产物不会被覆盖，extracted/ 只增不减；按 mtime 滚动
    清理兜底。``keep`` 中的路径（引擎正在播放的源文件、刚写入的目标产物）
    一律跳过；删除一律 best-effort，失败静默。
    """
    cutoff = time.time() - _EXTRACTED_TTL_DAYS * 86400
    keep_keys = set()
    for k in keep:
        if not k:
            continue
        try:
            keep_keys.add(str(Path(k).resolve()).lower())
        except OSError:
            keep_keys.add(str(Path(k)).lower())
    try:
        entries = list(cache_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if not entry.is_file() or entry.stat().st_mtime >= cutoff:
                continue
            if str(entry.resolve()).lower() in keep_keys:
                continue
            entry.unlink()
        except Exception:
            pass


def _silent_unlink(path: Path) -> None:
    """best-effort 删除文件（清理失败静默，不影响主流程）。"""
    try:
        if path.is_file():
            path.unlink()
    except Exception:
        pass


def extract_audio(
    video_path: str,
    progress_cb: Optional[LoadProgressCallback] = None,
    keep_path: Optional[str] = None,
) -> str:
    """从视频/音频文件中提取音频并压缩为 MP3 临时文件。

    Args:
        video_path: 视频文件路径
        progress_cb: 进度回调 (stage, 0.0~1.0)
        keep_path: 提取清理时须跳过的路径（引擎正在播放的源文件，若
            accessible），避免旧命名清理 / TTL 清理误删使用中的音频

    Returns:
        生成的临时 MP3 文件路径

    Raises:
        FileNotFoundError: 视频文件不存在
        RuntimeError: FFmpeg 不可用或提取失败
    """
    if not Path(video_path).is_file():
        raise FileNotFoundError(f"文件不存在: {video_path}")

    if not is_ffmpeg_available():
        if is_embedded():
            raise RuntimeError(
                "当前环境未检测到 FFmpeg。嵌入式运行的 FFmpeg 由工作台统一管理，"
                "请检查工作台设置中的 FFmpeg 配置。"
            )
        raise RuntimeError(
            "当前环境未检测到 FFmpeg，请在「设置 → 关于/语言」中配置 FFmpeg 可执行文件路径。"
        )

    clear_extracted_cache()

    cache_dir = _get_cache_dir()
    # 产物名稳定（内容指纹），直接写最终名会在提取中途失败时留下同名的
    # 截断 mp3 被后续当完整产物引用——先写临时分片，成功后原子改名。
    final_path = cache_dir / _extracted_cache_name(video_path)
    tmp_path = cache_dir / f"{final_path.name}.part-{os.getpid()}"
    temp_path = str(final_path)

    if progress_cb:
        progress_cb("正在提取音频...", 0.1)

    ffmpeg = get_ffmpeg_path()
    cmd = [
        ffmpeg,
        # 损坏文件可能触发 ffmpeg 从 stdin 读交互指令而挂住至超时
        "-nostdin",
        "-y",
        "-i", video_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-ab", f"{_MP3_QUALITY}k",
        "-ar", str(_TARGET_SAMPLE_RATE),
        # 输出扩展名是 .part-*，ffmpeg 无法从扩展名推断容器，显式指定格式
        "-f", "mp3",
        str(tmp_path),
    ]

    # Windows 下隐藏控制台窗口，避免 GUI 应用调用 FFmpeg 时闪出黑框
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=600,
            creationflags=creation_flags,
        )
    except subprocess.TimeoutExpired:
        _silent_unlink(tmp_path)
        raise RuntimeError("FFmpeg 提取超时（超过 10 分钟）")
    except FileNotFoundError:
        _silent_unlink(tmp_path)
        if is_embedded():
            raise RuntimeError(
                f"找不到 FFmpeg 可执行文件: {ffmpeg}。嵌入式运行的 FFmpeg 由工作台统一管理，"
                "请检查工作台设置中的 FFmpeg 配置。"
            )
        raise RuntimeError(
            f"找不到 FFmpeg 可执行文件: {ffmpeg}，请在「设置 → 关于/语言」中重新配置路径。"
        )

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        _silent_unlink(tmp_path)
        raise RuntimeError(f"FFmpeg 提取失败:\n{stderr[-800:]}")

    if not tmp_path.is_file():
        raise RuntimeError("FFmpeg 未生成输出文件，请确认视频文件包含音频流。")

    try:
        os.replace(str(tmp_path), str(final_path))
    except OSError as exc:
        raise RuntimeError(f"提取音频写入缓存失败: {exc}") from exc

    # 新产物已就位：清掉同 stem 的旧命名残留，并对整个目录做一次有界的
    # TTL 清理（正在播放的源文件与刚写入的目标产物跳过）。
    _remove_legacy_extracted(video_path, cache_dir, keep_path)
    _prune_extracted_cache(cache_dir, keep=(keep_path, str(final_path)))

    if progress_cb:
        progress_cb("音频提取完成", 1.0)

    return temp_path
