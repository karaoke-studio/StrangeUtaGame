"""本地/自定义对齐模型（用户自己放进模型目录的 Wav2Vec2 CTC 模型）。

AI 打轴的对齐链路只认 **Wav2Vec2 家族**：``Wav2Vec2ForCTC`` 架构 +
拉丁字母（罗马字 / 无调拼音）词表——checkpoint 的读音先被罗马化再送进
CTC 对齐（见 ``worker/providers.py::normalize_latn_text``）。Whisper、
NeMo、CTP 等其它架构，或词表是假名 / 汉字 / IPA 的模型，接入后不会
报错，而是**安静地产出错误的时间轴**，因此这里把兼容性检查做成硬门槛。

本模块只负责「本地目录」这一侧，不负责下载（下载见 ``models.py``）：

- ``resolve_local_model_path``：把设置值解析成模型目录（绝对路径、
  相对路径、或直接放在模型目录下的文件夹名）；
- ``validate_local_model_dir``：目录是否可用（架构 / 权重 / 分词器 /
  词表覆盖，以及 Git LFS 指针这类「有文件没内容」的常见坑）；
- ``scan_local_models``：扫描模型目录，列出用户放进去、未经下载流程
  登记的候选模型，供弹窗下拉直接选择。

这类模型没有 ``manifest.json``（不是由应用下载的），因此状态行与执行
前快检都走本模块，而不是 :class:`ModelRegistry`。
"""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from strange_uta_game.backend.application.ai_timing.models import (
    MANIFEST_NAME,
    ModelStatus,
)

# 用户可见的兼容性说明（弹窗与错误消息共用一句话，避免两处措辞漂移）
LOCAL_MODEL_SUPPORT_HINT = (
    "自定义模型仅支持 Wav2Vec2 家族（Wav2Vec2ForCTC 架构 + 拉丁字母/"
    "罗马字词表）；Whisper、NeMo 等其它架构，或假名/汉字词表无法用于"
    "本对齐流程"
)

_SUPPORTED_ARCHITECTURES = ("Wav2Vec2ForCTC",)
_SUPPORTED_MODEL_TYPES = ("wav2vec2", "wav2vec2-conformer")
_TOKENIZER_NAMES = ("vocab.json", "tokenizer.json", "vocab.txt")
_SENTENCEPIECE_NAME = "tokenizer.model"
_MIN_WEIGHT_BYTES = 1024 * 1024
"""权重文件下限：低于 1MB 基本只可能是 Git LFS 指针或占位文件。"""

_LATN_MIN_COVERAGE = 20
"""词表至少覆盖 20 个拉丁字母（a-z 共 26 个）才认为能承载罗马字转写。"""

_FINGERPRINT_FILES = (
    "config.json",
    "preprocessor_config.json",
    "processor_config.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "vocab.json",
    "tokenizer.json",
    "vocab.txt",
)
"""参与缓存指纹的配置/分词器文件（权重另由 glob 收集）。"""


@dataclass
class LocalModelCandidate:
    """模型目录里发现的一个候选模型（供弹窗下拉展示）。"""

    path: Path
    label: str
    """相对模型根的展示名（根目录本身用「（模型目录根）」）。"""


def looks_like_model_dir(path: Path) -> bool:
    """目录是否像一个 HF 模型目录（只要 ``config.json``）。"""
    try:
        return Path(path).is_dir() and (Path(path) / "config.json").is_file()
    except OSError:
        return False


def normalize_model_reference(raw: str) -> str:
    """清理用户输入：去空白与误粘贴的引号（资源管理器「复制路径」带引号）。"""
    return (raw or "").strip().strip('"').strip("'").strip()


def is_url_reference(raw: str) -> bool:
    """用户是否误把网页地址当成了模型 ID / 路径。"""
    value = normalize_model_reference(raw).lower()
    return value.startswith(("http://", "https://", "www.")) or "://" in value


