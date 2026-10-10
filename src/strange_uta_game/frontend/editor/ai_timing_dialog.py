"""AI 打轴完整弹窗（阶段 F，2026-08 审查版）。

按计划文档 §3.2/§3.3 一次展示全部前置状态与执行区，并包含：

- 执行前状态（FluentGroupBox）：原始音频（含时长）、歌词标注、人声素材
  （多候选时下拉选择）、分离环境、对齐环境、对齐模型（许可证与非商业
  提示、模型页链接）；
- 高级选项（FluentGroupBox）：对齐模型（微调 / MMS_FA，与状态行联动）、
  设备、尾音修正、下载镜像；
- 存储位置（FluentGroupBox）：模型目录 / AI 缓存 / 运行环境三行，
  每行「名称 + 省略路径 + 浏览 + 更改…」统一样式；
- 动作：下载模型、安装/修复环境（含分离组件补装）、深度校验、
  恢复推荐设置；
- 执行区（固定底部，不随滚动）：进度 + ETA、二次确认取消；
- 底部标注：对齐思路参考 FA-Kara / yohane。

主体内容放在 ScrollArea 内（批量变更弹窗同款），任何分辨率下不再
出现窗口高度溢出。对话框与工具栏其他弹窗同构：原生 ``QDialog`` +
内部 Fluent 控件 + ``tr()`` 多语言文案。
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

from PyQt6.QtCore import QObject, QThread, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon as FIF,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
)
from qfluentwidgets import InfoBar, InfoBarPosition

from strange_uta_game.backend.application.ai_timing.ailog import ai_log_path, ailog
from strange_uta_game.backend.application.ai_timing.models import (
    ModelDownloadService,
    ModelRegistry,
)
from strange_uta_game.backend.application.ai_timing.runtime import (
    INSTALL_LOG_NAME,
    AiRuntimeError,
    AiRuntimeManager,
)
from strange_uta_game.backend.application.ai_timing.service import (
    AiTimingService,
    AiTimingSnapshot,
)
from strange_uta_game.backend.application.ai_timing.settings import (
    AiTimingSettings,
    resolve_model_root,
)
from strange_uta_game.backend.domain import Project
from strange_uta_game.frontend.editor.timing.dialogs import (
    FONT_DIALOG_BASE,
    char_dialog_font,
)
from strange_uta_game.frontend.fluent_widgets import (
    FluentGroupBox,
    message_question,
)
from strange_uta_game.frontend.window_sizing import fit_to_screen

DEFAULT_WAV2VEC2_MODEL_ID = "NextFire/mms-300m-ForcedAligner-karaoke-ja-Latn"
MODEL_PAGE_URL = (
    "https://huggingface.co/NextFire/mms-300m-ForcedAligner-karaoke-ja-Latn"
)
MODEL_LICENSE_TEXT = (
    f'<a href="{MODEL_PAGE_URL}">默认模型 {DEFAULT_WAV2VEC2_MODEL_ID}</a>'
    "（CC-BY-NC-SA-4.0，仅限非商业使用，仅随默认微调模型）"
)
CREDIT_TEXT = "对齐思路参考开源项目 FA-Kara 与 yohane（本项目不内嵌其代码）"

# 磁盘占用估算（2026-08 实测口径：CUDA venv 4.88GB / 模型 1.18GB /
# 共享增量合计约 0.4GB；CPU venv 按去掉 CUDA torch 加 CPU torch 推算）
DISK_EST_VENV_CUDA_GB = 5.0
DISK_EST_VENV_CPU_GB = 2.0
# 打包版（frozen）走发行包路线：完成后 CUDA 约 6GB / CPU 约 2GB；
# 过程峰值（底座 + torch wheel 缓存）由安装内预检把关
DISK_EST_RELEASE_CUDA_GB = 6.0
DISK_EST_RELEASE_CPU_GB = 2.0
DISK_EST_SHARED_GB = 0.5
DISK_EST_MODEL_GB = 1.2


def _install_log_file_for(mode: str, target) -> Optional[Path]:
    """安装日志文件路径（与 runtime.open_install_log 的目录口径一致）。

    shared 模式的 target 是解释器路径：日志在其上级目录；venv 模式
    的 target 是运行环境目录本身。
    """
    try:
        if mode == "shared":
            return Path(target).resolve().parents[1] / INSTALL_LOG_NAME
        return Path(target) / INSTALL_LOG_NAME
    except Exception:
        return None

# 上游进度消息中的速度/剩余时间片段（下载 MB/s、分离 s/块、pip 包/分）；
# ETA 优先采信上游自带值，百分比推算只作为无值阶段的兜底
_SPEED_PATTERN = re.compile(
    r"[0-9]+(?:\.[0-9]+)?\s*(?:MB/s|KB/s|GB/s|包/分|s/块|it/s)"
)
_ETA_PATTERN = re.compile(r"预计剩余\s*([0-9]+):([0-9]{2})")


class _TaskWorker(QObject):
    """在 QThread 中执行任意 ``(progress_cb, cancel_check) -> object`` 任务。"""

    progress = pyqtSignal(str, int, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn: Callable):
        super().__init__()
        self._fn = fn
        self._cancelled = False

    def request_cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        def _progress(stage: str, percent: int, message: str) -> None:
            self.progress.emit(stage, percent, message)

        try:
            result = self._fn(_progress, lambda: self._cancelled)
            self.finished.emit(result)
        except Exception as exc:  # noqa: BLE001 — 所有后台错误统一回主线程
            self.failed.emit(str(exc))


class _StateRow(QWidget):
    """状态卡中的一行：名称 + 彩点 + 状态文本。

    不继承 FluentLabelBase —— 其 ``__init__`` 是 singledispatchmethod，
    子类覆写构造器会与其内部 ``self.__init__(parent)`` 重派发形成无限
    递归；这里用组合方式持有一个名称标签与一个富文本状态标签。
    """

    _DOT_COLORS = {
        "ok": "#3ec46d",
        "warn": "#f5a623",
        "error": "#e85555",
        "busy": "#4a7de0",
    }

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self._name = name
        self._state = "busy"
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._name_label = BodyLabel(name, self)
        self._name_label.setFixedWidth(80)
        self._state_label = BodyLabel("", self)
        layout.addWidget(self._name_label)
        layout.addWidget(self._state_label, 1)
        self.set_state("busy", self.tr("检查中…"))

    def set_state(self, state: str, text: str) -> None:
        color = self._DOT_COLORS.get(state, "#999999")
        self._state = state
        # 状态文本统一中间省略（单行显示，超长路径会把滚动区最小宽度撑爆、
        # 导致窗口显示不全）；完整内容放 tooltip 供悬停查看
        shown = text if len(text) <= 48 else text[:22] + "…" + text[-22:]
        self._state_label.setText(f"<b style='color:{color}'>●</b> {shown}")
        self._state_label.setToolTip(text if text != shown else "")

    def add_action(self, text: str, clicked: Callable) -> PushButton:
        """在行尾放一个小动作按钮（缺什么就在该行旁边给什么操作）。"""
        btn = PushButton(text, self)
        btn.setFixedHeight(26)
        btn.clicked.connect(clicked)
        self.layout().addWidget(btn)
        return btn


class AiTimingDialog(QDialog):
    """AI 打轴完整弹窗。

    与工具栏其他弹窗（批量变更等）同构：原生 QDialog（系统关闭按钮、
    跟随应用主题、不置顶）+ 内部 Fluent 控件；全部用户可见文案
    走 ``self.tr()`` 以支持多语言。
    """

    def __init__(
        self,
        *,
        project: Project,
        audio_path: str,
        audio_duration_ms: int = 0,
        service: AiTimingService,
        settings: AiTimingSettings,
        registry: ModelRegistry,
        runtime: AiRuntimeManager,
        download_service: ModelDownloadService,
        on_applied: Callable,
        save_settings: Optional[Callable[[AiTimingSettings], None]] = None,
        download_proxy: str = "",
        managed_runtime_python: str = "",
        embedded_mode: bool = False,
        open_separation_page: Optional[Callable] = None,
        stop_separation_service: Optional[Callable] = None,
        context_checker: Optional[Callable[[], bool]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._project = project
        self._audio_path = audio_path
        self._audio_duration_ms = int(audio_duration_ms or 0)
        self._service = service
        self._settings = settings
        self._registry = registry
        self._runtime = runtime
        self._download_service = download_service
        self._on_applied = on_applied
        self._save_settings = save_settings
        self._download_proxy = download_proxy
        # 方案 B：宿主托管 Runtime 的 python.exe（embedded 注入）。
        # 存在且有效时「安装/修复」走 install_shared 增量安装；
        # embedded 模式一律不允许裸装 venv（会与宿主 runtime 重复一份
        # torch，实际测试中已造成过一次多装）
        self._managed_runtime_python = managed_runtime_python
        self._embedded_mode = embedded_mode
        # 宿主页面跳转（嵌入模式引导去第 2 步装环境）；None = 未提供
        self._open_separation_page = open_separation_page
        # 宿主分离服务停止（方案 B 装前腾出解释器文件锁）；None = 未提供
        self._stop_separation_service = stop_separation_service
        # 返回 True 表示打开时的工程/音频仍然有效（未切换/未关闭）
        self._context_checker = context_checker

        self._snapshot: Optional[AiTimingSnapshot] = None
        self._thread: Optional[QThread] = None
        self._worker: Optional[_TaskWorker] = None
        self._busy = False
        # 模型下拉变更的延迟应用标记（防抖：连续快速切换只应用最后一次）
        self._model_apply_pending = False
        self._eta_samples: List[tuple] = []
        self._task_started = 0.0
        self._last_eta_text = ""
        self._last_msg_time = 0.0
        # ETA 呈现方式：transport = 上游消息自带（下载 MB/s、分离 s/块），
        # derived = 由百分比样本推算；_speed_text 为消息中的速度片段
        self._eta_kind = ""
        self._eta_rate = 0.0
        self._eta_last = (0.0, 0)
        self._speed_text = ""
        # 当前任务「长时间无输出」的如实说明（按任务类型区分：
        # pip 下载大包、推理单步、环境探测导入 torch 原因各不相同）
        self._stall_text = ""
        # 秒级刷新：不依赖上游消息频率（模型加载等阶段可能长时间无输出），
        # 每秒刷新一次已耗时与最近估算
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._on_second_tick)

        self._build_ui()
        self.refresh()

    # ── UI ──

    def _build_ui(self) -> None:
        self.setWindowTitle(self.tr("AI 打轴（Beta）"))
        # 默认加宽：720 时存储/状态行的「名称+省略文本+多个按钮」会把
        # 滚动区最小宽度撑过视口，开窗即出现横向滚动条（实测反馈）
        fit_to_screen(self, 880, 660)
        self.setFont(char_dialog_font(FONT_DIALOG_BASE))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(6)

        # ── 主体（可滚动）：状态 / 高级选项 / 存储位置 ──
        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.enableTransparentBackground()
        content = QWidget(self)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(6)

        title = SubtitleLabel(self.tr("AI 打轴（Beta）"), content)
        layout.addWidget(title)

        hint = BodyLabel(
            self.tr(
                "自动对齐会把歌词标注对齐到人声；成功后覆盖全部时间戳，"
                "可在工具栏撤销一次恢复。结果仅供辅助参考，请务必人工"
                "校准后再使用。"
            ),
            content,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        # 重要警示（非普通提示）：和声/重叠人声需人工复查
        self.warn_overlap = BodyLabel(
            self.tr(
                "⚠ 请注意：对齐针对主唱人声。含和声、重叠人声或伴唱的段落"
                "可能出现错位，完成后务必人工复查这些段落。"
            ),
            content,
        )
        self.warn_overlap.setWordWrap(True)
        self.warn_overlap.setStyleSheet("color:#f5a623;font-weight:bold;")
        layout.addWidget(self.warn_overlap)
        # 默认模型说明（许可证 + 非商业）——属说明信息，放说明区
        model_credit = CaptionLabel(MODEL_LICENSE_TEXT, content)
        model_credit.setOpenExternalLinks(True)
        model_credit.setWordWrap(True)
        layout.addWidget(model_credit)
        # macOS：设备「自动」会优先 MPS（Apple 芯片加速，worker 内
        # CUDA→MPS→CPU 逐级回落）；运行环境安装走 venv（源码运行）

        # ── 执行前状态 ──
        self._box_status = FluentGroupBox(self.tr("执行前状态"), content)
        layout.addWidget(self._box_status)
        self.row_audio = _StateRow(self.tr("原始音频"), content)
        self.row_annotations = _StateRow(self.tr("歌词标注"), content)
        self.row_vocal = _StateRow(self.tr("人声素材"), content)
        self.row_separation = _StateRow(self.tr("分离环境"), content)
        self.row_runtime = _StateRow(self.tr("对齐环境"), content)
        self.row_model = _StateRow(self.tr("对齐模型"), content)
        for row in (
            self.row_audio,
            self.row_annotations,
            self.row_vocal,
            self.row_separation,
            self.row_runtime,
            self.row_model,
        ):
            self._box_status.contentLayout.addWidget(row)
        # 行内动作：对齐环境的「安装/修复」（含分离组件补装）、「日志」
        # （一键定位 ai_timing.log，用户可整个发回排查）、模型的
        # 「下载」「校验」（检测到缺什么，同行直接给操作）
        self.btn_install_runtime = self.row_runtime.add_action(
            self.tr("安装 / 修复"), self._on_install_runtime
        )
        self.btn_open_ai_log = self.row_runtime.add_action(
            self.tr("日志"), self._on_open_ai_log
        )
        # 嵌入模式宿主 Runtime 未安装时的一键跳转（宿主提供能力才显示）
        self.btn_goto_sep = self.row_runtime.add_action(
            self.tr("去音频分离"), self._on_goto_separation
        )
        self.btn_goto_sep.hide()
        self.btn_download_model = self.row_model.add_action(
            self.tr("下载模型"), self._on_download_model
        )
        self.btn_recheck = self.row_model.add_action(
            self.tr("校验"), self._on_deep_recheck
        )

        # 多人声候选选择（§6.1：多个严格候选时在弹窗内选择）
        self.vocal_combo = ComboBox(content)
        self.vocal_combo.hide()
        self._box_status.contentLayout.addWidget(self.vocal_combo)

        self.blocking_label = BodyLabel("", content)
        self.blocking_label.setWordWrap(True)
        self.blocking_label.setStyleSheet("color:#e85555;")
        self.blocking_label.hide()
        self._box_status.contentLayout.addWidget(self.blocking_label)

        # ── 高级选项（§3.2）──
        self._box_adv = FluentGroupBox(self.tr("高级选项"), content)
        layout.addWidget(self._box_adv)
        advanced = QHBoxLayout()
        advanced.addWidget(BodyLabel(self.tr("对齐模型:"), content))
        self.combo_model = ComboBox(content)
        self.combo_model.addItems(
            [
                self.tr("微调模型（推荐 · 日文歌曲效果最佳）"),
                self.tr("MMS_FA 基础模型（多语言 · 通用备选）"),
            ]
        )
        self.combo_model.currentIndexChanged.connect(self._on_model_combo_changed)
        advanced.addWidget(self.combo_model)
        self.model_desc = CaptionLabel("", content)
        self.model_desc.setWordWrap(True)
        self._update_model_desc(self.combo_model.currentIndex())
        advanced.addWidget(BodyLabel(self.tr("设备:"), content))
        self.combo_device = ComboBox(content)
        self.combo_device.addItems(
            [self.tr("自动"), "CPU", "CUDA", self.tr("MPS（Apple 芯片）")]
        )
        advanced.addWidget(self.combo_device)
        self._box_adv.contentLayout.addLayout(advanced)
        self._box_adv.contentLayout.addWidget(self.model_desc)
        mirror_row = QHBoxLayout()
        mirror_row.addWidget(BodyLabel(self.tr("下载镜像:"), content))
        self.combo_mirror = ComboBox(content)
        # 预设项 userData 存镜像端点 URL；「自定义 URL…」用 __custom__ 占位，
        # 选中时下方展开 edit_mirror 输入框（下载镜像此前形同虚设：自由
        # 文本框只写回设置，从不更新已建的 HfHubTransport 端点）
        self.combo_mirror.addItem(
            self.tr("官方（huggingface.co）"), userData=""
        )
        self.combo_mirror.addItem(
            self.tr("hf-mirror.com"), userData="https://hf-mirror.com"
        )
        self.combo_mirror.addItem(self.tr("自定义 URL…"), userData="__custom__")
        mirror_row.addWidget(self.combo_mirror, 1)
        self._box_adv.contentLayout.addLayout(mirror_row)
        self.edit_mirror = LineEdit(content)
        self.edit_mirror.setPlaceholderText(
            self.tr("输入镜像端点，如 https://hf-mirror.com")
        )
        self._box_adv.contentLayout.addWidget(self.edit_mirror)
        # 先按当前设置同步选中，再连信号——避免初始 setCurrentIndex 触发
        # 持久化（构造期写设置既多余又可能落到旧值上）
        self._sync_mirror_ui()
        self.combo_mirror.currentIndexChanged.connect(self._on_mirror_combo_changed)
        self.edit_mirror.editingFinished.connect(self._on_mirror_custom_edited)

        # ── 存储位置（§3.2）：统一行样式——名称 + 省略路径 + 浏览 + 更改… ──
        self._box_store = FluentGroupBox(self.tr("存储位置"), content)
        layout.addWidget(self._box_store)

        def _path_row(name, path_getter, buttons):
            row = QWidget(content)
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(8)
            h.addWidget(BodyLabel(name, row))
            value = CaptionLabel(self._elide_text(path_getter()), row)
            h.addWidget(value, 1)
            for text, cb in buttons:
                b = PushButton(text, row)
                b.setFixedHeight(26)
                b.clicked.connect(cb)
                h.addWidget(b)
            row._refresh = lambda: value.setText(self._elide_text(path_getter()))
            self._box_store.contentLayout.addWidget(row)
            return row

        self._row_model_dir = _path_row(
            self.tr("模型目录"),
            lambda: str(resolve_model_root(self._settings)),
            [
                (
                    self.tr("浏览"),
                    lambda: self._open_dir(resolve_model_root(self._settings)),
                ),
                (self.tr("更改…"), self._on_change_model_dir),
            ],
        )
        self._row_cache_dir = _path_row(
            self.tr("AI 缓存"),
            lambda: str(self._snapshot.cache_root)
            if self._snapshot and self._snapshot.cache_root
            else self.tr("默认（.cache）"),
            [
                (
                    self.tr("浏览"),
                    lambda: self._open_dir(self._current_cache_root()),
                ),
                (self.tr("更改…"), self._on_change_cache_dir),
            ],
        )
        self._row_runtime = _path_row(
            self.tr("运行环境"),
            # 打包版没有「当前解释器」：未安装时应明确提示而不是误导
            lambda: self._settings.runtime_python
            or (
                self.tr("未安装（点击下方「安装 / 修复」）")
                if getattr(sys, "frozen", False)
                else self.tr("当前解释器")
            ),
            [
                (
                    self.tr("浏览"),
                    lambda: self._open_dir(
                        Path(self._settings.runtime_python).parent
                        if self._settings.runtime_python
                        else resolve_model_root(self._settings).parent
                        / "ai_runtime"
                    ),
                ),
                (self.tr("更改…"), self._on_change_runtime),
            ],
        )
        tail = QHBoxLayout()
        tail.addStretch(1)
        self.btn_reset = PushButton(self.tr("恢复推荐设置"), content)
        self.btn_reset.setFixedHeight(26)
        self.btn_reset.clicked.connect(self._on_reset_settings)
        tail.addWidget(self.btn_reset)
        self._box_store.contentLayout.addLayout(tail)

        layout.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        # ── 执行区（固定底部）──
        self.progress = ProgressBar(self)
        self.progress.setValue(0)
        outer.addWidget(self.progress)
        self.status_label = StrongBodyLabel(self.tr("就绪"), self)
        outer.addWidget(self.status_label)
        self.eta_label = BodyLabel("", self)
        outer.addWidget(self.eta_label)

        run_row = QHBoxLayout()
        run_row.addStretch(1)
        self.btn_cancel = PushButton(self.tr("取消"), self)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._on_cancel_clicked)
        self.btn_run = PrimaryPushButton(self.tr("自动对齐"), self)
        self.btn_run.setIcon(FIF.ROBOT)
        self.btn_run.clicked.connect(self._on_run_clicked)
        run_row.addWidget(self.btn_cancel)
        run_row.addWidget(self.btn_run)
        outer.addLayout(run_row)

        # 参考标注（用户要求：UI 内标注参考 FA-Kara）
        credit = BodyLabel(CREDIT_TEXT, self)
        credit.setStyleSheet("color:#888888;")
        outer.addWidget(credit)

    # ── 后台任务基础设施 ──

    def _run_task(
        self,
        fn: Callable,
        on_done: Callable,
        busy_text: str,
        stall_text: str = "",
    ) -> None:
        if self._busy:
            return
        self._busy = True
        self._eta_samples = []
        self._eta_kind = ""
        self._eta_rate = 0.0
        self._eta_last = (0.0, 0)
        self._speed_text = ""
        self._stall_text = stall_text
        self.btn_run.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        for b in self._action_buttons():
            b.setEnabled(False)
        for w in self._advanced_widgets():
            w.setEnabled(False)
        self.status_label.setText(busy_text)
        self.eta_label.setText("")
        self._last_eta_text = ""
        import time as _time

        self._task_started = _time.monotonic()
        self._last_msg_time = self._task_started
        self._tick_timer.start()

        self._thread = QThread(self)
        self._worker = _TaskWorker(fn)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_task_progress)
        # cleanup 先连：_done/_on_task_failed 执行时 busy 已复位，
        # 其内部的 refresh() 才不会被挡掉（安装完成状态不刷新的根因）
        for sig in (self._worker.finished, self._worker.failed):
            sig.connect(self._cleanup_task)
        self._worker.finished.connect(on_done)
        self._worker.failed.connect(self._on_task_failed)
        # 线程结束后延迟销毁 worker 与线程对象（同 fulltext_interface）：
        # 长会话反复跑任务时不再累积 QThread/QObject
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _action_buttons(self) -> list:
        return [
            self.btn_download_model,
            self.btn_install_runtime,
            self.btn_goto_sep,
            self.btn_recheck,
            self.btn_reset,
            # 日志按钮保持可用：任务进行中打开日志看实时进展也是正当需求
        ]

    def _advanced_widgets(self) -> list:
        """高级选项控件：任务进行中一并禁用（改 provider/device/镜像/
        运行环境会让在途任务与设置处于不一致状态）。"""
        return [
            self.combo_model,
            self.combo_device,
            self.combo_mirror,
            self.edit_mirror,
        ]

    def _cleanup_task(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
        self._thread = None
        self._worker = None
        self._busy = False
        self._tick_timer.stop()
        self.btn_cancel.setEnabled(False)
        for b in self._action_buttons():
            b.setEnabled(True)
        for w in self._advanced_widgets():
            w.setEnabled(True)

    def _on_task_progress(self, stage: str, percent: int, message: str) -> None:
        self.progress.setValue(max(0, min(100, percent)))
        if message:
            # pip 解析行可能极长，截断避免窗口被拉长
            shown = message if len(message) <= 80 else message[:79] + "…"
            self.status_label.setText(shown)
        speed = _SPEED_PATTERN.search(message)
        if speed:
            # 保留最近一次速度：pip 在「获取依赖」与「Collecting」行之间
            # 交替输出，无速度的行不该把速度显示清掉
            self._speed_text = speed.group(0)
        eta = _ETA_PATTERN.search(message)
        if eta is not None:
            # 上游自带剩余时间（模型下载 / 分离 tqdm）：直接采信，随消息刷新
            self._eta_kind = "transport"
            parts = [f"预计剩余 {eta.group(1)}:{eta.group(2)}"]
            if self._speed_text:
                parts.append(self._speed_text)
            self._last_eta_text = "　·　".join(parts)
        else:
            self._eta_kind = "derived"
            self._last_eta_text = self._compute_eta(percent)
        import time as _time

        self._last_msg_time = _time.monotonic()
        self.eta_label.setText(self._format_eta_line())

    def _format_eta_line(self) -> str:
        elapsed = int(time.monotonic() - self._task_started)
        m, s = divmod(max(0, elapsed), 60)
        prefix = self.tr("已耗时 {m}:{s:02d}").format(m=m, s=s)
        tail = self._eta_tail_text()
        return prefix + ("　·　" + tail if tail else "")

    def _eta_tail_text(self) -> str:
        """ETA 尾部：transport 原样透传；derived 按最近速率继续倒计时。

        百分比消息之间不再冻结显示——用最近一次采样点外推，剩余时间
        随秒级 tick 持续递减；速率不可用时如实显示「正在估算」。
        """
        if self._eta_kind == "transport":
            return self._last_eta_text
        if self._eta_kind == "derived":
            if self._eta_rate > 0:
                now = time.monotonic()
                last_t, last_p = self._eta_last
                remaining = max(
                    0.0, (100.0 - last_p) / self._eta_rate - (now - last_t)
                )
                if remaining <= 1.0:
                    eta = self.tr("即将完成…")
                else:
                    m, s = divmod(int(remaining), 60)
                    eta = self.tr("预计剩余 {m}:{s:02d}").format(m=m, s=s)
            else:
                eta = self.tr("正在估算剩余时间…")
            if self._speed_text:
                eta = eta + "　·　" + self._speed_text
            return eta
        return ""

    def _on_second_tick(self) -> None:
        """每秒刷新：已耗时 + 倒计时 ETA；长时间无消息时按任务类型如实说明。"""
        import time as _time

        now = _time.monotonic()
        if self._last_msg_time and now - self._last_msg_time > 15:
            # 陈旧估算不如如实说明：不同任务无输出的原因不同
            # （推理单步 / pip 下载大包 / 环境探测导入 torch）
            tail = self._stall_text or self.tr("处理中…")
        else:
            tail = self._eta_tail_text()
        elapsed = int(now - self._task_started)
        m, s = divmod(max(0, elapsed), 60)
        prefix = self.tr("已耗时 {m}:{s:02d}").format(m=m, s=s)
        self.eta_label.setText(prefix + ("　·　" + tail if tail else ""))

    def _compute_eta(self, percent: int) -> str:
        """平滑 ETA：样本不足时显示「正在估算」（§8.2）。

        同时记录最近速率与采样点，供每秒 tick 在两条进度消息之间
        继续倒计时（不再冻结在上一次消息的数值）。
        """
        if percent <= 0:
            self._eta_rate = 0.0
            return self.tr("正在估算剩余时间…")
        now = time.monotonic()
        if self._eta_samples and percent < self._eta_samples[-1][1] - 5:
            # 阶段边界百分比回落的兜底（service 层进度现已全程单调，
            # 但嵌入宿主等外部进度源不受此约束）：旧样本会让速率
            # 算出负值，重新累计
            self._eta_samples = []
        self._eta_samples.append((now, percent))
        self._eta_samples = self._eta_samples[-20:]
        first_t, first_p = self._eta_samples[0]
        elapsed = now - first_t
        gained = percent - first_p
        if gained < 5 or elapsed < 2:
            # 百分比停滞（下载单个大包等）：清空速率，如实显示估算中
            self._eta_rate = 0.0
            return self.tr("正在估算剩余时间…")
        rate = gained / elapsed  # 百分点 / 秒
        self._eta_rate = rate
        self._eta_last = (now, percent)
        remaining = max(0, (100 - percent) / rate)
        minutes, seconds = divmod(int(remaining), 60)
        return self.tr("预计剩余 {m}:{s:02d}").format(m=minutes, s=seconds)

    def _on_task_failed(self, message: str) -> None:
        self.progress.setValue(0)
        if "取消" in message:
            InfoBar.warning(
                title=self.tr("已取消"),
                content=self.tr("AI 打轴已取消，未应用任何结果。"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            self.status_label.setText(self.tr("已取消"))
        else:
            InfoBar.error(
                title=self.tr("AI 打轴失败"),
                content=message,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=8000,
                parent=self,
            )
            self.status_label.setText(self.tr("失败"))
        self.refresh()

    # ── 设置读写 ──

    def _persist_settings(self) -> None:
        self._settings.provider = (
            "wav2vec2" if self.combo_model.currentIndex() == 0 else "mms_fa"
        )
        self._settings.device = ("auto", "cpu", "cuda", "mps")[
            self.combo_device.currentIndex()
        ]
        self._settings.download_mirror = self._mirror_value()
        # 下载端点必须同步到已建的 transport，否则镜像形同虚设：transport
        # 在弹窗构造时按当时设置固化 endpoint，改设置不改 transport 下载仍
        # 走旧源（曾长期是纯摆设——填了只存配置不生效）
        try:
            self._download_service.set_endpoint(self._settings.download_mirror)
        except Exception:
            pass
        if self._save_settings is not None:
            try:
                self._save_settings(self._settings)
            except Exception:
                pass

    # ── 下载镜像（预设 / 自定义）──

    def _sync_mirror_ui(self) -> None:
        """按当前 settings.download_mirror 同步 combo 选择与自定义输入框。"""
        value = (self._settings.download_mirror or "").strip()
        if not value:
            self.combo_mirror.setCurrentIndex(0)
            self.edit_mirror.clear()
        elif value == "https://hf-mirror.com":
            self.combo_mirror.setCurrentIndex(1)
            self.edit_mirror.clear()
        else:
            # 其它端点（自定义）：选中自定义项并回填输入框
            self.combo_mirror.setCurrentIndex(2)
            self.edit_mirror.setText(value)
        self._update_mirror_edit_visibility()

    def _update_mirror_edit_visibility(self) -> None:
        self.edit_mirror.setVisible(
            self.combo_mirror.currentData() == "__custom__"
        )

    def _mirror_value(self) -> str:
        """读当前镜像选择 → 端点 URL（空串 = 官方源）。"""
        data = self.combo_mirror.currentData()
        if data == "__custom__":
            return self.edit_mirror.text().strip()
        return str(data or "")

    def _on_mirror_combo_changed(self, *_args) -> None:
        # 弹层（RoundMenu）关闭栈内同步触发；切换仅改可见性 + 持久化，
        # 不启动线程，直接同步处理安全（与模型下拉的延迟无关）
        self._update_mirror_edit_visibility()
        self._persist_settings()

    def _on_mirror_custom_edited(self) -> None:
        # 自定义 URL 输入结束（回车 / 失焦）→ 持久化并同步端点
        self._persist_settings()

    MODEL_DESCRIPTIONS = {
        0: (
            "NextFire 日语卡拉 OK 微调模型（约 3 亿参数，MMS 架构）：针对"
            "日文歌词的罗马字对齐专门微调，日文歌曲首选；首次使用需下载"
            "约 1.2GB 权重。中英韩文歌曲也可使用，走拼音/读音/谚文转写。"
        ),
        1: (
            "Meta MMS 多语言基础对齐模型（约 4.7 亿参数，torchaudio 内置"
            "分发）：语言覆盖广、无需手动下载大文件，但对日文卡拉 OK 场景"
            "的精度通常不如微调模型；作为兼容备选。"
        ),
    }

    def _update_model_desc(self, index: int) -> None:
        self.model_desc.setText(
            self.tr(self.MODEL_DESCRIPTIONS.get(index, ""))
        )

    def _on_model_combo_changed(self, *_args) -> None:
        """高级选项切换模型 → 持久化并刷新状态行（保持两处一致）。

        重活延迟到下一轮事件循环：qfluentwidgets 的 currentIndexChanged
        在弹层（RoundMenu）自身的激活/关闭调用栈内同步发出，此时直接
        起线程/改控件会与弹层销毁竞争（用户实测连续快速点击闪退）。
        防抖合并连续切换，只应用最后一次。
        """
        self._update_model_desc(self.combo_model.currentIndex())
        if self._busy or self._model_apply_pending:
            return
        self._model_apply_pending = True
        QTimer.singleShot(0, self._apply_model_combo_change)

    def _apply_model_combo_change(self) -> None:
        self._model_apply_pending = False
        if self._busy:
            return
        self._persist_settings()
        self.refresh()

    # ── 状态刷新 ──

    def refresh(self) -> None:
        """后台刷新状态快照（含 Runtime 探测）。"""
        if self._busy:
            return
        for row in (
            self.row_audio,
            self.row_annotations,
            self.row_vocal,
            self.row_separation,
            self.row_runtime,
            self.row_model,
        ):
            row.set_state("busy", self.tr("检查中…"))
        self.btn_run.setEnabled(False)

        def _task(progress_cb, cancel_check):
            return self._service.snapshot(
                self._project, self._audio_path, probe_runtime=True
            )

        self._run_task(
            _task,
            self._on_snapshot_ready,
            self.tr("正在检查执行条件…"),
            self.tr("正在探测运行环境（首次需导入 PyTorch，可能需要几十秒）…"),
        )

    def _on_snapshot_ready(self, snapshot: AiTimingSnapshot) -> None:
        self._snapshot = snapshot
        self.progress.setValue(0)
        self.status_label.setText(self.tr("就绪"))
        # 上一轮任务的已耗时残留在底部（秒级 tick 停止后不再刷新）
        self.eta_label.setText("")

        # 原始音频（含时长，§3.2）
        if snapshot.audio_ok:
            duration = ""
            if self._audio_duration_ms > 0:
                secs = self._audio_duration_ms / 1000
                duration = f"，时长 {int(secs // 60)}:{int(secs % 60):02d}"
            self.row_audio.set_state(
                "ok", Path(snapshot.audio_path).name + duration
            )
        else:
            self.row_audio.set_state("error", self.tr("未加载音频"))

        if snapshot.project_ok and snapshot.has_content:
            if snapshot.pending_units or snapshot.generation_errors:
                # 直接指出缺注音的字（行/列/字符）——此前只有数量，
                # 用户无从知道该去补哪个字；行内过长自动省略+tooltip
                detail = "、".join(snapshot.pending_units_detail[:3])
                more = snapshot.pending_units - min(
                    3, len(snapshot.pending_units_detail)
                )
                if more > 0:
                    detail = f"{detail} 等 {snapshot.pending_units} 处".strip()
                text = f"{snapshot.pending_units} 个节奏点缺少读音"
                if detail:
                    text += f"：{detail}"
                gen = "、".join(snapshot.generation_errors[:2])
                if gen:
                    text += f" {gen}"
                self.row_annotations.set_state("error", text.strip())
            else:
                self.row_annotations.set_state(
                    "ok", self.tr("既有标注优先，缺口已补足")
                )
        else:
            self.row_annotations.set_state("error", self.tr("工程没有可对齐正文"))

        # 人声素材：多候选时展示下拉选择（§6.1）
        vocal = snapshot.vocal
        self.vocal_combo.clear()
        self.vocal_combo.hide()
        if vocal is None:
            self.row_vocal.set_state("warn", self.tr("未检查（缺少音频）"))
        elif vocal.state in ("session", "cache", "sibling", "separated"):
            source_names = {
                "session": self.tr("工作台会话人声"),
                "cache": self.tr("AI 缓存"),
                "sibling": self.tr("同目录人声文件"),
                "separated": self.tr("本次分离的人声"),
            }
            self.row_vocal.set_state(
                "ok",
                self.tr("可复用（{source}）").format(
                    source=source_names.get(vocal.state, vocal.state)
                ),
            )
        elif vocal.state == "needs_choice":
            self.row_vocal.set_state(
                "warn", self.tr("同目录存在多个人声文件，请选择")
            )
            for path in vocal.choices:
                self.vocal_combo.addItem(path.name, userData=str(path))
            if vocal.choices:
                self.vocal_combo.setCurrentIndex(0)
            self.vocal_combo.show()
        else:
            # 分离组件就绪时这不再是阻断：执行阶段会自动分离。状态灯用
            # ok（绿）而非 busy（蓝）：蓝点是「检查中」的语义，曾被用户
            # 读成「卡在检查/未完成」；这里要表达的是「无需任何处理」。
            if snapshot.separation_available:
                self.row_vocal.set_state("ok", self.tr("将自动分离人声"))
            else:
                self.row_vocal.set_state("warn", self.tr("需要分离人声"))

        # 分离环境（§3.2/§6.2）：embedded 跟随工作台设置；
        # standalone = 共享 Runtime（含 audio-separator）是否已装
        if snapshot.separation_follows_host:
            self.row_separation.set_state(
                "ok", self.tr("跟随工作台「分离人声」设置")
            )
        elif snapshot.separation_available:
            self.row_separation.set_state(
                "ok", self.tr("就绪：无人声时自动分离（UVR-MDX 人声模型）")
            )
        else:
            self.row_separation.set_state(
                "warn",
                self.tr(
                    "未安装：点击「对齐环境 → 安装 / 修复」自动补装"
                    "（已装过环境只会补缺失组件）"
                ),
            )

        runtime = snapshot.runtime
        self.btn_goto_sep.hide()
        if runtime is not None and runtime.available:
            if not runtime.cuda_available and runtime.gpu_name:
                # CPU 版运行环境 + 检测到 NVIDIA 显卡：一键升级 CUDA 版
                self.row_runtime.set_state(
                    "warn",
                    runtime.summary
                    + self.tr(
                        "（检测到 {gpu}，点「安装 / 修复」可升级 CUDA 版）"
                    ).format(gpu=runtime.gpu_name),
                )
            else:
                self.row_runtime.set_state("ok", runtime.summary)
        elif self._install_plan()[0] == "blocked":
            # 嵌入模式宿主 Runtime 未安装：引导去第 2 步（可点击跳转），
            # 或经确认后原生安装兜底
            self.row_runtime.set_state(
                "warn",
                self.tr("请先在第 2 步「音频分离」安装工作台运行环境"),
            )
            self.btn_goto_sep.setVisible(self._open_separation_page is not None)
        else:
            self.row_runtime.set_state(
                "error",
                (runtime.message if runtime else "") or self.tr("对齐环境不可用"),
            )

        model = snapshot.model
        is_mms = self.combo_model.currentIndex() == 1
        self.btn_download_model.setVisible(not is_mms)
        self.btn_recheck.setVisible(not is_mms)
        if is_mms:
            self.row_model.set_state(
                "ok", self.tr("MMS_FA（备选）：随对齐环境自动获取，无需下载")
            )
        elif model is not None and model.is_ready:
            self.row_model.set_state("ok", self._elide_text(model.model_dir))
        else:
            # 告知目标位置：用户在下载前就知道模型会放到哪里（可更改）
            self.row_model.set_state(
                "error",
                self.tr("未安装，将下载到 {path}").format(
                    path=resolve_model_root(self._settings)
                ),
            )

        self._row_model_dir._refresh()
        self._row_cache_dir._refresh()
        self._row_runtime._refresh()

        reasons = snapshot.blocking_reasons
        if reasons:
            self.blocking_label.setText(
                self.tr("执行前需解决：\n") + "\n".join(reasons)
            )
            self.blocking_label.show()
            self.btn_run.setEnabled(False)
        else:
            self.blocking_label.hide()
            self.btn_run.setEnabled(True)

    # ── 动作 ──

    @staticmethod
    def _elide_text(text, width=48):
        text = str(text)
        if len(text) <= width:
            return text
        return text[: width // 2 - 4] + "…" + text[-width // 2 :]

    def _open_dir(self, path: Path) -> None:
        """在系统资源管理器中打开目录（不存在则先创建）。"""
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _on_open_ai_log(self) -> None:
        """打开 AI 打轴统一日志（资源管理器中选中该文件）。

        首次落一行事件保证文件存在；explorer /select 优先（直接定位
        到文件），失败退化为打开所在目录。日志文件是用户反馈的主要
        载体：探测/安装/变体判定/分离/推理/报错全在其中。
        """
        try:
            path = ai_log_path()
            ailog("ui", "用户打开 AI 打轴日志")
            if sys.platform == "win32":
                import subprocess as _sp

                # 不能带 hidden_subprocess_kwargs()：explorer 是 GUI 程序
                # 本就不会闪黑框，而其 STARTUPINFO 的 SW_HIDE 会让
                # Explorer 窗口以隐藏方式创建，点了像没反应。
                _sp.run(
                    ["explorer", "/select,", str(path)],
                    check=False,
                    timeout=10,
                )
                return
        except Exception:
            pass
        try:
            self._open_dir(ai_log_path().parent)
        except Exception:
            pass

    def _confirm_disk_usage(self, estimate_gb: float, detail: str = "") -> bool:
        """大体积下载/安装前的占用提醒：预计大小 + 磁盘实际剩余。"""
        import shutil as _shutil

        free_gb = None
        try:
            root = resolve_model_root(self._settings)
            free_gb = (
                _shutil.disk_usage(root.anchor or root.parent).free
                / 1024
                / 1024
                / 1024
            )
        except Exception:
            pass
        if free_gb is not None:
            free_text = self.tr("，磁盘剩余 {g:.0f}GB").format(g=free_gb)
            if free_gb < estimate_gb:
                free_text += self.tr("（空间不足！）")
        else:
            free_text = ""
        return message_question(
            self,
            self.tr("磁盘占用提醒"),
            self.tr("{detail}预计占用约 {e:g}GB{free}，是否继续？").format(
                detail=detail, e=estimate_gb, free=free_text
            ),
            yes_text=self.tr("继续"),
            no_text=self.tr("取消"),
            default_cancel=False,
        )

    def _on_download_model(self) -> None:
        model_id = self._service.effective_model_id
        if not self._confirm_disk_usage(
            DISK_EST_MODEL_GB,
            self.tr("下载默认对齐模型（权重约 1.2GB）。"),
        ):
            return

        def _task(progress_cb, cancel_check):
            return self._download_service.download(
                model_id,
                "wav2vec2" if self._settings.provider == "wav2vec2" else "mms_fa",
                license_text="CC-BY-NC-SA-4.0（仅限非商业使用）",
                progress=lambda p, m: progress_cb("model", p, m),
                cancel=cancel_check,
            )

        def _done(result) -> None:
            InfoBar.success(
                title=self.tr("模型下载完成"),
                content=self.tr("对齐模型已就绪。"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            self.refresh()

        self._run_task(
            _task,
            _done,
            self.tr("正在下载模型…"),
            self.tr("正在下载（网络波动时进度可能短暂停顿）…"),
        )

    def _install_plan(self) -> tuple:
        """安装模式判定与目标环境分支。

        - ('shared', python.exe)：宿主托管 Runtime，或用户显式选择/
          原生安装过的解释器（嵌入模式下宿主未提供托管 Runtime——
          外部服务/MSST 等配置——时允许独立环境兜底，增量维护）；
        - ('venv', target_dir)：自建 venv（standalone，或嵌入模式
          经用户确认后的原生安装兜底）；
        - ('blocked', '')：嵌入模式且无任何可用解释器——引导去
          工作台第 2 步，不静默裸装。
        """
        managed = (
            self._managed_runtime_python
            if self._managed_runtime_python
            and Path(self._managed_runtime_python).is_file()
            else ""
        )
        chosen = self._settings.runtime_python or ""
        if chosen and Path(chosen).is_file() and chosen != managed:
            return "shared", chosen
        if managed:
            return "shared", managed
        if self._embedded_mode:
            return "blocked", ""
        return "venv", resolve_model_root(self._settings).parent / "ai_runtime"

    def _on_goto_separation(self) -> None:
        """跳转到宿主分离环境页（嵌入模式引导安装）。"""
        ok = False
        if self._open_separation_page is not None:
            try:
                ok = bool(self._open_separation_page())
            except Exception:
                ok = False
        if ok:
            return
        InfoBar.info(
            title=self.tr("请切换页面"),
            content=self.tr(
                "请在主界面进入第 2 步「音频分离」页完成环境安装"
            ),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=5000,
            parent=self,
        )

    def _on_install_runtime(self) -> None:
        mode, target = self._install_plan()
        ailog(
            "ui",
            f"用户点击「安装 / 修复」：mode={mode} target={target} "
            f"frozen={bool(getattr(sys, 'frozen', False))}",
        )
        if mode == "blocked":
            confirmed = message_question(
                self,
                self.tr("独立安装 AI 运行环境"),
                self.tr(
                    "当前工作台分离环境未使用托管 PyMSS（或尚未安装）。"
                    "可以独立安装一份 AI 运行环境（与工作台环境互不影响），"
                    "或先去第 2 步「音频分离」安装托管环境。"
                ),
                yes_text=self.tr("独立安装"),
                no_text=self.tr("取消"),
                default_cancel=True,
            )
            if not confirmed:
                return
            # 用户确认的原生安装兜底：同 standalone 的自建 venv 路径
            mode, target = (
                "venv",
                resolve_model_root(self._settings).parent / "ai_runtime",
            )

        if mode == "venv":
            # 占用提醒（源码运行实测口径：CUDA venv 约 5GB / CPU venv 约
            # 2GB；打包版走发行包路线，完成后约 6GB）。安装期间含下载
            # 缓存的峰值更高，由安装内预检把关。
            # GPU 判定用快照里探测好的 gpu_name——UI 线程上再起
            # nvidia-smi 子进程会卡界面并闪黑框（打包版实测）
            runtime_status = (
                self._snapshot.runtime if self._snapshot is not None else None
            )
            has_gpu = runtime_status is not None and runtime_status.gpu_name
            frozen = bool(getattr(sys, "frozen", False))
            estimate = (
                (DISK_EST_RELEASE_CUDA_GB if frozen else DISK_EST_VENV_CUDA_GB)
                if has_gpu
                else (DISK_EST_RELEASE_CPU_GB if frozen else DISK_EST_VENV_CPU_GB)
            )
            detail = (
                self.tr(
                    "打包版将下载官方运行环境发行包并安装（含 PyTorch，"
                    "下载缓存安装成功后自动清理）。"
                )
                if frozen
                else self.tr("安装 AI 运行环境（含 PyTorch 与依赖）。")
            )
            if not self._confirm_disk_usage(estimate, detail):
                return
        elif mode == "shared":
            # 增量路径体积小：不拦确认，开始消息里注明预估
            pass

        def _task(progress_cb, cancel_check):
            try:
                # 分离模型体检：残缺下载（audio-separator 非原子落盘的
                # 中断残留）会让之后每次分离都必败，环境维护时一并自愈
                from strange_uta_game.backend.application.ai_timing.separation import (
                    ensure_separation_model,
                )

                try:
                    model_note = ensure_separation_model(
                        resolve_model_root(self._settings)
                    )
                except Exception:
                    model_note = ""
                if model_note:
                    try:
                        self._runtime.log_line(f"分离模型体检：{model_note}")
                    except Exception:
                        pass
                    progress_cb("runtime", 1, model_note)
                if mode == "shared":
                    # 方案 B 前置：宿主分离服务进程加载中的 torch 等
                    # .pyd 锁着该解释器的 site-packages，pip 覆盖被锁
                    # 文件可能半失败、留下新旧混杂的安装——先让宿主
                    # 阻塞停服（最多等约 30s）再开 pip；旧宿主未实现
                    # 该可选能力（None）时静默跳过，不阻断安装
                    if self._stop_separation_service is not None:
                        progress_cb(
                            "runtime", 1, self.tr("正在停止工作台分离服务…")
                        )
                        try:
                            stop_result = self._stop_separation_service() or {}
                        except Exception as exc:
                            raise AiRuntimeError(
                                self.tr("停止工作台分离服务失败：{e}").format(
                                    e=exc
                                )
                            ) from exc
                        if not stop_result.get("stopped"):
                            # 拒绝原因来自宿主（中文，如任务执行中），
                            # 原样透传给错误弹窗
                            raise AiRuntimeError(
                                str(
                                    stop_result.get("message")
                                    or self.tr("无法停止工作台分离服务，请稍后重试")
                                )
                            )
                    # 方案 B：向宿主托管解释器增量安装（不建 venv、不重装
                    # torch，torchaudio 按其 torch 版本自动配对）。
                    # 注：不再把「下载镜像」（Hugging Face 端点）传给 pip——
                    # pip 索引由 runtime 层默认阿里源（mirror 参数可覆盖）
                    status = self._runtime.install_shared(
                        str(target),
                        proxy=self._download_proxy,
                        progress=lambda p, m: progress_cb("runtime", p, m),
                        cancel=cancel_check,
                    )
                else:
                    status = self._runtime.install(
                        target,
                        proxy=self._download_proxy,
                        progress=lambda p, m: progress_cb("runtime", p, m),
                        cancel=cancel_check,
                    )
            except Exception as exc:
                # 失败原因补进安装日志，并把日志路径附给错误弹窗——
                # 状态行只留最后一行且会丢失，排查全靠日志
                try:
                    self._runtime.log_line(f"失败：{exc}")
                except Exception:
                    pass
                log_file = _install_log_file_for(mode, target)
                if log_file is not None and log_file.is_file():
                    raise RuntimeError(
                        self.tr("{e}\n详细安装日志：{p}").format(
                            e=exc, p=log_file
                        )
                    ) from exc
                raise
            # 安装成功后记录解释器路径（_done 里持久化）
            self._settings.runtime_python = status.python_path
            return status

        def _done(status) -> None:
            self._persist_settings()
            log_file = _install_log_file_for(mode, target)
            ai_log = ai_log_path()
            # 有 note（CPU 版 + 检测到显卡等）时用警告样式完整告知原因，
            # 不再让用户对着「就绪」猜为什么不是 CUDA
            note = getattr(status, "note", "")
            if note:
                InfoBar.warning(
                    title=self.tr("运行环境已就绪（CPU 版）"),
                    content=(
                        note
                        + self.tr("｜详细日志：{p} / {i}").format(
                            p=ai_log, i=log_file
                        )
                        if log_file is not None
                        else note
                        + self.tr("｜详细日志：{p}").format(p=ai_log)
                    ),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=12000,
                    parent=self,
                )
            else:
                InfoBar.success(
                    title=self.tr("对齐环境就绪"),
                    content=(
                        status.summary
                        + self.tr("（安装日志：{p}）").format(p=log_file)
                        if log_file is not None
                        else status.summary
                    ),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=4000,
                    parent=self,
                )
            self.refresh()

        self._run_task(
            _task,
            _done,
            self.tr("正在安装对齐环境…"),
            self.tr("正在安装（创建虚拟环境与下载大包期间可能数分钟没有输出）…"),
        )

    def _current_cache_root(self) -> Path:
        """当前生效的 AI 缓存根：展示值（快照实例）优先于设置项。"""
        if self._snapshot is not None and self._snapshot.cache_root:
            return Path(self._snapshot.cache_root)
        if self._settings.ai_cache_root:
            return Path(self._settings.ai_cache_root)
        return Path.home()

    def _notify_path_applied(self) -> None:
        """路径变更即时生效提示（注册表/缓存已原地换根）。"""
        InfoBar.success(
            title=self.tr("位置已更新"),
            content=self.tr("新位置已生效。"),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2500,
            parent=self,
        )

    def _notify_busy_path_change(self) -> None:
        """任务运行中禁止改路径：换根会造成下载/缓存跨目录中间态。"""
        InfoBar.warning(
            title=self.tr("任务进行中"),
            content=self.tr("请等待当前任务完成后再更改存储位置。"),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=3000,
            parent=self,
        )

    def _on_change_model_dir(self) -> None:
        if self._busy:
            self._notify_busy_path_change()
            return
        chosen = QFileDialog.getExistingDirectory(
            self, self.tr("选择模型根目录"), str(resolve_model_root(self._settings))
        )
        if chosen:
            self._settings.model_root = chosen
            self._persist_settings()
            # 注册表原地换根：下载/校验立即用新目录，无需重开窗口
            self._registry.retarget(Path(chosen))
            self._notify_path_applied()
            self.refresh()

    def _on_change_cache_dir(self) -> None:
        if self._busy:
            self._notify_busy_path_change()
            return
        # 起点 = 行内当前显示的生效缓存根（而非设置项：独立模式默认
        # 缓存根不在设置里，此前会错误地从用户主目录开始）
        chosen = QFileDialog.getExistingDirectory(
            self,
            self.tr("选择 AI 缓存根目录（独立运行模式生效）"),
            str(self._current_cache_root()),
        )
        if chosen:
            self._settings.ai_cache_root = chosen
            self._persist_settings()
            # 共享缓存实例原地换根（service/人声服务一并跟随）
            self._service.retarget_cache(Path(chosen))
            self._notify_path_applied()
            self.refresh()

    def _on_change_runtime(self) -> None:
        if self._busy:
            # 任务运行中换解释器：在途任务仍持旧环境句柄，且安装/对齐
            # 期间解释器文件被锁定，与路径类更改同口径拦截
            self._notify_busy_path_change()
            return
        # 未显式选择过解释器时，从当前解释器所在目录开始
        start = self._settings.runtime_python or str(Path(sys.executable).parent)
        chosen, _filter = QFileDialog.getOpenFileName(
            self,
            self.tr("选择对齐运行环境的 python.exe"),
            start,
            "python (python.exe python3 python);;All files (*)",
        )
        if chosen:
            self._settings.runtime_python = chosen
            self._persist_settings()
            ailog("ui", f"用户更换对齐运行环境解释器：{chosen}")
            InfoBar.info(
                title=self.tr("已保存"),
                content=self.tr("运行环境已更新，将立即生效。"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self,
            )
            self.refresh()

    def _on_reset_settings(self) -> None:
        self._settings.provider = "wav2vec2"
        self._settings.device = "auto"
        self._settings.tail_snap = True
        self._settings.download_mirror = ""
        self.combo_model.setCurrentIndex(0)
        self.combo_device.setCurrentIndex(0)
        self.combo_mirror.setCurrentIndex(0)
        self._update_mirror_edit_visibility()
        self._persist_settings()
        self.refresh()

    def _on_deep_recheck(self) -> None:
        """深度重新校验（sha256 重算，§3.3「重新校验」）。"""
        model_id = self._service.effective_model_id

        def _task(progress_cb, cancel_check):
            status = self._registry.validate(model_id, deep=True)
            progress_cb("recheck", 100, self.tr("校验完成"))
            return status

        def _done(status) -> None:
            state_text = {
                "ok": self.tr("校验通过"),
                "corrupt": self.tr("校验失败"),
                "incomplete": self.tr("下载未完成"),
                "missing": self.tr("模型未安装"),
            }.get(status.state, status.state)
            (InfoBar.success if status.is_ready else InfoBar.warning)(
                title=self.tr("模型深度校验"),
                content=state_text
                + (f"：{status.message}" if status.message else ""),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )
            self.refresh()

        self._run_task(
            _task,
            _done,
            self.tr("正在深度校验模型…"),
            self.tr("正在校验（大模型哈希计算需要一些时间）…"),
        )

    def _on_run_clicked(self) -> None:
        if self._snapshot is None or not self._snapshot.ready:
            return
        if self._context_checker is not None and not self._context_checker():
            ailog("ui", "执行被拦截：弹窗打开后工程/音频已切换")
            InfoBar.warning(
                title=self.tr("工程已变化"),
                content=self.tr(
                    "工程或音频在弹窗打开后发生了切换，请关闭本窗口后重新打开"
                ),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=6000,
                parent=self,
            )
            return
        self._persist_settings()
        vocal_choice = None
        if self.vocal_combo.isVisible() and self.vocal_combo.count() > 0:
            vocal_choice = self.vocal_combo.currentData()

        def _task(progress_cb, cancel_check):
            return self._service.execute(
                self._project,
                self._audio_path,
                on_progress=progress_cb,
                is_cancelled=cancel_check,
                vocal_choice=Path(vocal_choice) if vocal_choice else None,
            )

        def _done(command) -> None:
            try:
                self._on_applied(command)
            except Exception as exc:  # noqa: BLE001
                self._on_task_failed(f"应用结果失败：{exc}")
                return
            # 不自动关窗：成功提示与保存提醒必须可见（此前挂在本弹窗上
            # 随 close 瞬间消失，造成“静默失败”错觉；数据实际已应用）。
            # success 样式 + 首句明确「已写入工程、可关窗」：曾有用户把
            # 旧的警告样式 + 「尚未保存」开头误读为打轴未成功，追问
            # 是否必须 Ctrl+S 才能关窗
            self.status_label.setText(
                self.tr(
                    "完成：时间轴已写入工程，可直接关闭本窗口"
                    "（Ctrl+S 保存落盘）"
                )
            )
            self.eta_label.setText("")
            self.btn_run.setEnabled(False)
            InfoBar.success(
                title=self.tr("AI 打轴成功"),
                content=self.tr(
                    "时间轴已全部生成并写入当前工程，可直接关闭本窗口"
                    "继续编辑。Ctrl+S 保存后落盘；如需恢复原时间轴，可在"
                    "工具栏撤销一次。结果仅供辅助，请人工校准；和声/重叠"
                    "人声段落务必人工复查。"
                ),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=10000,
                parent=self,
            )

        self._run_task(
            _task,
            _done,
            self.tr("正在执行自动对齐…"),
            self.tr("处理中…（模型加载期间无中间输出，大文件 CPU 需数分钟）"),
        )

    def _on_cancel_clicked(self) -> None:
        """取消需二次确认；确认后丢弃未应用结果（§8.3）。"""
        confirmed = message_question(
            self,
            self.tr("取消 AI 打轴"),
            self.tr("确定取消 AI 打轴吗？当前尚未应用的结果将被丢弃。"),
            yes_text=self.tr("取消任务"),
            no_text=self.tr("继续执行"),
            default_cancel=True,
        )
        if confirmed and self._worker is not None:
            ailog("ui", "用户确认取消 AI 打轴任务")
            self._worker.request_cancel()
            self.status_label.setText(self.tr("正在取消…"))

    def closeEvent(self, event) -> None:
        if self._busy:
            reply = message_question(
                self,
                self.tr("任务进行中"),
                self.tr(
                    "AI 打轴任务仍在进行，关闭窗口将取消任务并丢弃未应用的结果。"
                    "确定关闭吗？"
                ),
                yes_text=self.tr("取消并关闭"),
                no_text=self.tr("继续任务"),
                default_cancel=True,
            )
            if not reply:
                event.ignore()
                return
            if self._worker is not None:
                self._worker.request_cancel()
        # 等待后台线程收尾，避免线程销毁竞态（§9 Qt teardown 坑）
        if self._thread is not None:
            self._thread.quit()
            if not self._thread.wait(5000):
                # 宽限期内未退出（CUDA 上下文释放/模型卸载可能远超 5s，
                # 对齐子进程按取消流程收尾也要时间）：不能销毁仍在运行的
                # QThread（本对话框为父，析构即 "Destroyed while thread is
                # still running" 崩溃）——摘掉父子关系保持引用延迟销毁，
                # 线程结束后经 finished → deleteLater 自行回收；worker
                # 子进程由上面的取消请求经 service 的取消接线终止
                self._thread.setParent(None)
        super().closeEvent(event)


__all__ = ["AiTimingDialog"]
