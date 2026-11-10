"""AI 打轴模型注册表与下载服务（阶段 D）。

目录约定（§7.1）::

    <model_root>/
    └── <slug(model_id)>/
        ├── manifest.json          # 最后写入：只有它存在且校验通过才算已安装
        ├── config.json
        ├── model.safetensors
        └── ...

原子性：下载先写 ``<name>.part``，完成后改名并计算摘要；manifest 在
全部文件就绪后最后写入。中断（取消/断网/崩溃）不会注册半成品——
``validate`` 对无 manifest 或摘要不符的目录返回 incomplete/corrupt。

模型去重：下载到受控本地目录（hub local_dir 模式），并控制
HF_HOME / HF_HUB_CACHE 指向同一位置，避免应用模型目录与 Hugging Face
默认缓存各存一份权重（§7.1、§10）。
"""

import hashlib
import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

PROGRESS_CB = Callable[[int, str], None]
CANCEL_CB = Callable[[], bool]

MANIFEST_NAME = "manifest.json"
PART_SUFFIX = ".part"

# Wav2Vec2 模型需要的文件类型；排除 TF/Flax/ONNX 权重与冗余大文件。
# ``.bin`` / ``.model``：自定义（非默认）仓库可能只发 pytorch_model.bin，
# 或带 SentencePiece 的 tokenizer.model；不放进白名单会导致「仓库里有
# 模型但下载不到任何文件」。权重去重见 filter_model_files。
_INCLUDED_EXTENSIONS = {
    ".json",
    ".txt",
    ".safetensors",
    ".bin",
    ".model",
}
_EXCLUDED_NAMES = {".gitattributes", "README.md"}

_BIN_WEIGHT_NAMES = {"pytorch_model.bin", "adapter_model.bin"}
"""允许下载的 .bin 权重文件名（其余 .bin 多为训练产物，不进下载清单）。"""


def _is_weight_bin(filename: str) -> bool:
    base = Path(filename).name
    return base in _BIN_WEIGHT_NAMES or (
        base.startswith("pytorch_model-") and base.endswith(".bin")
    )


class ModelRegistryError(RuntimeError):
    """模型注册表操作错误（中文消息）。"""


def slugify_model_id(model_id: str) -> str:
    """repo id → 目录名（``NextFire/mms-300m-...`` → ``NextFire__mms-300m-...``）。"""
    slug = model_id.replace("/", "__").replace("\\", "__")
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", slug)
    return slug or "model"


def sha256_of_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class ModelFileEntry:
    filename: str
    size: int
    sha256: str