def is_network_path(path: Path) -> bool:
    """路径是否位于网络共享（UNC 或映射的网络驱动器）。

    扫描模型目录是同步 IO；网络路径上单次 stat 就可能几十毫秒，自动扫描
    会让弹窗打开即卡住，调用方据此改为「用户显式点刷新才扫」。
    """
    value = str(path)
    if value.startswith(("\\\\?\\", "//?/")):
        return False  # Windows 扩展长度路径前缀，不是网络共享
    if value.startswith(("\\\\", "//")):
        return True
    if os.name != "nt":  # pragma: no cover - 仅 Windows 有盘符判定
        return False
    drive = os.path.splitdrive(value)[0]
    if not drive:
        return False
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")) == 4
    except Exception:  # pragma: no cover - 非 Windows/API 不可用
        return False


def portable_model_reference(path: str, model_root: Path) -> str:
    """模型位于模型根内时收成相对路径，便携包移动/换机后仍可用。

    只用 ``abspath``（不访问文件系统），因为它会被 UI 线程频繁调用；
    路径不存在也应收成相对形式，避免用户填了一半就被改回绝对路径。
    """
    value = normalize_model_reference(path)
    if not value:
        return value
    try:
        absolute = Path(os.path.abspath(os.path.expanduser(value)))
        root = Path(os.path.abspath(os.path.expanduser(str(model_root))))
        relative = absolute.relative_to(root)
    except (OSError, ValueError):
        return value
    text = str(relative)
    # 模型根本身（"."）保留原绝对路径，避免设置里出现无意义的 "."
    return value if text in ("", ".") else text


def has_local_model_intent(raw: str, model_root: Optional[Path] = None) -> bool:
    """用户填的是否是「本地目录」而不是 HF 仓库 ID。

    判定与 :func:`resolve_local_model_path` 保持同一视图：绝对路径、
    ``.``/``~`` 开头、当前目录下存在的目录，以及 ``模型根/值`` 下存在的
    目录（用户把文件夹放进模型目录后只填文件夹名）。
    """
    value = normalize_model_reference(raw)
    if not value or is_url_reference(value):
        return False
    try:
        path = Path(value).expanduser()
    except (OSError, ValueError):
        return False
    if path.is_absolute() or value.startswith((".", "~")):
        return True
    if path.is_dir():
        return True
    if model_root is not None:
        try:
            return (Path(model_root) / value).is_dir()
        except (OSError, ValueError):
            return False
    return False


def resolve_local_model_path(raw: str, model_root: Path) -> Optional[Path]:
    """把设置值解析成已存在的本地模型目录；不是本地模型时返回 None。

    依次尝试：原样路径（绝对/相对当前目录）→ ``模型根 / 值``（用户
    直接把文件夹放到模型目录里，只填文件夹名也能命中）。
    """
    value = normalize_model_reference(raw)
    if not value or is_url_reference(value):
        return None
    try:
        direct = Path(value).expanduser()
    except (OSError, ValueError):
        return None
    candidates = [direct]
    try:
        candidates.append(Path(model_root) / value)
    except (OSError, ValueError):
        pass
    for candidate in candidates:
        if looks_like_model_dir(candidate):
            try:
                return candidate.resolve()
            except OSError:  # pragma: no cover - 极端文件系统错误
                return candidate
    return None


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _weight_files(path: Path) -> List[Path]:
    files: List[Path] = []
    for pattern in ("*.safetensors", "*.bin"):
        try:
            files.extend(sorted(path.glob(pattern)))
        except OSError:  # pragma: no cover - 目录不可读
            continue
    return files


def _tokenizer_file(path: Path) -> Optional[Path]:
    for name in _TOKENIZER_NAMES:
        candidate = path / name
        if candidate.is_file():
            return candidate
    return None


def _vocab_tokens(path: Path) -> Optional[List[str]]:
    """读取词表 token 列表；无法解析时返回 None（跳过覆盖度检查）。"""
    name = path.name.lower()
    try:
        if name == "vocab.txt":
            return path.read_text(encoding="utf-8", errors="ignore").split()
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if name == "tokenizer.json":
        # tokenizers 的 HF 格式：词表在 model.vocab（个别模型是 list）
        model = data.get("model") if isinstance(data, dict) else None
        data = model.get("vocab") if isinstance(model, dict) else None
    if isinstance(data, dict):
        return [str(key) for key in data]
    if isinstance(data, list):
        return [str(item) for item in data]
    return None


