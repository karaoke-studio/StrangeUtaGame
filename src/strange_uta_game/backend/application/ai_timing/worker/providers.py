"""forced-alignment provider 实现（阶段 C）。

Provider 抽象对应计划文档 §5.2 的 ForcedAlignmentProvider 接口；
推理实现吸收 yohane（Wav2Vec2 CTC + torchaudio forced_align）与
torchaudio MMS_FA bundle 的公开路径，不引入其 CLI 或私有 API。

重型依赖（torch / torchaudio / transformers / soundfile）全部在 load()
内部延迟导入：宿主与测试环境未安装 Runtime 时模块本身可正常导入，
错误在执行时转换为中文提示。
"""

import gc
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from strange_uta_game.backend.application.ai_timing.alignment import (
    AlignmentRequest,
    AlignmentResult,
    EmissionSpan,
    validate_windows,
    window_commit_ranges,
)

ProgressFn = Callable[[int, str], None]
CancelFn = Callable[[], bool]

DEFAULT_WAV2VEC2_MODEL = "NextFire/mms-300m-ForcedAligner-karaoke-ja-Latn"

# 尾音静音判据（2026-08 用户决策口径）：帧功率低于阈值为静音，且需
# 连续 min_frames 帧才认定「进入静音」（吸收换气与持续音的瞬时低谷）。
# v4（2026-09-14 用户决策）：阈值改为整轨帧功率对数直方图的 Otsu
# 谷底（类间方差最大切分点），逐轨自适应——分离残留电平因素材/分离
# 模型在 0.04~0.1×有声之间浮动，固定比例无法同时适配（0.1 判不出
# 静音、0.2 误切弱尾音）。Otsu 对单峰分布也会给出切分点，须以双峰
# 性护栏判其可信：两簇对数均值距 ≥6dB、每簇帧占比 ≥5%；不满足退回
# 固定比例兜底（0.15×P75，即 v3 判据，P75≈典型有声帧电平）。
TAIL_SILENCE_POWER_RATIO = 0.15
TAIL_SILENCE_MIN_FRAMES = 4
TAIL_SILENCE_OTSU_MIN_SEPARATION_DB = 6.0
TAIL_SILENCE_OTSU_MIN_SHARE = 0.05
TAIL_SILENCE_OTSU_BINS = 256
# 吸附回退（2026-09-07 用户决策）：静音判据失效时不再完全吸附到下一
# token 起点，而是回退该毫秒数——停顿点与下一字符首时间戳完全重合
# 会导致两个 Tag 重叠、不可点选；回退后仍不早于自身起点 + 同值
# （与前一时间戳至少保留 20ms，不产生逆序）。
TAIL_SNAP_BACKOFF_MS = 20


def apply_word_group_resplit(
    grouped: List[Tuple[int, int]],
    groups: List[List[int]],
    word_groups: List[List[int]],
) -> List[Tuple[int, int]]:
    """拉丁词组词内边界按子 token 数比例切分（原地，返回 grouped）。

    手工按音节/字母拆分的英文单位：整词字母序列的 CTC 端点（词首/词尾）
    可靠，词内边界不可靠（英文字母≠音素，Viterbi 常把切点落在元音中间
    或拖到下一词）。词内改为按各成员子 token 数加权均分，边界单调稳定。
    """
    for indexes in word_groups:
        if len(indexes) < 2:
            continue
        if any(not (0 <= i < len(grouped)) for i in indexes):
            continue
        span_start = grouped[indexes[0]][0]
        span_end = grouped[indexes[-1]][1]
        if span_end <= span_start:
            continue
        weights = [
            len(groups[i]) if 0 <= i < len(groups) and groups[i] else 1
            for i in indexes
        ]
        total = sum(weights)
        boundaries = [span_start]
        acc = 0
        for w in weights[:-1]:
            acc += w
            boundaries.append(
                span_start + int(round((span_end - span_start) * acc / total))
            )
        boundaries.append(span_end)
        for pos, i in enumerate(indexes):
            grouped[i] = (boundaries[pos], boundaries[pos + 1])
    return grouped