@dataclass
class ModelManifest:
    model_id: str
    provider: str
    revision: str
    license: str = ""
    files: List[ModelFileEntry] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "revision": self.revision,
            "license": self.license,
            "created_at": self.created_at,
            "files": [
                {
                    "filename": f.filename,
                    "size": f.size,
                    "sha256": f.sha256,
                }
                for f in self.files
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ModelManifest":
        return cls(
            model_id=str(data.get("model_id", "")),
            provider=str(data.get("provider", "")),
            revision=str(data.get("revision", "")),
            license=str(data.get("license", "")),
            created_at=str(data.get("created_at", "")),
            files=[
                ModelFileEntry(
                    filename=str(f.get("filename", "")),
                    size=int(f.get("size", 0)),
                    sha256=str(f.get("sha256", "")),
                )
                for f in data.get("files", [])
            ],
        )


@dataclass
class ModelStatus:
    """单个模型在注册表中的状态（对应弹窗状态卡）。"""

    state: str
    """missing / incomplete / corrupt / ok / error"""

    model_dir: Optional[Path] = None
    manifest: Optional[ModelManifest] = None
    message: str = ""

    @property
    def is_ready(self) -> bool:
        return self.state == "ok"


class ModelRegistry:
    """受控模型目录 + manifest 管理（只负责本地状态，不负责下载）。"""

    def __init__(self, root: Path):
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def retarget(self, root: Path) -> None:
        """动态更换模型根（弹窗内改路径即时生效；任务运行中由调用方拦截）。"""
        self._root = Path(root)

    def model_dir(self, model_id: str) -> Path:
        return self._root / slugify_model_id(model_id)

    def read_manifest(self, model_id: str) -> Optional[ModelManifest]:
        path = self.model_dir(model_id) / MANIFEST_NAME
        if not path.is_file():
            return None
        try:
            return ModelManifest.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, ValueError, OSError):
            return None

    def register(self, manifest: ModelManifest) -> Path:
        """写入 manifest（安装的最后一步）。manifest 存在即视为已安装。"""
        target = self.model_dir(manifest.model_id)
        target.mkdir(parents=True, exist_ok=True)
        manifest.created_at = manifest.created_at or time.strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        manifest_path = target / MANIFEST_NAME
        tmp = manifest_path.with_suffix(".json.part")
        tmp.write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(manifest_path)
        return target

    def validate(
        self, model_id: str, *, deep: bool = False
    ) -> ModelStatus:
        """校验模型目录。

        deep=False：只检查 manifest 与文件存在/大小（快）；
        deep=True：重算 sha256（慢，用户主动「重新校验」时用）。
        """
        model_dir = self.model_dir(model_id)
        if not model_dir.is_dir():
            return ModelStatus(state="missing", model_dir=None, message="模型未安装")
        manifest = self.read_manifest(model_id)
        if manifest is None:
            return ModelStatus(
                state="incomplete",
                model_dir=model_dir,
                message="下载未完成（缺少 manifest）",
            )
        for entry in manifest.files:
            f = model_dir / entry.filename
            if not f.is_file():
                return ModelStatus(
                    state="corrupt",
                    model_dir=model_dir,
                    manifest=manifest,
                    message=f"文件缺失：{entry.filename}",
                )
            if f.stat().st_size != entry.size:
                return ModelStatus(
                    state="corrupt",
                    model_dir=model_dir,
                    manifest=manifest,
                    message=f"文件大小不符：{entry.filename}",
                )
        if deep:
            for entry in manifest.files:
                digest = sha256_of_file(model_dir / entry.filename)
                if digest != entry.sha256:
                    return ModelStatus(
                        state="corrupt",
                        model_dir=model_dir,
                        manifest=manifest,
                        message=f"文件校验失败：{entry.filename}",
                    )
        return ModelStatus(state="ok", model_dir=model_dir, manifest=manifest)

    def resolve_model_path(self, model_id: str, *, deep: bool = False) -> Optional[Path]:
        """校验通过则返回本地模型目录（worker 直接从该路径加载）。"""
        status = self.validate(model_id, deep=deep)
        return status.model_dir if status.is_ready else None

    def list_installed(self) -> List[ModelManifest]:
        """列出已注册（manifest 存在）的模型。"""
        result: List[ModelManifest] = []
        if not self._root.is_dir():
            return result
        for child in sorted(self._root.iterdir()):
            if not child.is_dir():
                continue
            manifest_path = child / MANIFEST_NAME
            if not manifest_path.is_file():
                continue
            try:
                result.append(
                    ModelManifest.from_dict(
                        json.loads(manifest_path.read_text(encoding="utf-8"))
                    )
                )
            except (json.JSONDecodeError, ValueError, OSError):
                continue
        return result

    def clear_partial_downloads(self, model_id: str) -> int:
        """清理 ``.part`` 残留（任务结束与下次启动时调用，§7.3）。"""
        model_dir = self.model_dir(model_id)
        removed = 0
        if not model_dir.is_dir():
            return 0
        for p in model_dir.rglob(f"*{PART_SUFFIX}"):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
        return removed