def _latn_coverage(tokens: List[str]) -> int:
    letters = set()
    for token in tokens:
        for ch in token.lower():
            if "a" <= ch <= "z":
                letters.add(ch)
    return len(letters)


def _architecture_names(config: dict) -> List[str]:
    """config.architectures 归一化：容忍字符串写法与大小写差异。"""
    raw = config.get("architectures")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _check_compatibility(directory: Path) -> Optional[str]:
    """架构检查；不兼容时返回中文原因。"""
    config = _read_json(directory / "config.json")
    if config is None:
        return "config.json 无法解析（不是有效的模型目录）"
    architectures = _architecture_names(config)
    supported = {name.casefold() for name in _SUPPORTED_ARCHITECTURES}
    if architectures:
        if not any(arch.casefold() in supported for arch in architectures):
            return (
                f"模型架构为 {'、'.join(architectures)}，不在支持范围内。"
                + LOCAL_MODEL_SUPPORT_HINT
            )
        return None
    model_type = str(config.get("model_type") or "").strip()
    if not model_type:
        # 两个字段都没有：无法证明是 CTC 模型，宁可不放行（基座权重会被
        # 随机初始化分类头，不报错但产出噪声时间轴）
        return (
            "config.json 既没有 architectures 也没有 model_type，"
            "无法确认是 Wav2Vec2 家族的 CTC 模型。" + LOCAL_MODEL_SUPPORT_HINT
        )
    if model_type.casefold() not in {
        item.casefold() for item in _SUPPORTED_MODEL_TYPES
    }:
        return (
            f"模型类型为 {model_type}，不在支持范围内。"
            + LOCAL_MODEL_SUPPORT_HINT
        )
    return None


def _check_weights(directory: Path) -> Optional[str]:
    weights = _weight_files(directory)
    if not weights:
        return "缺少模型权重（model.safetensors 或 pytorch_model.bin）"
    try:
        sizes = [(item, item.stat().st_size) for item in weights]
    except OSError as exc:  # pragma: no cover - 权限/文件消失
        return f"无法读取模型权重（{exc}）"
    # 任一权重足够大即视为真实权重（分片模型可能带 index.json）
    if any(size >= _MIN_WEIGHT_BYTES for _, size in sizes):
        return None
    shown = "、".join(f"{item.name}（{size} 字节）" for item, size in sizes)
    return (
        f"权重文件只有 {shown}，像是 Git LFS 指针或未下载完成的内容。"
        "请把模型的实际权重文件复制进来（用 git clone 时需安装 Git LFS）"
    )


def _check_tokenizer(directory: Path) -> Optional[str]:
    tokenizer = _tokenizer_file(directory)
    if tokenizer is None:
        if (directory / _SENTENCEPIECE_NAME).is_file():
            return (
                "该模型的分词器是 SentencePiece 二进制词表（tokenizer.model），"
                "本对齐流程需要 vocab.json 形式的 Wav2Vec2 CTC 分词器"
            )
        return (
            "缺少分词器文件（vocab.json / tokenizer.json / vocab.txt 任一）"
        )
    tokens = _vocab_tokens(tokenizer)
    if tokens is None:
        # 文件存在却读不出词表：不能当作通过，否则假名/汉字词表会漏检
        return f"分词器文件无法解析（{tokenizer.name}），模型文件可能不完整"
    if not tokens:
        return "分词器词表为空，模型文件可能不完整"
    coverage = _latn_coverage(tokens)
    if coverage < _LATN_MIN_COVERAGE:
        return (
            f"模型词表只覆盖 {coverage} 个拉丁字母，不是罗马字/拉丁字母词表。"
            + LOCAL_MODEL_SUPPORT_HINT
        )
    return None


