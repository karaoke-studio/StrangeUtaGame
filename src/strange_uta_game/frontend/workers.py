"""后台工作线程 — 将耗时 I/O 和 CPU 操作移出 UI 线程。

所有 Worker 均为 QObject，配合 QThread 使用 moveToThread 模式。
调用方负责创建 QThread、moveToThread、连接信号、启动。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

if TYPE_CHECKING:
    from strange_uta_game.backend.infrastructure.audio import IAudioEngine
    from strange_uta_game.backend.domain import Project


# ──────────────────────────────────────────────
# 声谱图 / BPM 检测
# ──────────────────────────────────────────────


class PeakLevelWorker(QObject):
    """后台构建波形峰值层（min/max/RMS 多 bin 层金字塔）。

    纯计算包装 backend.infrastructure.audio.spectrum 的
    build_peak_levels_single_pass；结果为 {bin_size: (mins, maxs, rmss)}。
    """

    progress = pyqtSignal(float)
    finished = pyqtSignal(object)  # dict | None（取消）
    error = pyqtSignal(str)

    def __init__(self, samples: np.ndarray):
        super().__init__()
        self._samples = samples
        self._cancelled = False

    def request_cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            from strange_uta_game.backend.infrastructure.audio import spectrum

            result = spectrum.build_peak_levels_single_pass(
                self._samples,
                cancel_check=lambda: self._cancelled,
                progress_cb=self.progress.emit,
            )
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class SpectrogramWorker(QObject):
    """后台计算整段音频的声谱图（uint8 dB 矩阵 + 预算内金字塔，≈140ms/分钟音频）。

    samples 必须是单声道 float32；调用方保证计算期间不就地修改该数组。
    取消时同样发出 ``finished``（载荷 None）——调用方据此做纯信号驱动的
    线程回收，UI 路径不得同步 ``wait()``。金字塔总内存超过
    ``spectrum_core.PYRAMID_BUDGET_BYTES``（长音频）时 result 不带 levels，
    由渲染侧按需惰性构建单个所需层。
    """

    progress = pyqtSignal(float)   # 0.0~1.0
    finished = pyqtSignal(object)  # 结果 dict；取消时为 None
    error = pyqtSignal(str)

    def __init__(
        self,
        samples: np.ndarray,
        sample_rate: int,
        fft_size: int,
        hop: Optional[int] = None,
    ):
        super().__init__()
        self._samples = samples
        self._sample_rate = sample_rate
        self._fft_size = fft_size
        # 窗口重叠（帧距）：hop 越小时间粒度越细，计算量与内存越大
        self._hop = hop
        self._cancelled = False

    def request_cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            from strange_uta_game.backend.infrastructure.audio import spectrum as spectrum_core

            result = spectrum_core.compute_spectrogram(
                self._samples,
                self._sample_rate,
                self._fft_size,
                hop=self._hop,
                progress_cb=self.progress.emit,
                cancel_check=lambda: self._cancelled,
            )
            if result is None:
                self.finished.emit(None)  # 已取消：仍发信号驱动线程回收
                return
            # 金字塔在 worker 线程构建，避免 UI 回调里几十~几百 ms 的停顿。
            # 超预算（长音频）：构建受预算限制的中间粗层金字塔，渲染侧深缩放
            # 用可见切片归约——UI 绘制路径永远不同步构建完整层。
            if result["matrix"].nbytes * 2 <= spectrum_core.PYRAMID_BUDGET_BYTES:
                result["levels"] = spectrum_core.build_pyramid(result["matrix"])
            else:
                result["levels"] = None
                mid, coarse = spectrum_core.coarse_pyramid_for_budget(
                    result["matrix"], spectrum_core.PYRAMID_BUDGET_BYTES
                )
                result["coarse_mid"] = mid
                result["coarse_levels"] = coarse
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class BpmDetectWorker(QObject):
    """后台检测音频 BPM（librosa 管线的纯 numpy 复刻，见 spectrum_core.detect_bpm）。

    finished 携带 {"bpm": float | None, "confidence": 0.0~1.0}；
    bpm=None 表示未能识别。结果应回填给用户确认，不静默生效。
    """

    progress = pyqtSignal(float)
    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, samples: np.ndarray, sample_rate: int):
        super().__init__()
        self._samples = samples
        self._sample_rate = sample_rate
        self._cancelled = False

    def request_cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            from strange_uta_game.backend.infrastructure.audio import spectrum as spectrum_core

            result = spectrum_core.detect_bpm(
                self._samples,
                self._sample_rate,
                progress_cb=self.progress.emit,
                cancel_check=lambda: self._cancelled,
            )
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


# ──────────────────────────────────────────────
# 音频 / 视频
# ──────────────────────────────────────────────


class AudioLoadWorker(QObject):
    """在后台线程加载音频文件到引擎。"""

    progress = pyqtSignal(str, float)  # (stage, 0.0~1.0)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, engine: IAudioEngine, file_path: str):
        super().__init__()
        self._engine = engine
        self._file_path = file_path

    def run(self) -> None:
        try:
            self._engine.stop()
            from strange_uta_game.backend.infrastructure.audio.video_converter import (
                clear_extracted_cache,
            )
            clear_extracted_cache()
            self._engine.load(self._file_path, progress_cb=self.progress.emit)
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))


class VideoExtractWorker(QObject):
    """从视频提取音频并加载到引擎（编辑器用）。"""

    progress = pyqtSignal(str, float)
    finished = pyqtSignal(str)  # temp_path，供调用方清理
    error = pyqtSignal(str)

    def __init__(self, engine: IAudioEngine, file_path: str):
        super().__init__()
        self._engine = engine
        self._file_path = file_path

    def run(self) -> None:
        temp_path = None
        try:
            from strange_uta_game.backend.infrastructure.audio.video_converter import (
                extract_audio,
            )

            self.progress.emit(self.tr("正在提取音频..."), 0.0)
            temp_path = extract_audio(self._file_path, progress_cb=self.progress.emit)

            self._engine.stop()
            self._engine.load(temp_path, progress_cb=self.progress.emit)
            self.finished.emit(temp_path or "")
        except Exception as e:
            self.error.emit(str(e))


class VideoExtractOnlyWorker(QObject):
    """仅从视频提取音频（不加载到引擎），用于首页。"""

    progress = pyqtSignal(str, float)
    finished = pyqtSignal(str)  # extracted audio path
    error = pyqtSignal(str)

    def __init__(self, file_path: str):
        super().__init__()
        self._file_path = file_path

    def run(self) -> None:
        try:
            from strange_uta_game.backend.infrastructure.audio.video_converter import (
                extract_audio,
            )

            self.progress.emit(self.tr("正在提取音频..."), 0.0)
            temp_path = extract_audio(self._file_path, progress_cb=self.progress.emit)
            self.finished.emit(temp_path)
        except Exception as e:
            self.error.emit(str(e))


# ──────────────────────────────────────────────
# 项目
# ──────────────────────────────────────────────


class ProjectLoadWorker(QObject):
    """后台加载 .sug 项目文件。"""

    finished = pyqtSignal(object, str, object)  # (Project, file_path, extras)
    error = pyqtSignal(str)

    def __init__(self, file_path: str):
        super().__init__()
        self._file_path = file_path

    def run(self) -> None:
        from time import perf_counter

        from strange_uta_game.frontend.perf_log import log_elapsed, perf_enabled

        _perf_start = perf_counter() if perf_enabled() else None
        try:
            from strange_uta_game.backend.infrastructure.persistence.sug_io import (
                SugProjectParser,
            )

            # 单次解析同时拿到 Project 与 extras，UI 侧不再二次解析文件
            project, extras = SugProjectParser.load_with_extras(self._file_path)
            if _perf_start is not None:
                # path 只记文件名：perf 日志的 key=value 分词对含空格的
                # 全路径不健壮，且无需把用户目录结构写进本地诊断日志
                from pathlib import Path

                log_elapsed(
                    "workers.project_load",
                    _perf_start,
                    lines=len(getattr(project, "sentences", []) or []),
                    path=Path(self._file_path).name,
                )
            self.finished.emit(project, self._file_path, extras)
        except Exception as e:
            self.error.emit(str(e))


class ProjectSaveWorker(QObject):
    """后台写盘 .sug 项目文件。

    接收主线程 ``SugProjectParser.serialize()`` 产出的纯 dict 快照——
    快照不引用任何 domain 对象，后台写盘期间 UI 继续编辑不影响内容，
    无需再对 Project 做 deepcopy（大项目主线程几十 ms → 几 ms）。
    """

    progress = pyqtSignal(str)  # stage description
    finished = pyqtSignal(str)  # saved path
    error = pyqtSignal(str)

    def __init__(self, data: dict, file_path: str):
        super().__init__()
        self._data = data
        self._file_path = file_path

    def run(self) -> None:
        try:
            from strange_uta_game.backend.infrastructure.persistence.sug_io import (
                SugProjectParser,
            )

            SugProjectParser.write(
                self._data, self._file_path, progress_cb=self.progress.emit
            )
            self.finished.emit(self._file_path)
        except Exception as e:
            self.error.emit(str(e))


# ──────────────────────────────────────────────
# 注音分析
# ──────────────────────────────────────────────


class RubyAnalyzeWorker(QObject):
    """在后台线程对项目副本执行注音分析。

    AutoCheckService 由调用方在主线程创建（确保 WinRT STA apartment 正确初始化），
    worker 只负责在自己线程调用 init_apartment 后执行分析计算，避免阻塞 UI。
    """

    progress = pyqtSignal(str, int, int)  # (phase, current_line, total_lines)
    llm_waiting = pyqtSignal()         # LLM 整首批量请求发出、等待返回中
    llm_progress = pyqtSignal(str)     # LLM 内部阶段文本（请求/解析/重试/轮次）
    finished = pyqtSignal(object, int) # (analyzed_project_copy, deleted_count)
    error = pyqtSignal(str)

    def __init__(
        self,
        project_copy: "Project",
        auto_check,
        only_noruby: bool,
        delete_types: list,
        llm_apply_user_dict: bool = True,
        update_checkpoints: bool = True,
    ):
        super().__init__()
        self._project = project_copy
        self._auto_check = auto_check
        self._only_noruby = only_noruby
        self._delete_types = delete_types
        # LLM 注音时是否仍应用用户词典（非 LLM 模式恒为 True，无副作用）
        self._llm_apply_user_dict = llm_apply_user_dict
        # False=只刷注音、保留现有节奏点不动
        self._update_checkpoints = update_checkpoints

    def run(self) -> None:
        try:
            # 为 worker 线程初始化 WinRT COM STA apartment，
            # 确保在非主线程调用 WinRT 静态方法时 apartment 已就绪。
            try:
                from winrt._winrt import STA, init_apartment
                init_apartment(STA)
            except Exception:
                pass

            def _progress_cb(phase: str, current: int, total: int) -> None:
                self.progress.emit(phase, current, total)

            # LLM 注音：显式预热整首批量请求，期间发「等待 LLM」信号给 UI。
            _analyzer = getattr(self._auto_check, "_analyzer", None)
            if _analyzer is not None and hasattr(_analyzer, "prewarm"):
                if hasattr(_analyzer, "set_progress_callback"):
                    _analyzer.set_progress_callback(self.llm_progress.emit)
                self.llm_waiting.emit()
                _analyzer.prewarm()

            deleted_count = self._auto_check.analyze_and_apply_pipeline(
                self._project,
                only_noruby=self._only_noruby,
                apply_user_dict=self._llm_apply_user_dict,
                delete_types=self._delete_types or None,
                update_checkpoints=self._update_checkpoints,
                progress_callback=_progress_cb,
            )

            self.finished.emit(self._project, deleted_count)
        except Exception as e:
            self.error.emit(str(e))


# ──────────────────────────────────────────────
# 注音分析（局部）
# ──────────────────────────────────────────────


class RubySubsetAnalyzeWorker(QObject):
    """对指定行/字符范围列表执行注音分析，避免 UI 阻塞。

    specs: list of (line_idx: int, restrict_indices: set | None)
        restrict_indices=None 表示整行分析。
    """

    progress = pyqtSignal(str, int, int)  # (phase, current, total)
    llm_waiting = pyqtSignal()     # LLM 整首批量请求发出、等待返回中
    llm_progress = pyqtSignal(str) # LLM 内部阶段文本（请求/解析/重试/轮次）
    finished = pyqtSignal(object)  # analyzed project copy
    error = pyqtSignal(str)

    def __init__(
        self,
        project_copy: "Project",
        auto_check,
        specs: list,
        apply_user_dict: bool = True,
        update_checkpoints: bool = True,
    ):
        super().__init__()
        self._project = project_copy
        self._auto_check = auto_check
        self._specs = specs
        self._apply_user_dict = apply_user_dict
        # False=只刷注音、保留现有节奏点不动
        self._update_checkpoints = update_checkpoints

    def run(self) -> None:
        try:
            try:
                from winrt._winrt import STA, init_apartment
                init_apartment(STA)
            except Exception:
                pass

            # LLM 注音：显式预热整首批量请求，期间发「等待 LLM」信号给 UI。
            _analyzer = getattr(self._auto_check, "_analyzer", None)
            if _analyzer is not None and hasattr(_analyzer, "prewarm"):
                if hasattr(_analyzer, "set_progress_callback"):
                    _analyzer.set_progress_callback(self.llm_progress.emit)
                self.llm_waiting.emit()
                _analyzer.prewarm()

            total = len(self._specs)
            for idx, (line_idx, restrict_indices) in enumerate(self._specs):
                if total > 1:
                    self.progress.emit(self.tr("注音分析"), idx + 1, total)
                sentence = self._project.sentences[line_idx]
                self._auto_check.analyze_and_apply_sentence_pipeline(
                    sentence,
                    only_noruby=False,
                    restrict_indices=restrict_indices,
                    apply_user_dict=self._apply_user_dict,
                    update_checkpoints=self._update_checkpoints,
                )

            self.finished.emit(self._project)
        except Exception as e:
            self.error.emit(str(e))


# ──────────────────────────────────────────────
# 通用项目任务
# ──────────────────────────────────────────────


class ProjectTaskWorker(QObject):
    """在后台线程对项目副本执行任意任务（罗马字转换、按类型删除注音等）。

    任务签名 ``task(project, progress_callback)``：对传入的项目副本就地修改，
    通过 ``(phase, current, total)`` 回调上报文字进度；计算结果经闭包盒子带回
    主线程，finished 信号携带处理完的项目副本。与 RubyAnalyzeWorker 不同，
    本类不绑定注音管线，供轻量的整项目批处理复用。
    """

    progress = pyqtSignal(str, int, int)  # (phase, current, total)
    finished = pyqtSignal(object)         # processed project copy
    error = pyqtSignal(str)

    def __init__(self, project_copy: Project, task):
        super().__init__()
        self._project = project_copy
        self._task = task

    def run(self) -> None:
        try:
            try:
                from winrt._winrt import STA, init_apartment
                init_apartment(STA)
            except Exception:
                pass

            self._task(self._project, self.progress.emit)
            self.finished.emit(self._project)
        except Exception as e:
            self.error.emit(str(e))


class ExportTaskWorker(QObject):
    """在后台线程执行导出任务列表（导出移出 UI 线程 + 原子落盘）。

    jobs 为界面侧预计算好的任务列表，每项::

        {"file_path": 最终目标路径, "format_name": 格式名, "kwargs": {...}}

    逐任务执行：先写同目录临时文件，成功后 ``os.replace`` 原子替换目标
    （避免中途异常留下截断的正式文件）；失败清理临时文件并记录。
    ``request_cancel()`` 在任务间生效（单个任务内部不可中断）。

    临时文件名形如 ``<主名>.tmp<扩展名>``（如 song.tmp.lrc）：导出器的
    ``_ensure_extension`` 会把非目标扩展名的末段替换为格式扩展名，
    临时文件必须以正式扩展名收尾才不会被导出器改写路径。
    """

    progress = pyqtSignal(str, int, int)  # (阶段文字, 当前完成数, 总数)
    finished = pyqtSignal(object)         # {"exported": [...], "failed": [(path, msg)], "cancelled": bool}
    error = pyqtSignal(str)

    def __init__(self, project: Project, export_service, jobs: list, *, snapshot: bool = True):
        super().__init__()
        # 持原始引用；snapshot=True 时副本在本线程 run() 内制作，大项目
        # deepcopy 的数百毫秒不再卡 UI 线程
        self._project = project
        self._snapshot = snapshot
        self._export_service = export_service
        self._jobs = jobs
        self._cancelled = False

    def request_cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        from copy import deepcopy

        exported: list = []
        failed: list = []
        cancelled = False
        try:
            project = self._project
            if self._snapshot:
                # 导出期间 UI 可继续编辑：对副本导出。并发编辑可能令
                # deepcopy 抛 RuntimeError（容器尺寸变化），重试一次
                for attempt in range(2):
                    try:
                        project = deepcopy(self._project)
                        break
                    except RuntimeError:
                        if attempt == 1:
                            raise
            total = len(self._jobs)
            for done, job in enumerate(self._jobs):
                if self._cancelled:
                    cancelled = True
                    break
                target = job["file_path"]
                target_path = Path(target)
                ext = job.get("ext") or target_path.suffix
                tmp_path = str(target_path.with_name(f"{target_path.stem}.tmp{ext}"))
                try:
                    result = self._export_service.export(
                        project, job["format_name"], tmp_path, **job["kwargs"]
                    )
                    if result.success:
                        # fsync 临时文件后再 replace（断电不致半截正式文件）；
                        # r+b 打开保证 Windows 上 FlushFileBuffers 的写权限
                        with open(tmp_path, "r+b") as f:
                            f.flush()
                            os.fsync(f.fileno())
                        os.replace(tmp_path, target)
                        exported.append(target)
                    else:
                        _silent_unlink(tmp_path)
                        failed.append((target, result.error_message or ""))
                except Exception as e:  # noqa: BLE001  # 单任务失败不拖垮整批
                    _silent_unlink(tmp_path)
                    failed.append((target, f"{type(e).__name__}: {e}"))
                self.progress.emit(self.tr("导出中"), done + 1, total)
            self.finished.emit(
                {"exported": exported, "failed": failed, "cancelled": cancelled}
            )
        except Exception as e:
            self.error.emit(str(e))


def _silent_unlink(path: str) -> None:
    """best-effort 删除文件（失败静默，如 .tmp 已不存在）。"""
    try:
        os.unlink(path)
    except OSError:
        pass


# ──────────────────────────────────────────────
# LLM 注音连通性测试
# ──────────────────────────────────────────────


class LLMTestWorker(QObject):
    """后台测试 LLM 注音连通性，避免阻塞设置页 UI。"""

    finished = pyqtSignal(bool, str)  # (ok, message)

    def __init__(self, config, proxies=None):
        super().__init__()
        self._config = config
        self._proxies = proxies

    def run(self) -> None:
        try:
            from strange_uta_game.backend.infrastructure.parsers.llm_ruby import (
                LLMRubyClient,
            )

            client = LLMRubyClient(self._config, proxies=self._proxies)
            ok, msg = client.test_connection()
            self.finished.emit(ok, msg)
        except Exception as e:  # noqa: BLE001
            self.finished.emit(False, f"{type(e).__name__}: {e}")


# ──────────────────────────────────────────────
# 歌词
# ──────────────────────────────────────────────


class LyricReadWorker(QObject):
    """后台读取歌词文件原始内容。"""

    finished = pyqtSignal(str)  # raw text content
    error = pyqtSignal(str)

    def __init__(self, file_path: str):
        super().__init__()
        self._file_path = file_path

    def run(self) -> None:
        try:
            path = Path(self._file_path)
            # 统一编码回退链（BOM → utf-8-sig → cp932 → gb18030 → big5），
            # 与歌词解析器/项目导入一致；GBK/Big5 歌词不再乱码。
            from strange_uta_game.backend.infrastructure.parsers.encoding import (
                decode_lyric_bytes,
            )

            content, _encoding = decode_lyric_bytes(path.read_bytes())
            self.finished.emit(content)
        except Exception as e:
            self.error.emit(str(e))


class LyricParseWorker(QObject):
    """后台读取并解析歌词文件，避免大文件解析时阻塞 UI 主线程。

    调用方在主线程预先读取 settings 值并传入，worker 内部不接触任何 Qt/UI 对象。
    Nicokara 元数据同步（_sync_nicokara_metadata_to_settings）延迟到主线程回调中执行。
    """

    progress = pyqtSignal(str)    # 阶段描述文字，用于更新 StateToolTip
    finished = pyqtSignal(object) # dict: {sentences, is_nicokara, new_singers, parse_meta}
    error = pyqtSignal(str)

    def __init__(
        self,
        file_path: str,
        default_singer_id: str,
        project_singers: list,
        software_compensation_ms: int,
        auto_check_flags: dict,
        user_dict: list,
        annotate_katakana_with_english: bool,
        *,
        content: str | None = None,
    ):
        super().__init__()
        self._file_path = file_path
        self._content = content  # 非 None 时直接用此内容，跳过文件读取
        self._default_singer_id = default_singer_id
        self._project_singers = project_singers
        self._software_compensation_ms = software_compensation_ms
        self._auto_check_flags = auto_check_flags
        self._user_dict = user_dict
        self._annotate_katakana_with_english = annotate_katakana_with_english

    def run(self) -> None:
        try:
            # Utaten 对齐阶段调用 AutoCheckService 可能依赖 WinRT，需初始化 STA apartment。
            try:
                from winrt._winrt import STA, init_apartment
                init_apartment(STA)
            except Exception:
                pass

            if self._content is not None:
                content = self._content
            else:
                self.progress.emit(self.tr("正在读取文件..."))
                path = Path(self._file_path)
                # 统一编码回退链（BOM → utf-8-sig → cp932 → gb18030 → big5），
                # 与歌词解析器/项目导入一致；GBK/Big5 歌词不再乱码。
                from strange_uta_game.backend.infrastructure.parsers.encoding import (
                    decode_lyric_bytes,
                )

                content, _encoding = decode_lyric_bytes(path.read_bytes())

            from strange_uta_game.frontend.editor.timing.lyric_loader import (
                parse_lyric_content,
            )

            sentences, is_nicokara, new_singers, parse_meta = parse_lyric_content(
                content,
                self._default_singer_id,
                self._project_singers,
                software_compensation_ms=self._software_compensation_ms,
                auto_check_flags=self._auto_check_flags,
                user_dict=self._user_dict,
                annotate_katakana_with_english=self._annotate_katakana_with_english,
                skip_settings_sync=True,
                progress_cb=self.progress.emit,
            )
            self.finished.emit({
                "sentences": sentences,
                "is_nicokara": is_nicokara,
                "new_singers": new_singers,
                "parse_meta": parse_meta,
            })
        except Exception as e:
            self.error.emit(str(e))