class ModelDownloadTransport(ABC):
    """远端模型仓库传输抽象（真实实现为 Hugging Face Hub）。"""

    @abstractmethod
    def list_files(self, repo_id: str, revision: str) -> List[Tuple[str, int]]:
        """返回 [(filename, size_bytes)]，按文件名排序。

        实现可选择性升级为 ``(filename, size_bytes, sha256)`` 三元组
        （sha256 = 远端 LFS oid）：下载编排据此做远端完整性比对；
        二元组仍受支持（跳过比对）。
        """

    @abstractmethod
    def download_file(
        self,
        repo_id: str,
        revision: str,
        filename: str,
        dest: Path,
        *,
        expected_size: int,
        progress: PROGRESS_CB,
        cancel: CANCEL_CB,
        expected_sha256: Optional[str] = None,
    ) -> None:
        """下载单个文件到 dest（原子：完成前写 .part）。

        progress 按本文件 0-100 回调（字节级真实进度）；cancel 在传输
        块之间被检查，必须可及时中断。``expected_sha256``（远端 LFS
        oid）可得时，断点续传前须校验 .part 前缀与远端一致。
        """


def filter_model_files(files: List[Tuple[str, int]]) -> List[Tuple[str, int]]:
    """按白名单过滤所需文件（排除 TF/Flax/ONNX 冗余权重）。

    条目兼容 ``(name, size)`` 与 ``(name, size, sha256)`` 两种形态，
    返回时保持原形态（sha256 为远端 LFS oid，透传给下载编排做完整性
    比对，防止损坏权重「自证」校验通过）。

    同一仓库同时提供 ``.safetensors`` 与 ``.bin`` 时只保留 safetensors：
    ``.bin`` 是同一份权重的另一种序列化，两份都下会白占一倍磁盘与流量。
    """
    kept = [
        item
        for item in files
        if item[0] not in _EXCLUDED_NAMES
        and Path(item[0]).suffix.lower() in _INCLUDED_EXTENSIONS
        and ".cache" not in item[0].split("/")
        and (
            Path(item[0]).suffix.lower() != ".bin"
            or _is_weight_bin(item[0])
        )
    ]
    names = [item[0] for item in kept]
    has_safetensors = any(Path(name).suffix.lower() == ".safetensors" for name in names)
    # 分片 bin 仓库会带 index.json 指向每一片；此时不能因为存在 safetensors
    # 就丢掉 bin（否则可能丢掉唯一完整的权重）
    has_bin_index = any(
        name.endswith("pytorch_model.bin.index.json") for name in names
    )
    if has_safetensors and not has_bin_index:
        kept = [item for item in kept if not _is_weight_bin(item[0])]
    return kept


