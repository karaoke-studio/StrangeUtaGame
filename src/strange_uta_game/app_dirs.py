"""跨平台用户数据目录解析（单一真源）。

各处需要落盘的模块都应经由本模块取目录，确保口径一致，并避免在只读位置写入。

设计：
- **Windows / Linux**：保持原「便携」行为 —— 配置与缓存都写在程序所在目录，
  并支持程序目录下的 ``.config_redirect`` 把配置重定向到自定义位置。
- **macOS**：程序被装进只读的 ``.app`` bundle（或经 Gatekeeper App Translocation
  从只读临时挂载点运行），程序目录不可写。改用系统约定的可写位置：
    * 配置 / 项目 → ``~/Library/Application Support/StrangeUtaGame``
    * 缓存        → ``~/Library/Caches/StrangeUtaGame``
    * 日志        → ``~/Library/Logs/StrangeUtaGame``
- **任意平台兜底**：若上述目录最终不可写（例如 Windows 装在 ``Program Files`` 且
  无写权限），回退到 ``~/.strange_uta_game``（及其 ``cache`` / ``logs`` 子目录），
  保证程序绝不因目录不可写而崩溃。

注意：可写性以「能否真正建文件」为准（试写探针），而不是 ``mkdir(exist_ok=True)``
是否成功 —— 只读 bundle 内目标目录已存在，``mkdir`` 不会报错，但写入会失败。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, Optional

APP_NAME = "StrangeUtaGame"
_FALLBACK_ROOT = Path.home() / ".strange_uta_game"
_REDIRECT_FILENAME = ".config_redirect"

# 目录解析的进程级缓存：键 = (目录类别, 相关环境变量当前值)。
# 解析本身含可写性探针（mkdir + TemporaryFile 试写 + realpath），每次
# ~1ms 的文件系统 IO；AppSettings() 等热路径逐句/逐字符触发时，大项目
# 加载会被放大成秒级延迟（见 sug_io._dict_to_sentence / 迁移路径）。
# 键含环境变量 → 测试 monkeypatch SUG_CONFIG_DIR / SUG_CACHE_DIR 时
# 自然失效；命中后仍做一次 is_dir 校验，目录被外部删除时重新解析。
# `.config_redirect` 标记的手工变更在进程内不自动生效（重启或经
# reset_dir_cache() / AppSettings.retarget_config_dir 失效）。
_DIR_CACHE: Dict[tuple, Path] = {}
_PROGRAM_DIR: Optional[Path] = None


def reset_dir_cache() -> None:
    """清空目录解析缓存（配置位置重定向等目录体系变更后调用）。

    必须重绑新 dict 而非 ``clear()`` 原地清空：并发 miss 的线程可能正
    持有旧 dict 引用（见 :func:`_cached_dir` 的捕获语义），原地清空后
    迟到写回会把按旧 ``.config_redirect`` 解析的目录钉回缓存，重定向
    静默失效；重绑则迟到写回落在孤儿 dict 上，不污染新缓存。
    """
    global _DIR_CACHE, _PROGRAM_DIR
    _DIR_CACHE = {}
    _PROGRAM_DIR = None


def _cached_dir(kind: str, env_value: str, resolver: Callable[[], Path]) -> Path:
    """按 (类别, 环境变量值) 缓存目录解析结果。

    命中且目录仍存在 → 直接返回（省去探针 IO）；miss / 目录已被删除 →
    重新解析并回填。「目录被删除后自动重建」的既有行为由此保留。

    读与写都走入口捕获的 dict 引用（而非重读全局）：miss 后 resolver
    探测期间若发生 :func:`reset_dir_cache`（重绑新 dict），迟到写回落在
    孤儿 dict 上，不会把旧解析结果钉进已重置的新缓存。
    """
    cache = _DIR_CACHE
    key = (kind, env_value)
    cached = cache.get(key)
    if cached is not None and cached.is_dir():
        return cached
    resolved = resolver()
    cache[key] = resolved
    return resolved


def program_dir() -> Path:
    """可执行文件（开发期为源码树根目录）所在目录。

    冻结（PyInstaller）环境下取 ``sys.executable``；源码运行时以本文件
    位置推导源码树根（``src/`` 的上一级），不再依赖 ``sys.argv[0]``——
    后者在 ``python -m pytest`` 等场景指向 site-packages 里的 runner 路径，
    会使配置目录的可写性探针写错位置（个别机器上对受保护目录的探针
    创建请求会长期阻塞，表现为测试无限卡死）。

    进程内不变，缓存返回（原每次调用做两次 ``Path.resolve`` 实路径
    解析，是配置目录解析的主要 IO 之一）。
    """
    global _PROGRAM_DIR
    if _PROGRAM_DIR is None:
        if getattr(sys, "frozen", False):
            _PROGRAM_DIR = Path(sys.executable).resolve().parent
        else:
            _PROGRAM_DIR = Path(__file__).resolve().parents[2]
    return _PROGRAM_DIR


def _is_dir_writable(d: Path) -> bool:
    """目录是否「真正可写」：能创建则建之，再用试写探针验证写权限。"""
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    try:
        # 仅探测写权限：临时文件用完即删，不留痕迹。
        with tempfile.TemporaryFile(dir=str(d)):
            pass
        return True
    except OSError:
        return False


def _first_writable(*candidates: Path) -> Path:
    """返回首个可写目录；全不可写时返回最后一个候选（已尽力 mkdir，交由调用方兜底）。"""
    for c in candidates:
        if _is_dir_writable(c):
            return c
    last = candidates[-1]
    try:
        last.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return last


def redirect_marker_path() -> Path:
    """``.config_redirect`` 标记文件位置（始终在程序目录，保持便携语义）。"""
    return program_dir() / _REDIRECT_FILENAME


def _read_redirect() -> Optional[Path]:
    marker = redirect_marker_path()
    try:
        if marker.exists():
            custom = Path(marker.read_text(encoding="utf-8").strip())
            if custom.is_dir():
                return custom
    except OSError:
        pass
    return None


def config_dir() -> Path:
    """配置 / 项目目录（已确保存在且可写）。

    优先级：``SUG_CONFIG_DIR`` 环境变量 > ``.config_redirect`` > 平台默认
    > ``~/.strange_uta_game`` 兜底。环境变量最高优先，供测试 / 嵌入宿主
    把配置重定向到隔离位置（与 ``SUG_CACHE_DIR`` / ``SUG_BACKUP_DIR`` 口径
    一致，避免测试读写开发者的真实 config.json）。

    结果按 (SUG_CONFIG_DIR 当前值) 进程级缓存（见 :func:`_cached_dir`），
    环境变量变化自然失效；``.config_redirect`` 变更经
    :func:`reset_dir_cache`（``AppSettings.retarget_config_dir`` 已接线）。
    """
    env_value = os.environ.get("SUG_CONFIG_DIR", "")

    def _resolve() -> Path:
        if env_value:
            return _first_writable(Path(env_value), _FALLBACK_ROOT)
        redirected = _read_redirect()
        if redirected is not None:
            return _first_writable(redirected, _FALLBACK_ROOT)
        if sys.platform == "darwin":
            return _first_writable(
                Path.home() / "Library" / "Application Support" / APP_NAME,
                _FALLBACK_ROOT,
            )
        return _first_writable(program_dir(), _FALLBACK_ROOT)

    return _cached_dir("config", env_value, _resolve)


def cache_dir() -> Path:
    """缓存目录（已确保存在且可写）。``SUG_CACHE_DIR`` 环境变量最高优先。

    结果按 (SUG_CACHE_DIR 当前值) 进程级缓存（见 :func:`_cached_dir`）。
    """
    env_value = os.environ.get("SUG_CACHE_DIR", "")

    def _resolve() -> Path:
        if env_value:
            return _first_writable(Path(env_value), _FALLBACK_ROOT / "cache")
        if sys.platform == "darwin":
            return _first_writable(
                Path.home() / "Library" / "Caches" / APP_NAME,
                _FALLBACK_ROOT / "cache",
            )
        return _first_writable(program_dir() / ".cache", _FALLBACK_ROOT / "cache")

    return _cached_dir("cache", env_value, _resolve)


def logs_dir() -> Path:
    """日志目录（已确保存在且可写）。

    ``SUG_LOGS_DIR`` 环境变量最高优先，与 ``SUG_CONFIG_DIR`` /
    ``SUG_CACHE_DIR`` 同口径：嵌入宿主把日志收进自己的数据目录
    （crash.log 与 ai_timing.log 都跟随本目录，不可写时回退
    ``~/.strange_uta_game/logs``）。

    结果按 (SUG_LOGS_DIR 当前值) 进程级缓存（见 :func:`_cached_dir`）。
    """
    env_value = os.environ.get("SUG_LOGS_DIR", "")

    def _resolve() -> Path:
        if env_value:
            return _first_writable(Path(env_value), _FALLBACK_ROOT / "logs")
        if sys.platform == "darwin":
            return _first_writable(
                Path.home() / "Library" / "Logs" / APP_NAME,
                _FALLBACK_ROOT / "logs",
            )
        return _first_writable(program_dir() / "logs", _FALLBACK_ROOT / "logs")

    return _cached_dir("logs", env_value, _resolve)


def default_backup_dir() -> Path:
    """默认的项目备份根目录（``<config_dir>/ProjectBackup``）。

    备份与项目/配置同源：Windows/Linux 便携模式下在程序目录，macOS 在
    ``~/Library/Application Support``，最终兜底 ``~/.strange_uta_game``。
    """
    return config_dir() / "ProjectBackup"


def backup_dir(custom: Optional[str] = None) -> Path:
    """项目备份根目录（已确保存在且可写）。

    优先级：``SUG_BACKUP_DIR`` 环境变量 > ``custom``（用户设置）> 默认位置。

    Args:
        custom: 用户在设置中显式指定的备份位置；为空 / 不可写时回退到
            :func:`default_backup_dir`，再兜底到 ``~/.strange_uta_game/ProjectBackup``。
    """
    fallback = _FALLBACK_ROOT / "ProjectBackup"
    env_dir = os.environ.get("SUG_BACKUP_DIR")
    if env_dir:
        return _first_writable(Path(env_dir), default_backup_dir(), fallback)
    if custom and custom.strip():
        return _first_writable(Path(custom.strip()), default_backup_dir(), fallback)
    return _first_writable(default_backup_dir(), fallback)