def validate_local_model_dir(path: Path) -> ModelStatus:
    """校验用户自行放入的模型目录，返回与注册表同形的 :class:`ModelStatus`。

    检查顺序刻意按「用户最容易犯的错」排列：目录不存在 → 不是模型目录
    → 架构不对 → 只有 LFS 指针 → 缺分词器 → 词表不是拉丁字母。
    """
    directory = Path(path)
    if not directory.is_dir():
        return ModelStatus(
            state="missing",
            message=f"模型目录不存在：{directory}",
        )
    config_path = directory / "config.json"
    if not config_path.is_file():
        return ModelStatus(
            state="corrupt",
            model_dir=directory,
            message="不是模型目录（缺少 config.json）",
        )
    reason = _check_compatibility(directory)
    if reason is not None:
        return ModelStatus(state="corrupt", model_dir=directory, message=reason)
    reason = _check_weights(directory)
    if reason is not None:
        return ModelStatus(state="corrupt", model_dir=directory, message=reason)
    reason = _check_tokenizer(directory)
    if reason is not None:
        return ModelStatus(state="corrupt", model_dir=directory, message=reason)
    return ModelStatus(
        state="ok",
        model_dir=directory,
        message="本地模型",
    )


def local_model_fingerprint(path: Path) -> str:
    """本地模型的文件指纹（全部权重 + 配置文件/分词器的 name/size/mtime）。

    本地模型会被用户原地替换（覆盖新权重、改词表、只换一个分片），而对齐
    缓存只按模型身份区分——不带指纹就会命中旧时间轴。这里只读元数据，
    不读文件内容（哈希大权重会让每次执行多花几秒）。
    """
    directory = Path(path)
    files = _weight_files(directory)
    for name in _FINGERPRINT_FILES:
        candidate = directory / name
        try:
            if candidate.is_file():
                files.append(candidate)
        except OSError:  # pragma: no cover - 权限错误
            continue
    if not files:
        return ""
    parts: List[str] = []
    try:
        for item in sorted(files):
            stat = item.stat()
            parts.append(f"{item.name}:{stat.st_size}:{stat.st_mtime_ns}")
    except OSError:
        return ""
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def _walk_models(base: Path, depth: int, budget: List[int]) -> List[Path]:
    """扫描候选模型目录；``budget[0]`` 为剩余可检查目录数（防极端目录树）。"""
    if budget[0] <= 0:
        return []
    try:
        children = sorted(base.iterdir())
    except OSError:
        return []
    found: List[Path] = []
    for child in children:
        if budget[0] <= 0:
            break
        budget[0] -= 1
        try:
            if not child.is_dir() or child.name.startswith("."):
                continue
        except OSError:  # pragma: no cover - 极端文件系统错误
            continue
        if (child / MANIFEST_NAME).is_file():
            # 应用下载的受控模型：已在注册表里，不重复列出
            continue
        if looks_like_model_dir(child):
            found.append(child)
            continue
        if depth > 1:
            # 容忍多套一层目录（解压/从缓存复制常见形态）
            found.extend(_walk_models(child, depth - 1, budget))
    return found


def scan_local_models(
    model_root: Path, *, max_depth: int = 2, max_entries: int = 400
) -> List[LocalModelCandidate]:
    """扫描模型目录，列出用户自行放入的模型（无 ``manifest.json``）。

    支持三种常见放法：模型根下的文件夹、多套一层目录、以及把文件
    直接摊在模型根里。受控下载的模型（带 manifest）不列出，避免与
    下载模型下拉混淆。``max_entries`` 限制检查的目录总数——模型根被
    指到一个巨型目录时，弹窗打开不能因此卡住界面。
    """
    root = Path(model_root)
    candidates: List[LocalModelCandidate] = []
    if looks_like_model_dir(root):
        candidates.append(
            LocalModelCandidate(path=root, label="（模型目录根）")
        )
    for path in _walk_models(root, max_depth, [max_entries]):
        try:
            label = str(path.relative_to(root))
        except ValueError:  # pragma: no cover - 不应发生
            label = path.name
        candidates.append(LocalModelCandidate(path=path, label=label))
    return candidates


__all__ = [
    "LOCAL_MODEL_SUPPORT_HINT",
    "LocalModelCandidate",
    "has_local_model_intent",
    "is_network_path",
    "is_url_reference",
    "local_model_fingerprint",
    "looks_like_model_dir",
    "normalize_model_reference",
    "portable_model_reference",
    "resolve_local_model_path",
    "scan_local_models",
    "validate_local_model_dir",
]