def _adaptive_min_frames(window_frames: int) -> int:
    """短窗自适应：连续静音帧数要求 = min(标准值, ceil(窗口帧数/2))，下限 1。

    20ms/帧下标准值 4 帧 = 80ms；字间间隙不足 80ms 时固定 4 帧永远凑
    不满，静音判定必然失败（终点只能整段吸附下一 token 起点）。短窗按
    「静音至少占窗口一半」折减，仍能吸收孤立的瞬时低谷。
    """
    if window_frames <= 0:
        return 1
    return max(1, min(TAIL_SILENCE_MIN_FRAMES, (window_frames + 1) // 2))


def _otsu_power_threshold(
    energies: Any,
    *,
    bins: int = TAIL_SILENCE_OTSU_BINS,
    min_separation_db: float = TAIL_SILENCE_OTSU_MIN_SEPARATION_DB,
    min_class_share: float = TAIL_SILENCE_OTSU_MIN_SHARE,
) -> Optional[float]:
    """整轨帧功率对数直方图的 Otsu 谷底阈值；双峰性不足返回 None。

    分离人声轨的帧功率分布双峰（残留底噪簇 + 发声簇），Otsu 在对数域
    取类间方差最大的切分点作谷底——残留电平因素材浮动时谷底自动跟移，
    这是固定比例（0.15×P75）做不到的。Otsu 对单峰分布同样会返回一个
    切分点（约在中位附近，无意义），故以护栏判可信：两簇对数均值距
    <min_separation_db（两簇未分开）或任一簇帧占比 <min_class_share
    （切分落在分布极端）时返回 None，由调用方退回固定比例兜底。
    纯函数便于单测。
    """
    try:
        import numpy as np
    except ImportError:
        return None
    arr = np.asarray(energies, dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    if arr.size < 16:
        return None
    # 数字静音/零功率统一地板到 1e-12：只把这类帧压到最左 bin，不改变
    # 帧间相对大小，避免 log(0)
    log_e = np.log(np.maximum(arr, 1e-12))
    lo = float(log_e.min())
    hi = float(log_e.max())
    if not hi > lo:
        return None
    hist, edges = np.histogram(log_e, bins=bins)
    hist = hist.astype(np.float64)
    total = float(hist.sum())
    if total <= 0:
        return None
    centers = (edges[:-1] + edges[1:]) / 2.0
    cum_h = np.cumsum(hist)
    cum_hx = np.cumsum(hist * centers)
    # 候选阈值取各 bin 上边缘（去掉最后一个，保证两类非空）；
    # 类0 = 低于阈值的帧（静音/残留簇），类1 = 高于（发声簇）
    omega0 = cum_h[:-1] / total
    omega1 = 1.0 - omega0
    valid = (omega0 >= min_class_share) & (omega1 >= min_class_share)
    if not valid.any():
        return None
    with np.errstate(invalid="ignore", divide="ignore"):
        mu0 = cum_hx[:-1] / np.maximum(cum_h[:-1], 1e-12)
        mu1 = (cum_hx[-1] - cum_hx[:-1]) / np.maximum(total - cum_h[:-1], 1e-12)
    sigma_b = omega0 * omega1 * (mu0 - mu1) ** 2
    best = int(np.nanargmax(np.where(valid, sigma_b, np.nan)))
    separation_db = float(mu1[best] - mu0[best]) * 10.0 / float(np.log(10.0))
    if separation_db < min_separation_db:
        return None
    return float(np.exp(edges[best + 1]))


def _silence_boundary(
    energies: Any, threshold: float, from_frame: int, to_frame: int
) -> int:
    """从 from_frame 向后找第一段持续静音的起始帧；未找到返回 to_frame。

    静音 = 帧功率低于 threshold（Otsu 谷底或兜底固定比例，由调用方
    决定），持续帧数要求按窗口长度自适应（_adaptive_min_frames）。
    """
    start = max(0, from_frame)
    min_frames = _adaptive_min_frames(to_frame - start)
    run = 0
    for f in range(start, to_frame):
        if float(energies[f]) < threshold:
            run += 1
            if run >= min_frames:
                return f - min_frames + 1
        else:
            run = 0
    return to_frame


def _trailing_silence_boundary(
    energies: Any, threshold: float, start_frame: int, end_frame: int
) -> int:
    """仅裁掉模型终点前连续的静音；不从模型终点向后延长。

    从模型 end_frame 向前搜索紧邻终点的持续静音，取其起点；
    不跨过重新发声的帧寻找更早的低谷，避免误截词内停顿。
    返回值不晚于 end_frame。
    """
    end = min(len(energies), max(0, end_frame))
    start = max(0, min(start_frame, end))
    run_start = end
    while run_start > start and float(energies[run_start - 1]) < threshold:
        run_start -= 1
    if end - run_start >= TAIL_SILENCE_MIN_FRAMES:
        return run_start
    return end


def _snap_end_with_backoff(start_ms: int, ctc_end_ms: int, cand_ms: int) -> int:
    """静音判据失效时的吸附终点：cand - 20ms，且不产生逆序。

    约束按序收敛（2026-09-07 用户决策）：
    - 回退后不早于自身起点 + TAIL_SNAP_BACKOFF_MS（与前一时间戳至少
      保留 20ms，停顿点不得早于本字符节拍起点）；
    - 不短于 CTC 原始终点（不切掉已确认的发声）；
    - 不越过 cand（下一 token 起点/音频末尾），保持 token 区间单调。
    极端小间隙下前两者与上限冲突时退化为紧贴 cand。
    """
    target = cand_ms - TAIL_SNAP_BACKOFF_MS
    target = max(target, start_ms + TAIL_SNAP_BACKOFF_MS, ctc_end_ms)
    return min(target, cand_ms)


def normalize_latn_text(text: str) -> str:
    """把 token 文本归一化为对齐器可接受的 Latn 转写（同 yohane 口径）。

    小写、弯撇号转直撇号，其余非 [a-z' ] 字符替换为空格并折叠。
    """
    text = text.lower().replace("’", "'")
    text = re.sub("([^a-z' ])", " ", text)
    text = re.sub(" +", " ", text)
    return text.strip()


class AlignmentProviderError(RuntimeError):
    """provider 执行错误（中文消息）。"""


class AlignmentCancelledError(AlignmentProviderError):
    """协作取消信号（worker 转换为 cancelled 消息）。"""


class ForcedAlignmentProvider(ABC):
    """对齐器 provider 抽象（§5.2）。"""

    provider_id: str = ""
    supports_windows: bool = False
    """是否支持 AlignmentRequest.windows 分窗解码。

    False 且请求携带非空 windows 时，align 入口抛
    AlignmentProviderError("当前对齐后端不支持分窗")——禁止静默忽略
    （宁可上层退化重试，不许悄悄整曲错解）。
    """

    @abstractmethod
    def validate_model(self, model_spec: Dict[str, Any]) -> None:
        """校验模型描述可用（缺文件/缺依赖时抛中文错误）。"""

    @abstractmethod
    def load(
        self,
        model_spec: Dict[str, Any],
        progress: ProgressFn,
        cancel: CancelFn,
    ) -> None:
        """加载模型到内存（任务内只加载一次）。"""

    @abstractmethod
    def align(
        self,
        request: AlignmentRequest,
        audio_path: str,
        progress: ProgressFn,
        cancel: CancelFn,
    ) -> AlignmentResult:
        """对音频执行 forced alignment，返回 AlignmentResult。"""

    def unload(self) -> None:
        """释放模型与设备资源（默认清引用 + 回收）。"""
        gc.collect()


def _cancelled_check(cancel: CancelFn) -> None:
    if cancel():
        raise AlignmentCancelledError("已取消")


def _require_windows_support(provider: ForcedAlignmentProvider, request) -> None:
    """请求带分窗且后端不支持 → 明确中文错误（禁止静默忽略）。"""
    if request.windows and not getattr(provider, "supports_windows", False):
        raise AlignmentProviderError("当前对齐后端不支持分窗")


def _find_encoder_layers(model: Any) -> List[Any]:
    """定位 transformer encoder 层列表（HF / torchaudio 各版本结构兼容）。

    - HF ``Wav2Vec2ForCTC``: ``wav2vec2.encoder.layers``
    - torchaudio 新版（MMS_FA 包装器）: ``model.encoder.transformer.layers``
    - torchaudio 旧版: ``encoder.layers``

    按 ``named_modules`` 扫描名字以 ``layers`` 结尾、路径含 encoder/
    transformer 的层容器，取最长者（transformer 块数最多、主导耗时）；
    找不到（结构变化/新版库）时返回空列表，调用方退化为无层进度。
    """
    best: List[Any] = []
    walker = getattr(model, "named_modules", None)
    if callable(walker):
        try:
            for name, mod in walker():
                if not name or not name.endswith("layers"):
                    continue
                if "encoder" not in name and "transformer" not in name:
                    continue
                try:
                    layers = list(mod)
                except TypeError:
                    continue
                if layers and len(layers) > len(best):
                    best = layers
        except Exception:
            pass
    if best:
        return best
    # 兜底：旧式显式属性路径（named_modules 缺失/异常的普通对象也可用）
    seen = set()
    for cand in (getattr(model, "wav2vec2", None), model):
        if cand is None or id(cand) in seen:
            continue
        seen.add(id(cand))
        encoder = getattr(cand, "encoder", None)
        layers = getattr(encoder, "layers", None)
        if layers is None or not hasattr(layers, "__len__"):
            continue
        try:
            return list(layers)
        except TypeError:
            continue
    return []


def resolve_device_pref(
    preference: str, cuda_available: bool, mps_available: bool
) -> str:
    """设备选择：auto 优先 CUDA → MPS → CPU；显式设备不可用时回退 CPU
    的判定由调用方给提示。纯函数便于单测（macOS 支持引入 MPS）。"""
    if preference == "auto":
        if cuda_available:
            return "cuda"
        if mps_available:
            return "mps"
        return "cpu"
    return preference


class FakeProvider(ForcedAlignmentProvider):
    """确定性假 provider：进程生命周期 / 协议 / 取消测试用。

    均分 token 区间（duration_ms / token 数），支持 options 控制：
    - ``fake_duration_ms``：音频总时长（默认 1000）；
    - ``fake_delay_ms``：每个 token 之间的延迟（模拟长任务，取消测试用）；
    - ``fake_crash``：load 阶段抛出异常（崩溃隔离测试用）；
    - ``fake_progress_mode``：align 循环 progress 发射模式（停滞看门狗
      测试用）：normal（默认，每 token 变化）/ static（每 token 同内容）/
      silent（不发）。
    """

    provider_id = "fake"
    supports_windows = True

    def validate_model(self, model_spec: Dict[str, Any]) -> None:
        if model_spec.get("fake_crash"):
            # 崩溃测试走 load 阶段
            return

    def load(
        self,
        model_spec: Dict[str, Any],
        progress: ProgressFn,
        cancel: CancelFn,
    ) -> None:
        progress(10, "加载测试模型")
        if model_spec.get("fake_crash"):
            raise AlignmentProviderError("测试崩溃：模拟模型加载失败")
        if model_spec.get("fake_crash_process"):
            import os

            os._exit(3)
        progress(50, "测试模型就绪")

    def align(
        self,
        request: AlignmentRequest,
        audio_path: str,
        progress: ProgressFn,
        cancel: CancelFn,
    ) -> AlignmentResult:
        _require_windows_support(self, request)
        options = request.options or {}
        duration = int(options.get("fake_duration_ms", 1000))
        delay = float(options.get("fake_delay_ms", 0))
        tokens = request.tokens
        if not tokens:
            raise AlignmentProviderError("没有可对齐的 token")
        if request.windows:
            validate_windows(
                request.windows, n_tokens=len(tokens), duration_ms=duration
            )
        step = duration / len(tokens)
        spans: List[EmissionSpan] = []
        import time

        # fake_progress_mode（停滞看门狗测试用）：
        # - "normal"（默认）：每个 token 发一条变化的 progress（percent/计数递增）；
        # - "static"：每个 token 发同一内容 progress（有输出但无变化）；
        # - "silent"：align 循环不发 progress（模拟挂死/无探针输出）。
        progress_mode = str(options.get("fake_progress_mode", "normal"))
        if request.windows:
            # 分窗路径：token 在各自窗口的提交范围（重叠区中点仲裁，见
            # window_commit_ranges）内均分——与真实 provider 的「整窗解码
            # + span 裁到提交范围」结果形态一致，保证拼接后全局单调
            commit_ranges = window_commit_ranges(request.windows)
            for w, commit in zip(request.windows, commit_ranges):
                tlo, thi = int(w["token_lo"]), int(w["token_hi"])
                count = thi - tlo
                if count <= 0 or commit is None:
                    continue
                c_lo, c_hi = commit
                wstep = (c_hi - c_lo) / count
                for j, i in enumerate(range(tlo, thi)):
                    _cancelled_check(cancel)
                    if delay:
                        time.sleep(delay / 1000.0)
                    token = tokens[i]
                    spans.append(
                        EmissionSpan(
                            token_index=token.index,
                            start_ms=int(c_lo + round(j * wstep)),
                            end_ms=int(c_lo + round((j + 1) * wstep)),
                            score=1.0,
                        )
                    )
                    if progress_mode == "normal":
                        progress(
                            50 + int(50 * len(spans) / max(1, len(tokens))),
                            f"对齐进度 {len(spans)}/{len(tokens)}",
                        )
                    elif progress_mode == "static":
                        progress(42, "对齐进度")
            return AlignmentResult(
                annotation_digest=request.annotation_digest,
                model_id="fake",
                spans=spans,
            )
        for i, token in enumerate(tokens):
            _cancelled_check(cancel)
            if delay:
                time.sleep(delay / 1000.0)
            spans.append(
                EmissionSpan(
                    token_index=token.index,
                    start_ms=int(round(i * step)),
                    end_ms=int(round((i + 1) * step)),
                    score=1.0,
                )
            )
            if progress_mode == "normal":
                progress(
                    50 + int(50 * (i + 1) / len(tokens)),
                    f"对齐进度 {i + 1}/{len(tokens)}",
                )
            elif progress_mode == "static":
                progress(42, "对齐进度")
        return AlignmentResult(
            annotation_digest=request.annotation_digest,
            model_id="fake",
            spans=spans,
        )


class _TorchProviderBase(ForcedAlignmentProvider):
    """torch 系 provider 共用逻辑：音频加载、CTC 对齐、帧→毫秒换算。"""

    supports_windows = True

    # Transformer 的峰值显存取决于单次 forward 的音频长度。
    FORWARD_CHUNK_SECONDS = 30
    FORWARD_CONTEXT_SECONDS = 3

    def __init__(self) -> None:
        self._model: Any = None
        self._device: Any = None
        self._model_id: str = ""
        self._ctc_blank: Optional[int] = None

    def _select_device(self, torch, preference: str, progress) -> Any:
        """按偏好选设备：auto = CUDA → MPS（Apple 芯片）→ CPU；
        显式 CUDA/MPS 不可用时回退 CPU 并给出提示而非崩溃。"""
        cuda = bool(torch.cuda.is_available())
        mps = False
        try:
            mps = bool(torch.backends.mps.is_available())
        except Exception:
            mps = False
        device = resolve_device_pref(preference, cuda, mps)
        if device in ("cuda", "mps") and not (cuda if device == "cuda" else mps):
            progress(18, f"未检测到可用的 {device.upper()}，回退 CPU 推理")
            device = "cpu"
        try:
            from strange_uta_game.backend.application.ai_timing.ailog import (
                ailog,
            )

            gpu_desc = (
                torch.cuda.get_device_name(0) if cuda and device == "cuda" else ""
            )
            ailog(
                "worker",
                f"设备选择：pref={preference} cuda={cuda} mps={mps} → {device}"
                + (f"（{gpu_desc}）" if gpu_desc else ""),
            )
        except Exception:
            pass
        return torch.device(device)

    def _import_torch(self):
        try:
            import torch
        except ImportError as exc:
            raise AlignmentProviderError(
                "对齐运行环境未安装（缺少 PyTorch）。请先在 AI 打轴弹窗中下载对齐运行环境"
            ) from exc
        return torch

    def _load_audio_mono(self, audio_path: str) -> Tuple[Any, int]:
        """加载音频为 (torch tensor [1, samples], sample_rate)。"""
        torch = self._import_torch()
        try:
            import soundfile as sf
        except ImportError as exc:
            raise AlignmentProviderError(
                "对齐运行环境缺少 soundfile，无法读取音频"
            ) from exc
        try:
            import numpy as np

            data, sample_rate = sf.read(audio_path, dtype="float32", always_2d=True)
        except Exception as exc:
            raise AlignmentProviderError(f"读取音频失败：{exc}") from exc
        waveform = torch.from_numpy(data.T)  # [channels, samples]
        if waveform.size(0) > 1:
            waveform = waveform.mean(dim=0, keepdim=True)
        return waveform, sample_rate

    def _ctc_align_groups(
        self, emission: Any, token_groups: Sequence[Sequence[int]], blank: int
    ) -> List[Tuple[int, int]]:
        """公开 API 路径的 CTC forced alignment。

        对展平的子 token 序列做 forced_align + merge_tokens，按分组
        聚合出每组的 (首子 token 起始帧, 末子 token 结束帧)。
        """
        # torchaudio 的 forced_align 在 CUDA emission 下要求 targets
        # 也在 CUDA（compute.cu:256 "targets must be a CUDA tensor"），
        # 设备组合的兼容矩阵难以逐一验证；CTC 对齐耗时相对 transformer
        # forward 可忽略，统一回 CPU 做，保证与 CPU 路径逐位一致
        if hasattr(emission, "cpu"):
            emission = emission.cpu()
        from torchaudio.functional import forced_align, merge_tokens

        torch = self._import_torch()
        flat = [t for group in token_groups for t in group]
        if not flat:
            raise AlignmentProviderError("没有可对齐的 token")
        # 新版 torchaudio 的 forced_align API：targets 必须是 [B, N] Tensor，
        # 且要求 input_lengths/target_lengths（yohane 基于旧版 list/None 签名）
        targets = torch.tensor([flat], dtype=torch.int64)
        alignments, scores = forced_align(
            emission,
            targets,
            input_lengths=torch.tensor([emission.size(1)]),
            target_lengths=torch.tensor([len(flat)]),
            blank=blank,
        )
        # align 返回 [B, T]；merge_tokens 接受一维
        spans = merge_tokens(
            alignments[0] if alignments.dim() > 1 else alignments,
            scores[0] if scores.dim() > 1 else scores,
            blank=blank,
        )
        grouped: List[Tuple[int, int]] = []
        offset = 0
        for group in token_groups:
            if not group:
                grouped.append((-1, -1))
                continue
            first = spans[offset]
            last = spans[offset + len(group) - 1]
            grouped.append((first.start, last.end))
            offset += len(group)
        return grouped

    def _ctc_align_groups_windowed(
        self,
        emission: Any,
        token_groups: Sequence[Sequence[int]],
        blank: int,
        windows: Sequence[Dict[str, Any]],
        duration_ms: int,
    ) -> List[Tuple[int, int]]:
        """按请求 windows 切 emission 帧区间做分窗 CTC 对齐。

        ms→帧换算用实际 emission 长度与音频时长比例（worker 侧完成，
        宿主不感知帧）。窗口内局部 span 加回帧偏移，再各自裁到提交范围
        （相邻窗重叠区中点，见 :func:`window_commit_ranges`），保证拼接
        后全局时间轴单调。无窗/单窗覆盖整曲时与整曲解码路径逐位一致
        （退化等价）。
        """
        validate_windows(
            windows, n_tokens=len(token_groups), duration_ms=duration_ms
        )
        if hasattr(emission, "cpu"):
            emission = emission.cpu()
        num_frames = int(emission.size(1))
        if duration_ms <= 0 or num_frames <= 0:
            raise AlignmentProviderError("分窗解码需要有效音频时长与 emission 帧")
        # 单窗覆盖整曲：直接走整曲路径，保证与 windows=None 逐位一致
        if (
            len(windows) == 1
            and int(windows[0]["token_lo"]) == 0
            and int(windows[0]["token_hi"]) == len(token_groups)
            and int(windows[0]["start_ms"]) <= 0
            and int(windows[0]["end_ms"]) >= duration_ms
        ):
            return self._ctc_align_groups(emission, token_groups, blank)
        grouped: List[Tuple[int, int]] = [(-1, -1)] * len(token_groups)
        commit_ranges = window_commit_ranges(windows)
        for w, commit in zip(windows, commit_ranges):
            tlo = int(w["token_lo"])
            thi = int(w["token_hi"])
            if thi <= tlo or commit is None:
                continue
            f0 = int(round(int(w["start_ms"]) * num_frames / duration_ms))
            f1 = int(round(int(w["end_ms"]) * num_frames / duration_ms))
            f0 = max(0, min(num_frames, f0))
            f1 = max(0, min(num_frames, f1))
            if f1 <= f0:
                if f0 >= num_frames:
                    continue
                f1 = f0 + 1
            sub = emission[:, f0:f1]
            sub_groups = list(token_groups[tlo:thi])
            sub_grouped = self._ctc_align_groups(sub, sub_groups, blank=blank)
            # 提交范围仲裁：窗口 end/相邻窗 start 带 margin 重叠（搜索边距），
            # 双方 span 各自裁到重叠区中点以内，拼接后才满足全局单调
            c_lo, c_hi = commit
            cf_lo = max(f0, int(round(c_lo * num_frames / duration_ms)))
            cf_hi = min(f1, int(round(c_hi * num_frames / duration_ms)))
            if cf_hi < cf_lo:
                cf_hi = cf_lo
            for i, g in enumerate(sub_grouped):
                if g[0] < 0:
                    grouped[tlo + i] = g
                else:
                    s = max(g[0] + f0, cf_lo)
                    e = min(g[1] + f0, cf_hi)
                    if e < s:
                        e = s
                    grouped[tlo + i] = (s, e)
        return grouped

    def _frame_energies(self, waveform: Any, num_frames: int) -> Any:
        """按 emission 帧计算平均功率（numpy 数组）；不可用时返回 None。

        返回 None 时尾音退回「吸附下一起点但回退 20ms」的行为。
        """
        try:
            import numpy as np

            wav = waveform.detach().cpu().numpy().reshape(-1)
        except Exception:
            return None
        n = int(wav.shape[0])
        if num_frames <= 0 or n <= num_frames:
            return None
        bounds = np.minimum(
            (np.arange(num_frames + 1) * (n / num_frames)).astype(np.int64), n
        )
        counts = np.diff(bounds)
        sums = np.add.reduceat(wav * wav, bounds[:-1])
        return sums / np.maximum(counts, 1)

    def _frames_to_spans(
        self,
        request: AlignmentRequest,
        groups: List[Tuple[int, int]],
        num_frames: int,
        num_samples: int,
        sample_rate: int,
        tail_snap: bool,
        waveform: Any = None,
    ) -> List[EmissionSpan]:
        """帧区间 → 毫秒 EmissionSpan；空组用相邻 token 插值补齐。

        显式 tail_correct=3 使用模型优先的行尾静音裁短；=0 原样返回。
        旧调用方未传 tail_correct 时沿用 tail_snap：吸附下一 token 起点（弥补 CTC 对
        长音/尾音的截断，FA-Kara 思路），再按整轨对数能量直方图的
        Otsu 谷底判据裁到静音边界——否则行尾尾音会一路延伸跨过整段
        间奏静音；末个 token 也借此把被 CTC 截断的真实尾音延伸到静音
        边界。双峰性不足时退回固定比例兜底（0.15×P75）；窗口内找不
        到持续静音时不再完全吸附，而是回退 20ms
        （_snap_end_with_backoff），避免停顿点与下一字符首时间戳重合。
        """
        ratio = (num_samples / num_frames) / sample_rate * 1000.0  # ms / frame
        spans: List[EmissionSpan] = []
        valid = [(i, g) for i, g in enumerate(groups) if g[0] >= 0]
        for i, token in enumerate(request.tokens):
            match = next(((gi, g) for gi, g in valid if gi == i), None)
            if match is None:
                # 归一化后无子 token 的读音：用前一个有效 token 终点 /
                # 后一个有效 token 起点插值
                prev_end = None
                next_start = None
                for gi, g in valid:
                    if gi < i:
                        prev_end = int(round(g[1] * ratio))
                    elif next_start is None:
                        next_start = int(round(g[0] * ratio))
                start = prev_end if prev_end is not None else (next_start or 0)
                end = next_start if next_start is not None else start
                spans.append(
                    EmissionSpan(token_index=token.index, start_ms=start, end_ms=end)
                )
                continue
            _, g = match
            spans.append(
                EmissionSpan(
                    token_index=token.index,
                    start_ms=int(round(g[0] * ratio)),
                    end_ms=int(round(g[1] * ratio)),
                )
            )
        # 新策略对应 FA-Kara tail_correct=3 的行尾范围，但结果按用户要求
        # 与模型原始端点取 min：微调模型默认 0 完全保留原始端点；MMS_FA
        # 只在原始端点已落入持续静音时裁短，不再延长任何 token。
        correction = (request.options or {}).get("tail_correct")
        if correction is not None:
            if correction != 3 or not tail_snap or waveform is None:
                return spans
            energies = self._frame_energies(waveform, num_frames)
            if energies is None:
                return spans
            import numpy as np

            threshold = _otsu_power_threshold(energies)
            if threshold is None:
                threshold = TAIL_SILENCE_POWER_RATIO * float(
                    np.percentile(energies, 75)
                )
            trimmed = 0
            for i, cur in enumerate(spans):
                if i + 1 < len(spans) and (
                    request.tokens[i].line_idx == request.tokens[i + 1].line_idx
                ):
                    continue
                end_frame = min(num_frames, int(np.ceil(cur.end_ms / ratio)))
                start_frame = max(
                    int(cur.start_ms / ratio), end_frame - int(800 / ratio)
                )
                boundary = _trailing_silence_boundary(
                    energies, threshold, start_frame, end_frame
                )
                candidate = int(round(boundary * ratio))
                new_end = max(cur.start_ms, min(cur.end_ms, candidate))
                if new_end < cur.end_ms:
                    spans[i] = EmissionSpan(
                        token_index=cur.token_index,
                        start_ms=cur.start_ms,
                        end_ms=new_end,
                        score=cur.score,
                    )
                    trimmed += 1
            try:
                from strange_uta_game.backend.application.ai_timing.ailog import ailog

                ailog("worker", f"模型尾音优先：行尾静音裁短 {trimmed} 个 token")
            except Exception:
                pass
            return spans
        if tail_snap:
            energies = (
                self._frame_energies(waveform, num_frames)
                if waveform is not None
                else None
            )
            threshold = 0.0
            criterion = "无波形（仅吸附回退）"
            if energies is not None:
                import numpy as np

                otsu = _otsu_power_threshold(energies)
                if otsu is not None:
                    threshold = otsu
                    criterion = f"Otsu 谷底阈值 {otsu:.4g}"
                else:
                    # 兜底判据基线取能量上四分位（≈典型有声帧电平），
                    # 而非全轨均值：人声轨大部分帧是句间静音/分离残留，
                    # 全轨均值被拉到极低，固定比例×均值的阈值形同虚设
                    mean_power = float(np.percentile(energies, 75))
                    threshold = TAIL_SILENCE_POWER_RATIO * mean_power
                    criterion = f"兜底固定比例 {threshold:.4g}（0.15×P75）"
            audio_end_ms = int(round(num_frames * ratio))
            hits = attempted = 0
            for i, cur in enumerate(spans):
                cand = (
                    spans[i + 1].start_ms if i + 1 < len(spans) else audio_end_ms
                )
                if cur.end_ms >= cand:
                    continue
                if energies is not None:
                    attempted += 1
                    raw_end_f = min(num_frames - 1, int(cur.end_ms / ratio))
                    cand_f = min(num_frames, int(cand / ratio))
                    boundary_f = _silence_boundary(
                        energies, threshold, raw_end_f, cand_f
                    )
                    if boundary_f >= cand_f:
                        # 窗口内找不到持续静音：吸附但回退 20ms，
                        # 不与下一 token 起点完全重合
                        new_end = _snap_end_with_backoff(
                            cur.start_ms, cur.end_ms, cand
                        )
                    else:
                        hits += 1
                        new_end = int(round(boundary_f * ratio))
                        # 不短于 CTC 原始终点，不超过下一 token 起点/音频末尾
                        new_end = max(cur.end_ms, min(new_end, cand))
                    if new_end <= cur.start_ms:
                        continue
                else:
                    new_end = _snap_end_with_backoff(
                        cur.start_ms, cur.end_ms, cand
                    )
                spans[i] = EmissionSpan(
                    token_index=cur.token_index,
                    start_ms=cur.start_ms,
                    end_ms=new_end,
                    score=cur.score,
                )
            if energies is not None:
                # 命中率日志（2026-09-14 用户决策）：判据调参不再靠体感，
                # 每次对齐输出静音判据窗口内实际裁到边界的 token 数
                try:
                    from strange_uta_game.backend.application.ai_timing.ailog import (
                        ailog,
                    )

                    ailog(
                        "worker",
                        f"尾音静音判据：{criterion}，命中 {hits}/{attempted} 个 token",
                    )
                except Exception:
                    pass
        return spans

    def _forward_with_layer_progress(
        self,
        run_forward: Callable[[], Any],
        progress: ProgressFn,
        lo: int = 66,
        hi: int = 84,
    ) -> Any:
        """执行一次模型 forward，按 encoder 层上报真实推理进度。

        通过 ``register_forward_hook`` 观察每层完成时刻——不改变任何
        计算与输出（与 FA-Kara/yohane 的一次整段 forward 完全一致），
        但把最耗时的推理阶段拆成 N 层的细粒度进度，ETA 有真实数据
        可算（各层耗时近似均匀，进度与时间近似线性）。

        结构上找不到层列表时静默退化为无层进度（只保留阶段消息）。
        """
        layers = _find_encoder_layers(self._model)
        n = len(layers)
        if not n:
            return run_forward()
        handles: List[Any] = []
        counter = {"done": 0}

        def _make_hook():
            def _hook(_module, _inputs, _output):
                counter["done"] += 1
                done = counter["done"]
                progress(
                    lo + int((hi - lo) * done / n),
                    f"模型推理中（编码器层 {done}/{n}）",
                )

            return _hook

        try:
            for layer in layers:
                handles.append(layer.register_forward_hook(_make_hook()))
            return run_forward()
        finally:
            for handle in handles:
                handle.remove()

    def _forward_audio_chunks(
        self,
        waveform: Any,
        sample_rate: int,
        forward: Callable[[Any], Any],
        progress: ProgressFn,
        cancel: CancelFn,
    ) -> Any:
        """限制单次推理长度，在 CPU 拼接 emission 后仍做全曲对齐。"""
        torch = self._import_torch()
        total = waveform.size(1)
        chunk = self.FORWARD_CHUNK_SECONDS * sample_rate
        if total <= chunk:
            return self._forward_with_layer_progress(
                lambda: forward(waveform), progress
            )
        context = int(self.FORWARD_CONTEXT_SECONDS * sample_rate)
        pieces = []
        count = (total + chunk - 1) // chunk
        for i, start in enumerate(range(0, total, chunk)):
            _cancelled_check(cancel)
            end = min(start + chunk, total)
            left = max(0, start - context)
            right = min(total, end + context)
            part = forward(waveform[:, left:right]).detach().cpu()
            frames = part.size(1)
            first = round((start - left) * frames / (right - left))
            last = round((end - left) * frames / (right - left))
            pieces.append(part[:, first:last])
            del part
            progress(
                65 + int(19 * (i + 1) / count),
                f"模型分段推理中（{i + 1}/{count}）",
            )
        return torch.cat(pieces, dim=1)

    def unload(self) -> None:
        self._model = None
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass


class Wav2Vec2LatnProvider(_TorchProviderBase):
    """微调 Wav2Vec2 CTC 模型（默认 NextFire/mms-300m-ForcedAligner-karaoke-ja-Latn）。

    输入为 Latn（罗马字/无调拼音）转写；每个 checkpoint 读音编码为一组
    子 token，CTC 对齐后聚合为该 checkpoint 的区间。
    """

    provider_id = "wav2vec2"

    def validate_model(self, model_spec: Dict[str, Any]) -> None:
        model_id = str(model_spec.get("model_id") or DEFAULT_WAV2VEC2_MODEL)
        if not model_id:
            raise AlignmentProviderError("未指定对齐模型")

    def load(
        self,
        model_spec: Dict[str, Any],
        progress: ProgressFn,
        cancel: CancelFn,
    ) -> None:
        progress(12, "加载 PyTorch…（首次可能需要 1-3 分钟）")
        torch = self._import_torch()
        progress(16, "加载 Transformers…（首次可能需要 1-3 分钟）")
        try:
            from transformers import Wav2Vec2ForCTC, Wav2Vec2Processor
        except ImportError as exc:
            raise AlignmentProviderError(
                "对齐运行环境未安装 Transformers，请先下载对齐运行环境"
            ) from exc
        self._model_id = str(model_spec.get("model_id") or DEFAULT_WAV2VEC2_MODEL)
        device = str(model_spec.get("device") or "auto")
        self._device = self._select_device(torch, device, progress)
        progress(20, f"加载对齐模型 {self._model_id}")
        _cancelled_check(cancel)
        try:
            self._processor = Wav2Vec2Processor.from_pretrained(self._model_id)
        except Exception as exc:
            # 只有「目录里根本没有 processor/preprocessor 配置」时才用默认
            # 特征参数补齐（用户自行放入的模型常只带 tokenizer）。配置存在
            # 却加载失败说明是别的问题，静默换成默认参数会丢掉用户设置
            # 的采样率/归一化参数，反而让对齐质量悄悄退化。
            if self._has_processor_config():
                raise AlignmentProviderError(
                    f"对齐模型加载失败：{exc}"
                ) from exc
            try:
                from transformers import Wav2Vec2CTCTokenizer, Wav2Vec2FeatureExtractor

                tokenizer = Wav2Vec2CTCTokenizer.from_pretrained(self._model_id)
                feature_extractor = Wav2Vec2FeatureExtractor(
                    feature_size=1,
                    sampling_rate=16000,
                    padding_value=0.0,
                    do_normalize=True,
                    return_attention_mask=False,
                )
                self._processor = Wav2Vec2Processor(
                    feature_extractor=feature_extractor, tokenizer=tokenizer
                )
            except Exception as exc2:
                raise AlignmentProviderError(
                    "对齐模型加载失败（仅支持 Wav2Vec2 家族的 CTC 模型）："
                    f"{exc2}"
                ) from exc2
        try:
            self._model, loading_info = Wav2Vec2ForCTC.from_pretrained(
                self._model_id, output_loading_info=True
            )
        except Exception as exc:
            raise AlignmentProviderError(
                "对齐模型加载失败（仅支持 Wav2Vec2 家族的 CTC 模型）："
                f"{exc}"
            ) from exc
        self._reject_non_ctc_model(loading_info)
        self._ctc_blank = self._resolve_ctc_blank()
        self._model.to(self._device)
        progress(50, "对齐模型就绪")

    def _has_processor_config(self) -> bool:
        """模型目录里是否自带 processor/preprocessor 配置（决定能否兜底）。"""
        try:
            directory = Path(self._model_id)
        except (OSError, ValueError):
            return True
        if not directory.is_dir():
            # 仓库 ID（预检已拦）：不猜，保留原始错误
            return True
        return any(
            (directory / name).is_file()
            for name in ("preprocessor_config.json", "processor_config.json")
        )

    def _reject_non_ctc_model(self, loading_info) -> None:
        """拒绝基座模型/非 Wav2Vec2 CTC 模型（随机初始化的 CTC 头会静默出错）。"""
        config = self._model.config
        architectures = getattr(config, "architectures", None)
        if isinstance(architectures, str):
            architectures = [architectures]
        names = [str(arch) for arch in (architectures or []) if arch]
        if names and not any(
            name.casefold() == "wav2vec2forctc" for name in names
        ):
            raise AlignmentProviderError(
                "对齐模型不是 Wav2Vec2 CTC 模型"
                f"（架构：{'、'.join(names)}），"
                "仅支持 Wav2Vec2 家族的 Wav2Vec2ForCTC"
            )
        missing = [
            str(key)
            for key in ((loading_info or {}).get("missing_keys") or [])
        ]
        if any(key.startswith("lm_head.") for key in missing):
            # 分类头没权重 = 加载的是基座/非 CTC 检查点；from_pretrained
            # 只会警告并随机初始化，推理结果无法使用
            raise AlignmentProviderError(
                "模型缺少 CTC 分类头权重（lm_head.*），不是训练好的 "
                "Wav2Vec2ForCTC 模型，无法用于强制对齐"
            )

    def _resolve_ctc_blank(self) -> int:
        """确定 CTC blank id；缺失或越界时给出可操作的中文错误。"""
        config = self._model.config
        blank = getattr(config, "pad_token_id", None)
        if blank is None:
            blank = getattr(self._processor.tokenizer, "pad_token_id", None)
        if blank is None:
            raise AlignmentProviderError(
                "模型未声明 CTC blank（config.pad_token_id 与分词器 pad 均为空），"
                "无法进行强制对齐；请更换为训练好的 Wav2Vec2 CTC 模型"
            )
        blank = int(blank)
        vocab_size = getattr(config, "vocab_size", None)
        if vocab_size and not 0 <= blank < int(vocab_size):
            raise AlignmentProviderError(
                f"模型的 CTC blank（{blank}）超出词表范围（vocab_size="
                f"{vocab_size}），分词器与模型不匹配"
            )
        return blank

    def align(
        self,
        request: AlignmentRequest,
        audio_path: str,
        progress: ProgressFn,
        cancel: CancelFn,
    ) -> AlignmentResult:
        _require_windows_support(self, request)
        if self._model is None:
            raise AlignmentProviderError("对齐模型尚未加载")
        torch = self._import_torch()
        from torchaudio.functional import resample

        progress(55, "读取音频")
        waveform, sample_rate = self._load_audio_mono(audio_path)
        _cancelled_check(cancel)

        target_sr = int(self._processor.feature_extractor.sampling_rate)
        if sample_rate != target_sr:
            waveform = resample(waveform, sample_rate, target_sr)
            sample_rate = target_sr

        texts = [normalize_latn_text(t.text) for t in request.tokens]
        tokenizer = self._processor.tokenizer
        groups = [
            tokenizer.encode(text, add_special_tokens=False) if text else []
            for text in texts
        ]
        progress(65, "模型推理中")
        _cancelled_check(cancel)
        def _run_forward(part):
            inputs = self._processor(
                audio=part.numpy(),
                sampling_rate=sample_rate,
                return_tensors="pt",
            )
            outputs = self._model(**inputs.to(self._device))
            return torch.nn.functional.log_softmax(outputs.logits, dim=-1)

        with torch.inference_mode():
            emission = self._forward_audio_chunks(
                waveform, sample_rate, _run_forward, progress, cancel
            )

        progress(85, "计算对齐区间")
        blank = self._ctc_blank
        if blank is None:
            blank = self._resolve_ctc_blank()
        num_classes = int(emission.size(-1))
        if not 0 <= blank < num_classes:
            raise AlignmentProviderError(
                f"模型输出的分类维度（{num_classes}）与 CTC blank（{blank}）"
                "不一致，分词器与模型不匹配"
            )
        if any(blank in group for group in groups):
            # tokenizer 把 blank 编进了待对齐文本：forced_align 会拒绝或
            # 静默错位，属于模型与分词器不配套
            raise AlignmentProviderError(
                "分词器把 CTC blank token 编进了歌词文本（分词器与模型不匹配），"
                "请更换为 Wav2Vec2 CTC 模型"
            )
        duration_ms = int(round(waveform.size(1) / sample_rate * 1000))
        if request.windows:
            grouped = self._ctc_align_groups_windowed(
                emission, groups, blank, request.windows, duration_ms
            )
        else:
            grouped = self._ctc_align_groups(emission, groups, blank=blank)
        apply_word_group_resplit(grouped, groups, request.word_groups)
        spans = self._frames_to_spans(
            request,
            grouped,
            num_frames=emission.size(1),
            num_samples=waveform.size(1),
            sample_rate=sample_rate,
            tail_snap=bool((request.options or {}).get("tail_snap", True)),
            waveform=waveform,
        )
        progress(100, "对齐完成")
        return AlignmentResult(
            annotation_digest=request.annotation_digest,
            model_id=self._model_id,
            spans=spans,
        )


class MmsFaProvider(_TorchProviderBase):
    """torchaudio MMS_FA 基础模型（备选，许可证限制较少）。"""

    provider_id = "mms_fa"

    def validate_model(self, model_spec: Dict[str, Any]) -> None:
        if not str(model_spec.get("model_id") or "MMS_FA"):
            raise AlignmentProviderError("未指定对齐模型")

    def load(
        self,
        model_spec: Dict[str, Any],
        progress: ProgressFn,
        cancel: CancelFn,
    ) -> None:
        torch = self._import_torch()
        from torchaudio.pipelines import MMS_FA

        self._model_id = "MMS_FA"
        device = str(model_spec.get("device") or "auto")
        self._device = self._select_device(torch, device, progress)
        progress(20, "加载 MMS_FA 对齐模型")
        _cancelled_check(cancel)
        try:
            self._bundle = MMS_FA
            self._tokenizer = self._bundle.get_tokenizer()
            self._aligner = self._bundle.get_aligner()
            self._model = self._bundle.get_model()
        except Exception as exc:
            raise AlignmentProviderError(f"MMS_FA 模型加载失败：{exc}") from exc
        self._model.to(self._device)
        progress(50, "MMS_FA 模型就绪")

    def align(
        self,
        request: AlignmentRequest,
        audio_path: str,
        progress: ProgressFn,
        cancel: CancelFn,
    ) -> AlignmentResult:
        _require_windows_support(self, request)
        if self._model is None:
            raise AlignmentProviderError("对齐模型尚未加载")
        torch = self._import_torch()
        from torchaudio.functional import resample

        progress(55, "读取音频")
        waveform, sample_rate = self._load_audio_mono(audio_path)
        _cancelled_check(cancel)

        target_sr = int(self._bundle.sample_rate)
        if sample_rate != target_sr:
            waveform = resample(waveform, sample_rate, target_sr)
            sample_rate = target_sr
        waveform = waveform.mean(0, keepdim=True)

        texts = [normalize_latn_text(t.text) for t in request.tokens]
        # MMS_FA tokenizer 按 uroman 转写整词序列；空文本产生空组，
        # 空组不参与 bundle aligner（其内部按词插入 <star>）
        encoded = self._tokenizer(texts)
        groups = [
            list(ids) if text else [] for text, ids in zip(texts, encoded)
        ]

        progress(65, "模型推理中")
        _cancelled_check(cancel)

        def _run_forward(part):
            out, _ = self._model(part.to(self._device))
            return out

        with torch.inference_mode():
            emission = self._forward_audio_chunks(
                waveform, sample_rate, _run_forward, progress, cancel
            )

        progress(85, "计算对齐区间")
        duration_ms = int(round(waveform.size(1) / sample_rate * 1000))
        if request.windows:
            # bundle aligner 与 CTC 同为帧级 forced alignment：按窗切
            # emission，对窗内 token 做 aligner，帧偏移加回
            validate_windows(
                request.windows, n_tokens=len(groups), duration_ms=duration_ms
            )
            if hasattr(emission, "cpu"):
                emission = emission.cpu()
            num_frames = int(emission.size(1))
            grouped = [(-1, -1)] * len(groups)
            commit_ranges = window_commit_ranges(request.windows)
            for w, commit in zip(request.windows, commit_ranges):
                tlo, thi = int(w["token_lo"]), int(w["token_hi"])
                if thi <= tlo or commit is None:
                    continue
                f0 = max(
                    0,
                    min(
                        num_frames,
                        int(round(int(w["start_ms"]) * num_frames / duration_ms)),
                    ),
                )
                f1 = max(
                    0,
                    min(
                        num_frames,
                        int(round(int(w["end_ms"]) * num_frames / duration_ms)),
                    ),
                )
                if f1 <= f0:
                    if f0 >= num_frames:
                        continue
                    f1 = f0 + 1
                sub_groups = [g for g in groups[tlo:thi] if g]
                if not sub_groups:
                    continue
                sub_emission = emission[:, f0:f1]
                token_spans = self._aligner(
                    sub_emission[0] if sub_emission.dim() > 1 else sub_emission,
                    sub_groups,
                )
                # 与 CTC 分窗同款提交范围仲裁：裁到重叠区中点以内，
                # 保证拼接后全局时间轴单调
                c_lo, c_hi = commit
                cf_lo = max(f0, int(round(c_lo * num_frames / duration_ms)))
                cf_hi = min(f1, int(round(c_hi * num_frames / duration_ms)))
                if cf_hi < cf_lo:
                    cf_hi = cf_lo
                span_iter = iter(token_spans)
                for i, g in enumerate(groups[tlo:thi]):
                    if not g:
                        continue
                    sp = next(span_iter)
                    s = max(sp[0].start + f0, cf_lo)
                    e = min(sp[-1].end + f0, cf_hi)
                    if e < s:
                        e = s
                    grouped[tlo + i] = (s, e)
        else:
            non_empty = [g for g in groups if g]
            # bundle aligner 内部同样要求 emission/targets 同设备：统一 CPU
            token_spans = self._aligner(
                emission[0].cpu() if hasattr(emission, "cpu") else emission[0],
                non_empty,
            )
            grouped = []
            span_iter = iter(token_spans)
            for g in groups:
                if not g:
                    grouped.append((-1, -1))
                    continue
                sp = next(span_iter)
                grouped.append((sp[0].start, sp[-1].end))
        apply_word_group_resplit(grouped, groups, request.word_groups)
        spans = self._frames_to_spans(
            request,
            grouped,
            num_frames=emission.size(1),
            num_samples=waveform.size(1),
            sample_rate=sample_rate,
            tail_snap=bool((request.options or {}).get("tail_snap", True)),
            waveform=waveform,
        )
        progress(100, "对齐完成")
        return AlignmentResult(
            annotation_digest=request.annotation_digest,
            model_id=self._model_id,
            spans=spans,
        )


_REGISTRY: Dict[str, type] = {
    FakeProvider.provider_id: FakeProvider,
    Wav2Vec2LatnProvider.provider_id: Wav2Vec2LatnProvider,
    MmsFaProvider.provider_id: MmsFaProvider,
}


def create_provider(model_spec: Dict[str, Any]) -> ForcedAlignmentProvider:
    """按 model_spec["provider"] 创建 provider；环境变量
    ``SUG_AITIMING_FAKE_PROVIDER=1`` 强制使用 fake（测试/离线诊断）。"""
    import os

    provider_id = str(model_spec.get("provider") or "wav2vec2")
    if os.environ.get("SUG_AITIMING_FAKE_PROVIDER") == "1":
        provider_id = "fake"
    cls = _REGISTRY.get(provider_id)
    if cls is None:
        raise AlignmentProviderError(
            f"未知的对齐 provider：{provider_id}（可用：{'、'.join(_REGISTRY)}）"
        )
    return cls()


__all__ = [
    "ForcedAlignmentProvider",
    "AlignmentProviderError",
    "AlignmentCancelledError",
    "FakeProvider",
    "Wav2Vec2LatnProvider",
    "MmsFaProvider",
    "create_provider",
    "normalize_latn_text",
    "DEFAULT_WAV2VEC2_MODEL",
]