class HfHubTransport(ModelDownloadTransport):
    """Hugging Face Hub 直连流式下载（不经过 hub 的 tqdm/缓存）。

    - 真实字节级进度（UI 进度条不再停在 0.08%）；
    - 逐块检查取消标记，大文件下载可随时打断（断点续传保留 .part）；
    - Range 断点续传：已下载部分不重拉；
    - 代理：默认继承系统/环境代理（requests trust_env）；显式 proxy
      优先（embedded 由宿主注入工作台的网络代理设置）；镜像端点支持。
    """

    _CHUNK = 256 * 1024
    _TIMEOUT = (10, 30)  # 连接 / 读超时（秒）

    def __init__(
        self,
        endpoint: str = "",
        hf_cache_root: Optional[Path] = None,
        proxy: str = "",
    ):
        self._endpoint = (endpoint or "https://huggingface.co").rstrip("/")
        self._hf_cache_root = hf_cache_root
        self._proxies = {"http": proxy, "https": proxy} if proxy else None

    def set_endpoint(self, endpoint: str = "") -> None:
        """动态更换下载端点（镜像热更新；空串 = 官方源）。

        transport 在弹窗构造时按当时设置创建；用户在弹窗内改「下载镜像」
        后必须调用本方法，否则下载仍走旧端点（此前该设置形同虚设）。
        """
        self._endpoint = (endpoint or "https://huggingface.co").rstrip("/")

    def _proxies_kwargs(self) -> dict:
        return {"proxies": self._proxies} if self._proxies else {}

    def list_files(self, repo_id: str, revision: str) -> List[Tuple[str, int]]:
        import requests

        url = f"{self._endpoint}/api/models/{repo_id}/tree/{revision}?recursive=true"
        try:
            resp = requests.get(
                url, timeout=self._TIMEOUT, **self._proxies_kwargs()
            )
            resp.raise_for_status()
            entries = resp.json()
        except Exception as exc:
            raise ModelRegistryError(f"获取模型文件列表失败：{exc}") from exc
        result: List[Tuple[str, int]] = []
        for item in entries if isinstance(entries, list) else []:
            name = str(item.get("path", ""))
            if not name:
                continue
            # LFS 权重在 lfs.size；普通文件在 size
            size = int(
                (item.get("lfs") or {}).get("size") or item.get("size") or 0
            )
            # 保留 LFS oid（远端 sha256）：下载完成后与本地内容比对，
            # 完整性以远端为准而非 manifest 自算哈希
            oid = (item.get("lfs") or {}).get("oid")
            result.append((name, size, str(oid) if oid else None))
        return sorted(result)

    def download_file(
        self,
        repo_id: str,
        revision: str,
        filename: str,
        dest: Path,
        *,
        expected_size: int,
        progress: PROGRESS_CB,
        cancel: CANCEL_CB,
        expected_sha256: Optional[str] = None,
    ) -> None:
        import requests

        url = f"{self._endpoint}/{repo_id}/resolve/{revision}/{filename}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + PART_SUFFIX)
        offset = part.stat().st_size if part.is_file() else 0
        if offset and expected_size and offset > expected_size:
            # .part 比目标文件还大：来自旧版本/其他来源的残留，不可续传
            part.unlink(missing_ok=True)
            offset = 0
        if offset and expected_sha256:
            # 断点续传前校验 .part 前缀：抽头 64KB 与远端比对，防止把
            # 旧版本文件接上新版本尾巴（最终 sha256 比对会兜底，这里
            # 提前止损省一次整文件重拉）；探测失败按可续传处理
            try:
                probe = requests.get(
                    url,
                    headers={"Range": f"bytes=0-{min(offset, 65536) - 1}"},
                    timeout=self._TIMEOUT,
                    **self._proxies_kwargs(),
                )
                probe.raise_for_status()
                with part.open("rb") as fh:
                    local_prefix = fh.read(min(offset, 65536))
                remote_prefix = b""
                for chunk in probe.iter_content(chunk_size=self._CHUNK):
                    remote_prefix += chunk
                if remote_prefix[: len(local_prefix)] != local_prefix:
                    part.unlink(missing_ok=True)
                    offset = 0
            except Exception:
                pass  # 前缀探测失败不阻断：最终 sha256 比对兜底

        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            resp = requests.get(
                url,
                headers=headers,
                stream=True,
                timeout=self._TIMEOUT,
                **self._proxies_kwargs(),
            )
            if offset and resp.status_code == 200:
                # 服务端不支持 Range：重头下载
                offset = 0
            resp.raise_for_status()
        except Exception as exc:
            raise ModelRegistryError(f"连接下载源失败：{exc}") from exc

        total = expected_size or int(resp.headers.get("Content-Length", 0)) + offset
        done_bytes = offset
        import time as _time
        _t0 = _time.monotonic()
        _b0 = done_bytes
        try:
            mode = "ab" if offset and resp.status_code == 206 else "wb"
            with part.open(mode) as fh:
                for chunk in resp.iter_content(chunk_size=self._CHUNK):
                    if cancel():
                        raise ModelRegistryError("已取消")
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done_bytes += len(chunk)
                    if total:
                        _elapsed = max(0.001, _time.monotonic() - _t0)
                        _rate = (done_bytes - _b0) / _elapsed / 1024 / 1024
                        # 单位统一：剩余 MB ÷ MB/s；起步 3 秒内速率不稳，只报速度不报剩余
                        _remain_mb = (total - done_bytes) / 1024 / 1024
                        _eta = int(_remain_mb / max(_rate, 0.001))
                        _m, _s = divmod(_eta, 60)
                        progress(
                            min(99, int(done_bytes * 100 / total)),
                            f"下载 {filename}"
                            f"({done_bytes // 1024 // 1024}MB / {total // 1024 // 1024}MB，{_rate:.1f}MB/s，预计剩余 {_m}:{_s:02d}）",
                        )
        except ModelRegistryError:
            raise
        except Exception as exc:
            raise ModelRegistryError(f"下载 {filename} 失败：{exc}") from exc

        if total and done_bytes != total:
            raise ModelRegistryError(
                f"下载 {filename} 不完整（{done_bytes}/{total} 字节），已保留断点"
            )
        part.replace(dest)
        progress(100, f"完成 {filename}")


