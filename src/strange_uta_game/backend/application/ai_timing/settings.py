"""AI 打轴设置（阶段 D）。

standalone 与 embedded 共用同一份字段结构；读取/写入通过 getter/setter
适配器完成，SUG 前端（AppSettings 的 ``ai_timing.*`` 节）与工作台宿主
（阶段 G 的 AiTimingHost）各自接线，避免应用层直接依赖前端模块。

模型根目录默认值不在 ``.cache`` 下——``.cache`` 会被自动清理，模型
权重绝不放入自动清理范围（§7.1）。
"""

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import sys

from strange_uta_game.app_dirs import config_dir

SETTINGS_SECTION = "ai_timing"


@dataclass
class AiTimingSettings:
    """AI 打轴的用户可配置项。"""

    model_root: str = ""
    """对齐模型根目录；空串 = 默认目录（config_dir/ai_models）。"""

    provider: str = "wav2vec2"
    """对齐 provider：wav2vec2（默认效果优先）/ mms_fa（备选）。"""

    wav2vec2_model_id: str = ""
    """自定义对齐模型：空 = 内置默认。

    可填 Hugging Face 仓库 ID（走受控下载），或本地模型目录（用户直接
    放进模型目录的文件夹，也接受相对于模型根的文件夹名）。仅支持
    Wav2Vec2 家族的 CTC 模型 + 拉丁字母/罗马字词表，见
    ``local_models.LOCAL_MODEL_SUPPORT_HINT``。
    """

    device: str = "auto"
    """推理设备：auto / cpu / cuda。"""

    download_mirror: str = ""
    """Hugging Face 镜像端点（如 https://hf-mirror.com）；空 = 官方源。"""

    runtime_python: str = ""
    """对齐 Runtime 的 python.exe 路径（standalone 自管；空 = 使用当前解释器）。"""

    ai_cache_root: str = ""
    """AI 缓存根目录（standalone 自定义位置；空 = 默认 .cache/ai_timing；
    embedded 由宿主注入，此字段不生效）。"""

    tail_snap: bool = True
    """尾音修正：token 终点吸附到下一 token 起点。"""

    align_scope: str = "whole"
    """对齐范围：``"whole"`` 整曲（默认，与历史行为一致）/
    ``"lrc_window"`` 按歌词行分窗（用 LRC 行时间作锚点，长前奏、间奏、
    重复副歌更稳；需工程含行时间戳，缺失时由 service 退化为整曲并记日志）。"""

    audio_speed: float = 1.0
    """音频倍速预处理（预留，阶段 C worker options 消费）。"""


def default_model_root() -> Path:
    """默认模型根目录（不在自动清理的 .cache 内）。"""
    return config_dir() / "ai_models"


def resolve_model_root(settings: AiTimingSettings) -> Path:
    return Path(settings.model_root) if settings.model_root else default_model_root()


def portable_base_dir() -> Path:
    """便携基准目录：frozen = exe 所在目录；源码运行 = ``strange_uta_game``
    包目录（仓库 ``src/`` 下的包根）。

    ``runtime_python`` 位于基准目录内时以相对路径持久化——便携包整体
    移动/换机后路径不失配（frozen 打包态的关键行为）。读写两侧都走本
    函数（存取自洽），仅基准所指目录层级在不同运行形态间不同。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def resolve_runtime_python(raw: str) -> str:
    """读取时的规范化：相对路径按便携基准目录展开为绝对路径。"""
    if not raw:
        return ""
    path = Path(raw)
    if path.is_absolute():
        return raw
    return str(portable_base_dir() / path)


def relativize_runtime_python(path: str) -> str:
    """写入时的规范化：基准目录内的绝对路径收为相对，其余原样保留。"""
    if not path:
        return path
    try:
        rel = Path(path).resolve().relative_to(portable_base_dir().resolve())
        return str(rel)
    except ValueError:
        return path


Getter = Callable[[str, object], object]
"""``getter(path, default) -> value`` 适配器。"""


def load_ai_timing_settings(getter: Getter) -> AiTimingSettings:
    """从设置存储读取 ai_timing 节（缺省字段回落默认值）。"""
    base = AiTimingSettings()
    return AiTimingSettings(
        model_root=str(getter(f"{SETTINGS_SECTION}.model_root", base.model_root)),
        provider=str(getter(f"{SETTINGS_SECTION}.provider", base.provider)),
        wav2vec2_model_id=str(
            getter(f"{SETTINGS_SECTION}.wav2vec2_model_id", base.wav2vec2_model_id)
        ),
        device=str(getter(f"{SETTINGS_SECTION}.device", base.device)),
        download_mirror=str(
            getter(f"{SETTINGS_SECTION}.download_mirror", base.download_mirror)
        ),
        runtime_python=resolve_runtime_python(
            str(getter(f"{SETTINGS_SECTION}.runtime_python", base.runtime_python))
        ),
        ai_cache_root=str(
            getter(f"{SETTINGS_SECTION}.ai_cache_root", base.ai_cache_root)
        ),
        tail_snap=bool(getter(f"{SETTINGS_SECTION}.tail_snap", base.tail_snap)),
        align_scope=str(
            getter(f"{SETTINGS_SECTION}.align_scope", base.align_scope)
        ),
        audio_speed=float(
            getter(f"{SETTINGS_SECTION}.audio_speed", base.audio_speed)
        ),
    )


def save_ai_timing_settings(
    setter: Callable[[str, object], None], settings: AiTimingSettings
) -> None:
    """把全部字段写回设置存储（由调用方负责持久化/保存触发）。

    ``runtime_python`` 以相对形式持久化（基准目录内时），便携移动不失配。
    """
    payload = asdict(settings)
    payload["runtime_python"] = relativize_runtime_python(
        payload["runtime_python"]
    )
    for key, value in payload.items():
        setter(f"{SETTINGS_SECTION}.{key}", value)


def resolve_effective_model_root(
    settings: AiTimingSettings, host_model_root: str
) -> Path:
    """embedded 模型根解析：宿主统一目录为默认，**不覆盖用户显式选择**。

    弹窗里改过模型路径（settings.model_root ≠ 宿主值）时以用户为准，
    与运行环境注入同口径——此前无条件覆盖导致嵌入模式改路径永远
    不生效（standalone 正常）。
    """
    if host_model_root:
        host_path = Path(host_model_root)
        chosen = (settings.model_root or "").strip()
        if chosen and Path(chosen) != host_path:
            return Path(chosen)
        return host_path
    return resolve_model_root(settings)


def resolve_effective_cache_root(
    settings: AiTimingSettings, host_cache_dir: str
) -> Path:
    """embedded AI 缓存根解析：用户显式设置的缓存目录优先于宿主默认。"""
    chosen = (settings.ai_cache_root or "").strip()
    if chosen:
        return Path(chosen)
    if host_cache_dir:
        return Path(host_cache_dir)
    # 惰性导入：default_ai_cache_root 在 vocals 模块（避免环）
    from strange_uta_game.backend.application.ai_timing.vocals import (
        default_ai_cache_root,
    )

    return default_ai_cache_root()


__all__ = [
    "SETTINGS_SECTION",
    "AiTimingSettings",
    "default_model_root",
    "resolve_model_root",
    "portable_base_dir",
    "resolve_runtime_python",
    "relativize_runtime_python",
    "load_ai_timing_settings",
    "save_ai_timing_settings",
    "resolve_effective_model_root",
    "resolve_effective_cache_root",
]