class ModelDownloadService:
    """编排：列文件 → 逐文件下载（.part → 改名 → 摘要）→ 注册 manifest。"""

    def __init__(self, registry: ModelRegistry, transport: ModelDownloadTransport):
        self._registry = registry
        self._transport = transport

    def set_endpoint(self, endpoint: str = "") -> None:
        """把下载端点同步到 transport（弹窗内改镜像后调用）。

        transport 支持 ``set_endpoint``（HfHubTransport）时动态更换；不
        支持（自定义 transport）时静默跳过，仍按其自身默认端点下载。
        """
        setter = getattr(self._transport, "set_endpoint", None)
        if callable(setter):
            try:
                setter(endpoint)
            except Exception:  # pragma: no cover - 防御自定义 transport
                pass

    def download(
        self,
        model_id: str,
        provider: str,
        *,
        revision: str = "main",
        license_text: str = "",
        progress: Optional[PROGRESS_CB] = None,
        cancel: Optional[CANCEL_CB] = None,
    ) -> Path:
        """下载并注册模型，返回本地目录；已就绪时直接返回现有目录。"""
        from strange_uta_game.backend.application.ai_timing.ailog import ailog

        cancel = cancel or (lambda: False)
        progress = progress or (lambda p, m: None)

        existing = self._registry.resolve_model_path(model_id)
        if existing is not None:
            progress(100, "模型已安装")
            return existing

        import time as _time

        started = _time.monotonic()
        ailog("model", f"模型下载开始：{model_id}@{revision}")
        try:
            target = self._download_impl(
                model_id,
                provider,
                revision=revision,
                license_text=license_text,
                progress=progress,
                cancel=cancel,
                ailog=ailog,
            )
        except Exception as exc:
            ailog(
                "model",
                f"模型下载失败（{type(exc).__name__}: {exc}，"
                f"{(_time.monotonic() - started):.1f}s）：{model_id}",
            )
            raise
        ailog(
            "model",
            f"模型下载完成：{(_time.monotonic() - started):.1f}s → {target}",
        )
        return target

    def _download_impl(
        self,
        model_id: str,
        provider: str,
        *,
        revision: str = "main",
        license_text: str = "",
        progress: Optional[PROGRESS_CB] = None,
        cancel: Optional[CANCEL_CB] = None,
        ailog=None,
    ) -> Path:
        ailog = ailog or (lambda msg: None)

        existing = self._registry.resolve_model_path(model_id)
        if existing is not None:
            progress(100, "模型已安装")
            return existing

        model_dir = self._registry.model_dir(model_id)
        model_dir.mkdir(parents=True, exist_ok=True)
        progress(2, "获取模型文件列表")
        # transport 条目兼容 (name, size) 与 (name, size, sha256)：
        # sha256 = 远端 LFS oid（可得时下载后必须比对）
        files = filter_model_files(
            list(self._transport.list_files(model_id, revision))
        )
        if not files:
            raise ModelRegistryError(f"模型仓库中没有可下载的文件：{model_id}")

        entries: List[ModelFileEntry] = []
        # 远端未提供 LFS oid 的文件（非 LFS 小文件属正常；若镜像源整体
        # 不回传 oid，权重也会落进来——记录出来，退化不再完全静默）
        no_remote_sha: List[str] = []
        for i, item in enumerate(files):
            filename, size = str(item[0]), int(item[1])
            remote_sha = str(item[2]) if len(item) > 2 and item[2] else None
            if cancel():
                raise ModelRegistryError("已取消")
            base = int(5 + 90 * i / len(files))
            span = int(90 / len(files))
            part = model_dir / (filename + PART_SUFFIX)
            if part.is_file() and part.stat().st_size > 0:
                progress(
                    base,
                    f"检测到断点（{part.stat().st_size // 1024 // 1024}MB），"
                    f"续传 {filename}（{i + 1}/{len(files)}）",
                )
            else:
                progress(base, f"下载 {filename}（{i + 1}/{len(files)}）")
            dest = model_dir / filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            # The manifest is committed last. Reuse completed files after an
            # interrupted installation, but only with a known matching size.
            if size > 0 and dest.is_file() and dest.stat().st_size == size:
                progress(base + span, f"复用已下载文件 {filename}")
            else:
                extra = (
                    {"expected_sha256": remote_sha} if remote_sha else {}
                )
                self._transport.download_file(
                    model_id,
                    revision,
                    filename,
                    dest,
                    expected_size=size,
                    progress=lambda p, m, _b=base, _s=span: progress(
                        min(95, _b + int(p * _s / 100)), m
                    ),
                    cancel=cancel,
                    **extra,
                )
            if not dest.is_file():
                raise ModelRegistryError(f"下载后文件不存在：{filename}")
            if size > 0 and dest.stat().st_size != size:
                raise ModelRegistryError(
                    f"下载 {filename} 大小不符（{dest.stat().st_size}/{size} 字节）"
                )
            actual_sha = sha256_of_file(dest)
            if remote_sha and actual_sha != remote_sha:
                # 远端 sha256 比对失败：删掉坏文件与断点，避免下次再被
                # 「复用」；manifest 不落盘，模型保持未注册
                dest.unlink(missing_ok=True)
                part = dest.with_name(dest.name + PART_SUFFIX)
                part.unlink(missing_ok=True)
                raise ModelRegistryError(
                    f"下载 {filename} 完整性校验失败（与远端 sha256 不符，"
                    "文件可能损坏或被篡改），请重试下载"
                )
            if remote_sha is None:
                no_remote_sha.append(filename)
            entries.append(
                ModelFileEntry(
                    filename=filename,
                    size=dest.stat().st_size,
                    sha256=actual_sha,
                )
            )

        if no_remote_sha:
            ailog(
                "model",
                f"注意：{len(no_remote_sha)} 个文件远端未提供 LFS sha256"
                f"（如 {no_remote_sha[0]}），仅校验文件大小，"
                "未与远端哈希比对",
            )
        if cancel():
            raise ModelRegistryError("已取消")
        progress(97, "写入模型清单")
        manifest = ModelManifest(
            model_id=model_id,
            provider=provider,
            revision=revision,
            license=license_text,
            files=entries,
        )
        target = self._registry.register(manifest)
        progress(100, "模型下载完成")
        return target


__all__ = [
    "MANIFEST_NAME",
    "ModelRegistryError",
    "slugify_model_id",
    "sha256_of_file",
    "ModelFileEntry",
    "ModelManifest",
    "ModelStatus",
    "ModelRegistry",
    "ModelDownloadTransport",
    "HfHubTransport",
    "ModelDownloadService",
    "filter_model_files",
]
