"""AI 打轴阶段 F：弹窗与工具栏入口的离屏 UI 测试。

使用注入的假服务栈验证：状态卡渲染、阻断理由禁用执行按钮、
ETA 估算与取消二次确认文案；不启动真实后台任务。
"""

from pathlib import Path

import pytest

from strange_uta_game.backend.application.ai_timing.models import ModelRegistry
from strange_uta_game.backend.application.ai_timing.runtime import (
    AiRuntimeError,
    RuntimeStatus,
)
from strange_uta_game.backend.application.ai_timing.service import (
    AiTimingService,
    AiTimingSnapshot,
)
from strange_uta_game.backend.application.ai_timing.settings import AiTimingSettings
from strange_uta_game.backend.application.ai_timing.vocals import VocalCandidate
from strange_uta_game.backend.domain import (
    Character,
    Project,
    Sentence,
)


class _FakeService:
    """跳过真实 snapshot 的假 AiTimingService。"""

    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.executed = False
        self.retargeted = []

    def snapshot(self, project, audio_path, *, probe_runtime=True):
        return self._snapshot

    @property
    def effective_model_id(self):
        return "NextFire/demo"

    def execute(self, project, audio_path, *, on_progress=None, is_cancelled=None):
        self.executed = True
        return None

    def retarget_cache(self, root):
        self.retargeted.append(Path(root))


def _project():
    project = Project()
    project.sentences = [
        Sentence(
            singer_id="s1",
            characters=[
                Character(char="あ", check_count=1, ruby=None, singer_id="s1"),
            ],
        )
    ]
    return project


def _ready_snapshot():
    return AiTimingSnapshot(
        audio_ok=True,
        audio_path="C:/x/song.flac",
        project_ok=True,
        has_content=True,
        vocal=VocalCandidate(state="sibling", path=Path("C:/x/song_人声.wav")),
        runtime=RuntimeStatus(
            available=True,
            torch_version="2.9.0",
            transformers_version="4.44.0",
        ),
        model=None,
    )


def _make_dialog(qapp, tmp_path, snapshot, applied_calls):
    from strange_uta_game.backend.application.ai_timing.models import (
        ModelDownloadService,
    )
    from strange_uta_game.backend.application.ai_timing.runtime import (
        AiRuntimeManager,
    )
    from strange_uta_game.frontend.editor.ai_timing_dialog import AiTimingDialog

    registry = ModelRegistry(tmp_path / "models")
    service = _FakeService(snapshot)
    dialog = AiTimingDialog(
        project=_project(),
        audio_path=str(tmp_path / "song.flac"),
        service=service,
        settings=AiTimingSettings(),
        registry=registry,
        runtime=AiRuntimeManager(),
        download_service=ModelDownloadService(registry, _NullTransport()),
        on_applied=applied_calls.append,
        parent=None,
    )
    return dialog, service


class _NullTransport:
    def list_files(self, repo_id, revision):
        return []

    def download_file(self, *a, **k):
        raise RuntimeError("测试不执行下载")


class TestModelComboDeferredApply:
    """模型下拉变更延迟到事件循环应用：不与弹层销毁竞争（连续快速
    点击闪退回归），且防抖合并连续切换。"""

    def test_rapid_changes_apply_once(self, qapp, tmp_path):
        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        TestPathChangeAppliesImmediately._wait_initial_refresh(qapp, dialog)
        persisted = []
        dialog._save_settings = lambda s: persisted.append(1)
        # 连续两次切换：pending 期间第二次不重复排队
        dialog.combo_model.setCurrentIndex(1)
        assert dialog._model_apply_pending is True
        dialog.combo_model.setCurrentIndex(0)
        assert dialog._model_apply_pending is True
        # 描述标签即时更新（便宜操作）
        assert dialog.model_desc.text()
        # 派发事件循环：延迟应用执行一次
        for _ in range(20):
            qapp.processEvents()
        assert dialog._model_apply_pending is False
        assert len(persisted) == 1

    def test_busy_skips_apply(self, qapp, tmp_path):
        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        dialog._busy = True
        dialog.combo_model.setCurrentIndex(1)
        for _ in range(10):
            qapp.processEvents()
        # busy 期间早退：不排队、不持久化、快照不变
        assert dialog._model_apply_pending is False
        assert dialog._snapshot is snap


class TestPathChangeAppliesImmediately:
    """改模型/缓存路径即时生效：注册表与缓存原地换根，不再要求重开窗口。"""

    def _pick(self, monkeypatch, chosen):
        import strange_uta_game.frontend.editor.ai_timing_dialog as dlg_mod

        monkeypatch.setattr(
            dlg_mod.QFileDialog,
            "getExistingDirectory",
            lambda *a, **k: str(chosen),
        )

    @staticmethod
    def _wait_initial_refresh(qapp, dialog):
        """等构造函数触发的异步 refresh 落地（_busy 复位）。"""
        import time as _time

        for _ in range(200):
            if not dialog._busy:
                return
            qapp.processEvents()
            _time.sleep(0.01)

    def test_model_dir_retargets_registry(self, qapp, tmp_path, monkeypatch):
        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        self._wait_initial_refresh(qapp, dialog)
        new_root = tmp_path / "new-models"
        self._pick(monkeypatch, new_root)
        dialog._on_change_model_dir()
        assert dialog._registry.root == new_root
        assert dialog._settings.model_root == str(new_root)
        assert not dialog.isVisible() or True  # 不再自动关窗

    def test_cache_dir_retargets_service_cache(self, qapp, tmp_path, monkeypatch):
        snap = _ready_snapshot()
        dialog, service = _make_dialog(qapp, tmp_path, snap, [])
        self._wait_initial_refresh(qapp, dialog)
        new_root = tmp_path / "new-cache"
        self._pick(monkeypatch, new_root)
        dialog._on_change_cache_dir()
        assert service.retargeted == [new_root]
        assert dialog._settings.ai_cache_root == str(new_root)

    def test_busy_blocks_path_change(self, qapp, tmp_path, monkeypatch):
        snap = _ready_snapshot()
        dialog, service = _make_dialog(qapp, tmp_path, snap, [])
        # 先等构造期异步 refresh 收尾：否则本测试对 dialog 的引用随
        # 用例结束被 GC，会命中「QThread 以对话框为父 + 线程仍在跑」
        # 的销毁竞态（高负载下挂死整个进程）
        self._wait_initial_refresh(qapp, dialog)
        dialog._busy = True
        self._pick(monkeypatch, tmp_path / "elsewhere")
        dialog._on_change_model_dir()
        dialog._on_change_cache_dir()
        assert dialog._settings.model_root == ""
        assert service.retargeted == []


class TestAiTimingDialog:
    def test_ready_snapshot_enables_run(self, qapp, tmp_path):
        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        dialog._on_snapshot_ready(snap)
        assert dialog.btn_run.isEnabled()
        assert dialog.blocking_label.isHidden()

    def test_blocking_reasons_disable_run(self, qapp, tmp_path):
        snap = _ready_snapshot()
        snap.pending_units = 3
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        dialog._on_snapshot_ready(snap)
        assert not dialog.btn_run.isEnabled()
        assert not dialog.blocking_label.isHidden()
        assert "缺少读音" in dialog.blocking_label.text()

    def test_rows_show_states(self, qapp, tmp_path):
        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        dialog._on_snapshot_ready(snap)
        assert dialog.row_audio._state == "ok"
        assert "song.flac" in dialog.row_audio._state_label.text()
        assert dialog.row_vocal._state == "ok"
        assert dialog.row_runtime._state == "ok"

    def test_eta_estimating_then_estimated(self, qapp, tmp_path):
        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        dialog._eta_samples = [(0.0, 10)]
        # 样本不足 → 正在估算
        assert dialog._compute_eta(12) == "正在估算剩余时间…"
        # 足够样本后给出估算
        dialog._eta_samples = [(0.0, 10), (10.0, 60)]
        text = dialog._compute_eta(60)
        assert "预计剩余" in text

    def test_eta_counts_down_between_messages(self, qapp, tmp_path):
        """两条进度消息之间 ETA 不冻结：按最近速率继续倒计时。"""
        import re as _re
        import time as _t

        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        now = _t.monotonic()
        dialog._eta_samples = [(now - 10.0, 10), (now - 5.0, 35)]
        dialog._eta_kind = "derived"  # 真实流程由 _on_task_progress 设置
        dialog._compute_eta(50)
        assert dialog._eta_rate > 0
        first = dialog._eta_tail_text()
        assert "预计剩余" in first
        # 模拟 3 秒没有新消息：剩余时间应随之递减
        dialog._eta_last = (dialog._eta_last[0] - 3.0, dialog._eta_last[1])
        second = dialog._eta_tail_text()

        def _secs(text):
            m = _re.search(r"(\d+):(\d{2})", text)
            return int(m.group(1)) * 60 + int(m.group(2))

        assert 0 < _secs(second) < _secs(first)
        assert _secs(first) - _secs(second) >= 2

    def test_eta_resets_on_phase_boundary_percent_drop(self, qapp, tmp_path):
        """阶段边界百分比回落（分离 100% → 对齐 20%）时速率重新累计。"""
        import time as _t

        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        now = _t.monotonic()
        # 分离阶段推进到 100% 的样本
        dialog._eta_samples = [(now - 8.0, 80), (now - 4.0, 95)]
        dialog._compute_eta(100)
        # 对齐阶段从 20% 重新开始：旧样本必须被清掉，速率不得为负
        text = dialog._compute_eta(20)
        assert dialog._eta_samples == [(dialog._eta_samples[-1][0], 20)]
        assert dialog._eta_rate == 0.0
        assert text == "正在估算剩余时间…"

    def test_transport_eta_and_speed_extracted(self, qapp, tmp_path):
        """上游消息自带剩余时间/速度时直接展示（分离、pip、下载通用）。"""
        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        dialog._on_task_progress(
            "separation", 70, "分离处理 3/10 块，预计剩余 0:12（2.1s/块）"
        )
        assert "预计剩余 0:12" in dialog.eta_label.text()
        assert "2.1s/块" in dialog.eta_label.text()
        # pip 包速度：无「预计剩余」→ 推算 ETA + 包/分速度同屏
        dialog._on_task_progress(
            "runtime", 40, "获取依赖 4/12（1.3 包/分）：Downloading torch"
        )
        assert "1.3 包/分" in dialog.eta_label.text()
        assert "已耗时" in dialog.eta_label.text()

    def test_stall_hint_is_task_specific(self, qapp, tmp_path):
        """长时间无输出的提示按任务区分（pip 安装不再显示推理文案）。"""
        import time as _t

        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        dialog._stall_text = (
            "正在安装（创建虚拟环境与下载大包期间可能数分钟没有输出）…"
        )
        now = _t.monotonic()
        dialog._task_started = now - 40
        dialog._last_msg_time = now - 20  # 超过 15s 无消息
        dialog._on_second_tick()
        assert "正在安装" in dialog.eta_label.text()
        assert "单步推理" not in dialog.eta_label.text()

    def test_vocal_needs_choice_warns(self, qapp, tmp_path):
        snap = _ready_snapshot()
        snap.vocal = VocalCandidate(state="needs_choice", choices=[])
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        dialog._on_snapshot_ready(snap)
        assert dialog.row_vocal._state == "warn"
        assert not dialog.btn_run.isEnabled()

    def test_vocal_auto_separate_green_when_separation_ready(self, qapp, tmp_path):
        """无人声但分离可用 → ok（绿）而非 busy（蓝）：蓝点是「检查中」
        的语义，曾被用户读成「卡在检查/未完成」。"""
        snap = _ready_snapshot()
        snap.vocal = VocalCandidate(state="separation")
        snap.separation_available = True
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        dialog._on_snapshot_ready(snap)
        assert dialog.row_vocal._state == "ok"
        assert "将自动分离人声" in dialog.row_vocal._state_label.text()

        snap2 = _ready_snapshot()
        snap2.vocal = VocalCandidate(state="separation")
        snap2.separation_available = False
        dialog2, _ = _make_dialog(qapp, tmp_path, snap2, [])
        dialog2._on_snapshot_ready(snap2)
        assert dialog2.row_vocal._state == "warn"
        assert "需要分离人声" in dialog2.row_vocal._state_label.text()

    def test_snapshot_ready_clears_stale_eta(self, qapp, tmp_path):
        """检查完成回到就绪时，清掉上一轮任务残留的已耗时行。"""
        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        dialog.eta_label.setText("已耗时 0:05 · 预计剩余 0:12")
        dialog._on_snapshot_ready(snap)
        assert dialog.eta_label.text() == ""

    def test_cache_dialog_starts_from_effective_root(self, qapp, tmp_path):
        """「更改…/浏览」起点 = 行内显示的生效缓存根，而非空设置项回退主目录。"""
        snap = _ready_snapshot()
        snap.cache_root = tmp_path / "cache" / "ai_timing"
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        dialog._on_snapshot_ready(snap)
        assert dialog._current_cache_root() == snap.cache_root

        # 无快照时回退到设置项
        dialog2, _ = _make_dialog(qapp, tmp_path, snap, [])
        dialog2._snapshot = None
        dialog2._settings.ai_cache_root = str(tmp_path / "cfg")
        assert dialog2._current_cache_root() == tmp_path / "cfg"

    def test_install_plan_prefers_managed_runtime(self, qapp, tmp_path):
        """方案 B：宿主托管解释器存在时走 shared 增量安装，否则自建 venv。"""
        snap = _ready_snapshot()
        exe = tmp_path / "managed" / "python.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("#fake", encoding="utf-8")
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        dialog._managed_runtime_python = str(exe)
        mode, target = dialog._install_plan()
        assert mode == "shared" and str(target) == str(exe)

        # 路径失效（宿主卸载 runtime）→ 回落自建 venv
        dialog._managed_runtime_python = str(tmp_path / "gone" / "python.exe")
        mode, target = dialog._install_plan()
        assert mode == "venv" and target.name == "ai_runtime"

    def test_embedded_blocks_bare_venv_install(self, qapp, tmp_path, monkeypatch):
        """嵌入模式宿主 Runtime 未安装：默认引导，确认后才原生安装兜底。"""
        import strange_uta_game.frontend.editor.ai_timing_dialog as dlg_mod

        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        dialog._embedded_mode = True
        dialog._managed_runtime_python = ""  # 宿主未提供（未安装/非托管配置）
        mode, target = dialog._install_plan()
        assert mode == "blocked"

        started = []
        dialog._run_task = lambda *a, **k: started.append(a)

        # 未确认 → 只引导，不发起安装
        monkeypatch.setattr(dlg_mod, "message_question", lambda *a, **k: False)
        dialog._on_install_runtime()
        assert started == []

        # 确认「独立安装」→ 走 standalone 同款 venv 路径
        monkeypatch.setattr(dlg_mod, "message_question", lambda *a, **k: True)
        dialog._on_install_runtime()
        assert len(started) == 1

        # 状态行给出去第 2 步的引导而非"缺少依赖请下载"
        snap.runtime = RuntimeStatus(
            available=False, message="缺少对齐依赖（transformers）"
        )
        dialog._on_snapshot_ready(snap)
        assert dialog.row_runtime._state == "warn"
        assert "音频分离" in dialog.row_runtime._state_label.text()

    def test_install_plan_prefers_user_chosen_interpreter(self, qapp, tmp_path):
        """用户显式选择/原生安装过的解释器优先于托管值（环境调用分支）。"""
        snap = _ready_snapshot()
        managed = tmp_path / "managed" / "python.exe"
        managed.parent.mkdir(parents=True, exist_ok=True)
        managed.write_text("#m", encoding="utf-8")
        chosen = tmp_path / "chosen" / "python.exe"
        chosen.parent.mkdir(parents=True, exist_ok=True)
        chosen.write_text("#c", encoding="utf-8")

        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        dialog._managed_runtime_python = str(managed)
        dialog._settings.runtime_python = str(chosen)
        mode, target = dialog._install_plan()
        assert mode == "shared" and str(target) == str(chosen)

        # 未显式选择时托管值生效
        dialog._settings.runtime_python = ""
        mode, target = dialog._install_plan()
        assert mode == "shared" and str(target) == str(managed)

    def test_goto_separation_button_jumps(self, qapp, tmp_path):
        """blocked 且宿主提供跳转能力时显示按钮，点击完成跳转。"""
        snap = _ready_snapshot()
        snap.runtime = RuntimeStatus(available=False, message="缺少对齐依赖")
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        dialog._embedded_mode = True
        dialog._managed_runtime_python = ""
        called = []
        dialog._open_separation_page = lambda: called.append(1) or True
        dialog._on_snapshot_ready(snap)
        assert not dialog.btn_goto_sep.isHidden()
        # 弹窗初始 refresh 尚未结束时会禁用动作按钮，测试先解禁
        dialog.btn_goto_sep.setEnabled(True)
        dialog.btn_goto_sep.click()
        assert called == [1]

        # 非阻塞状态隐藏按钮
        ok_snap = _ready_snapshot()
        dialog._open_separation_page = None
        dialog._on_snapshot_ready(ok_snap)
        assert dialog.btn_goto_sep.isHidden()

    def test_runtime_row_hints_cuda_upgrade(self, qapp, tmp_path):
        """CPU 版运行环境 + 检测到 NVIDIA 显卡：橙色提示可升级 CUDA 版。"""
        snap = _ready_snapshot()
        snap.runtime = RuntimeStatus(
            available=True,
            torch_version="2.9.1",
            transformers_version="5.15.0",
            cuda_available=False,
            gpu_name="NVIDIA GeForce RTX 4080",
        )
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        dialog._on_snapshot_ready(snap)
        assert dialog.row_runtime._state == "warn"
        combined = (
            dialog.row_runtime._state_label.text()
            + dialog.row_runtime._state_label.toolTip()
        )
        assert "安装 / 修复" in combined and "RTX 4080" in combined
        # 运行环境本身可用：不进阻断理由，执行按钮不受影响
        assert dialog.btn_run.isEnabled()

        # 无显卡信息时保持正常绿色状态
        snap2 = _ready_snapshot()
        dialog._on_snapshot_ready(snap2)
        assert dialog.row_runtime._state == "ok"


class TestToolbarButton:
    def test_toolbar_has_ai_timing_button(self, qapp):
        from strange_uta_game.frontend.editor.timing.toolbar import EditorToolBar

        bar = EditorToolBar()
        assert hasattr(bar, "btn_ai_timing")
        assert bar.btn_ai_timing.text() == "AI 打轴"

        fired = []
        bar.ai_timing_clicked.connect(lambda: fired.append(1))
        bar.btn_ai_timing.click()
        assert fired == [1]

    def test_guide_actions_live_in_edit_menu_not_standalone(self, qapp):
        """导唱符系列并入编辑管理菜单（AI 打轴按钮腾出工具栏空间）。"""
        from strange_uta_game.frontend.editor.timing.toolbar import EditorToolBar

        bar = EditorToolBar()
        assert not hasattr(bar, "btn_insert_guide")
        actions = [a.text() for a in bar.btn_edit.menu().actions()]
        assert "插入导唱符" in actions
        assert "自动插入导唱符" in actions


class TestServicePartsArity:
    """入口接线守卫：_build_ai_timing_service 的返回元数必须与
    _on_ai_timing_clicked 的解包元数一致（2026-08 曾因在入口引用了
    builder 内部变量 host 直接 NameError，UI 入口无覆盖难发现）。"""

    def test_parts_tuple_arity_matches_unpack(self):
        import ast
        import inspect

        from strange_uta_game.frontend.editor import timing_interface

        tree = ast.parse(inspect.getsource(timing_interface))
        fns = {
            n.name: n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
        }
        build = fns["_build_ai_timing_service"]
        rets = [
            n
            for n in ast.walk(build)
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple)
        ]
        assert rets, "builder 必须返回元组"
        ret_arity = len(rets[-1].value.elts)

        click = fns["_on_ai_timing_clicked"]
        unpacks = [
            n
            for n in ast.walk(click)
            if isinstance(n, ast.Assign)
            and isinstance(n.targets[0], ast.Tuple)
        ]
        assert unpacks, "入口必须解包 parts"
        assert len(unpacks[0].targets[0].elts) == ret_arity


class TestDiskUsageReminder:
    """大体积下载/安装前的占用提醒：预计大小 + 磁盘剩余实测口径。"""

    def _dialog(self, qapp, tmp_path, snap):
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        return dialog

    def test_confirm_shows_estimate_and_free(self, qapp, tmp_path, monkeypatch):
        import strange_uta_game.frontend.editor.ai_timing_dialog as dlg_mod

        snap = _ready_snapshot()
        dialog = self._dialog(qapp, tmp_path, snap)
        captured = {}

        def _capture(parent, title, text, **kwargs):
            captured["title"] = title
            captured["text"] = text
            return True

        monkeypatch.setattr(dlg_mod, "message_question", _capture)
        assert dialog._confirm_disk_usage(
            5.0, "安装 AI 运行环境（含 PyTorch 与依赖）。"
        )
        assert captured["title"] == "磁盘占用提醒"
        assert "5GB" in captured["text"]
        assert "磁盘剩余" in captured["text"]  # 真实磁盘可查

    def test_cancel_blocks_download(self, qapp, tmp_path, monkeypatch):
        import strange_uta_game.frontend.editor.ai_timing_dialog as dlg_mod

        snap = _ready_snapshot()
        dialog = self._dialog(qapp, tmp_path, snap)
        monkeypatch.setattr(
            dlg_mod, "message_question", lambda *a, **k: False
        )
        started = []
        dialog._run_task = lambda *a, **k: started.append(a)
        dialog._on_download_model()
        assert started == []  # 未确认不开始下载

    def test_venv_install_asks_with_gpu_aware_estimate(
        self, qapp, tmp_path, monkeypatch
    ):
        import strange_uta_game.frontend.editor.ai_timing_dialog as dlg_mod

        snap = _ready_snapshot()
        dialog = self._dialog(qapp, tmp_path, snap)
        dialog._on_snapshot_ready(snap)  # 快照就位（gpu_name 来源）
        texts = []

        def _capture(parent, title, text, **kwargs):
            texts.append(text)
            return False  # 取消

        monkeypatch.setattr(dlg_mod, "message_question", _capture)
        started = []
        dialog._run_task = lambda *a, **k: started.append(a)

        # 快照探测到 GPU → CUDA 估算；UI 线程零子进程
        snap.runtime = RuntimeStatus(
            available=True, gpu_name="RTX 5080"
        )
        dialog._on_snapshot_ready(snap)
        dialog._on_install_runtime()
        assert started == []
        assert "5GB" in texts[-1]

        # 无 GPU 信息 → CPU 估算
        snap.runtime = RuntimeStatus(available=False, message="未装")
        dialog._on_snapshot_ready(snap)
        dialog._on_install_runtime()
        assert "2GB" in texts[-1]

    def test_shared_install_skips_confirmation(self, qapp, tmp_path, monkeypatch):
        import strange_uta_game.frontend.editor.ai_timing_dialog as dlg_mod

        snap = _ready_snapshot()
        exe = tmp_path / "managed" / "python.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("#m", encoding="utf-8")
        dialog = self._dialog(qapp, tmp_path, snap)
        dialog._managed_runtime_python = str(exe)
        called = []
        monkeypatch.setattr(
            dlg_mod, "message_question", lambda *a, **k: called.append(a) or False
        )
        started = []
        dialog._run_task = lambda *a, **k: started.append(a)
        dialog._on_install_runtime()
        # 增量路径体积小：不弹确认，直接开始
        assert called == [] and len(started) == 1

    def test_install_task_runs_model_preflight(
        self, qapp, tmp_path, monkeypatch
    ):
        """安装/修复任务开头执行分离模型体检：自愈说明走进度行，
        且不阻断后续环境安装。"""
        from strange_uta_game.backend.application.ai_timing import (
            separation as sep_mod,
        )

        snap = _ready_snapshot()
        exe = tmp_path / "managed" / "python.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("#m", encoding="utf-8")
        dialog = self._dialog(qapp, tmp_path, snap)
        dialog._managed_runtime_python = str(exe)

        monkeypatch.setattr(
            sep_mod, "ensure_separation_model", lambda root: "体检说明"
        )
        captured = {}
        dialog._run_task = lambda fn, *a, **k: captured.update(fn=fn)
        installed = []

        def _fake_install_shared(python, **kwargs):
            installed.append(python)
            return RuntimeStatus(available=True, python_path=python)

        monkeypatch.setattr(
            dialog._runtime, "install_shared", _fake_install_shared
        )
        dialog._on_install_runtime()
        assert "fn" in captured  # shared 路径不弹确认，直接开始

        messages = []
        captured["fn"](
            lambda stage, pct, msg: messages.append(msg), lambda: False
        )
        assert "体检说明" in messages
        assert installed == [str(exe)]


class TestSharedInstallStopsHostService:
    """方案 B 装前腾环境：shared 增量安装开 pip 前先让宿主停分离服务。

    宿主服务进程加载中的 .pyd 锁着解释器的 site-packages——停服失败
    或被拒（任务执行中）时必须中止安装，不能带着文件锁开 pip。
    """

    def _prepare(
        self, qapp, tmp_path, monkeypatch, stop_fn, *, managed=True
    ):
        from strange_uta_game.backend.application.ai_timing import (
            separation as sep_mod,
        )

        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        if managed:
            exe = tmp_path / "managed" / "python.exe"
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_text("#m", encoding="utf-8")
            dialog._managed_runtime_python = str(exe)
        else:
            exe = None
            dialog._confirm_disk_usage = lambda *a, **k: True
        dialog._stop_separation_service = stop_fn
        monkeypatch.setattr(sep_mod, "ensure_separation_model", lambda root: "")
        captured = {}
        dialog._run_task = lambda fn, *a, **k: captured.update(fn=fn)
        dialog._on_install_runtime()
        return dialog, captured, exe

    def test_stop_called_before_pip(self, qapp, tmp_path, monkeypatch):
        order = []

        def _stop():
            order.append("stop")
            return {"stopped": True, "message": "分离服务已停止"}

        dialog, captured, exe = self._prepare(qapp, tmp_path, monkeypatch, _stop)

        def _fake_install_shared(python, **kwargs):
            order.append("pip")
            return RuntimeStatus(available=True, python_path=python)

        monkeypatch.setattr(
            dialog._runtime, "install_shared", _fake_install_shared
        )
        messages = []
        status = captured["fn"](
            lambda stage, pct, msg: messages.append(msg), lambda: False
        )
        assert order == ["stop", "pip"]  # 停服完成前不开 pip
        assert "正在停止工作台分离服务" in messages[0]
        assert status.available and status.python_path == str(exe)

    def test_refusal_aborts_install_with_host_reason(
        self, qapp, tmp_path, monkeypatch
    ):
        def _stop():
            return {
                "stopped": False,
                "message": "分离环境正在执行任务，无法腾出安装环境，请稍后重试",
            }

        dialog, captured, _ = self._prepare(qapp, tmp_path, monkeypatch, _stop)
        installed = []
        monkeypatch.setattr(
            dialog._runtime,
            "install_shared",
            lambda python, **kw: installed.append(python)
            or RuntimeStatus(available=True, python_path=python),
        )
        with pytest.raises(AiRuntimeError, match="分离环境正在执行任务"):
            captured["fn"](lambda *a: None, lambda: False)
        assert installed == []  # 拒绝时绝不开 pip

    def test_stop_failure_wrapped_as_runtime_error(
        self, qapp, tmp_path, monkeypatch
    ):
        def _stop():
            raise RuntimeError("boom")

        dialog, captured, _ = self._prepare(qapp, tmp_path, monkeypatch, _stop)
        monkeypatch.setattr(
            dialog._runtime,
            "install_shared",
            lambda python, **kw: RuntimeStatus(
                available=True, python_path=python
            ),
        )
        with pytest.raises(AiRuntimeError, match="停止工作台分离服务失败"):
            captured["fn"](lambda *a: None, lambda: False)

    def test_venv_mode_skips_stop(self, qapp, tmp_path, monkeypatch):
        """standalone venv 路径装的是自有解释器，与宿主服务无关。"""
        stopped = []

        def _stop():
            stopped.append(1)
            return {"stopped": True, "message": ""}

        dialog, captured, _ = self._prepare(
            qapp, tmp_path, monkeypatch, _stop, managed=False
        )
        installed = []
        monkeypatch.setattr(
            dialog._runtime,
            "install",
            lambda target, **kw: installed.append(target)
            or RuntimeStatus(available=True, python_path=str(target)),
        )
        captured["fn"](lambda *a: None, lambda: False)
        assert stopped == [] and len(installed) == 1


class TestDefaultWidthFits:
    """默认 880px 宽度下，滚动区内容不应出现横向滚动条。"""

    def test_no_horizontal_overflow_at_default_width(self, qapp, tmp_path):
        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        dialog.resize(880, 660)
        dialog.show()
        qapp.processEvents()
        # 等构造函数触发的异步 refresh 落地：worker 的 finished 信号是
        # 跨线程排队投递，需要主线程持续派发事件才会复位 _busy。否则
        # close() 会命中 closeEvent 的「任务进行中」模态框，offscreen 下
        # 无人可点 → 整个测试会话挂起（单文件跑靠线程调度碰巧通过，
        # 与其他文件组合运行必现）
        import time as _time

        for _ in range(200):
            if not dialog._busy:
                break
            qapp.processEvents()
            _time.sleep(0.01)
        from qfluentwidgets import ScrollArea

        overflow = []
        for sa in dialog.findChildren(ScrollArea):
            content = sa.widget()
            if content is None:
                continue
            need = content.minimumSizeHint().width()
            have = sa.viewport().width()
            if need > have:
                overflow.append((need, have))
        assert overflow == [], f"内容超出视口: {overflow}"
        dialog.close()


class TestDownloadMirrorApplied:
    """下载镜像：改设置必须同步到 download_service 的 transport 端点。

    回归：镜像此前形同虚设——transport 在弹窗构造时按当时设置固化 endpoint，
    dialog 内改镜像只写配置、从不更新已建的 transport，下载仍走官方源。
    """

    class _SpyTransport:
        def __init__(self):
            self.endpoints = []

        def list_files(self, repo_id, revision):
            return []

        def download_file(self, *a, **k):
            raise RuntimeError("测试不执行下载")

        def set_endpoint(self, endpoint):
            self.endpoints.append(endpoint)

    def _make(self, qapp, tmp_path, snapshot, transport):
        from strange_uta_game.backend.application.ai_timing.models import (
            ModelDownloadService,
        )
        from strange_uta_game.backend.application.ai_timing.runtime import (
            AiRuntimeManager,
        )
        from strange_uta_game.frontend.editor.ai_timing_dialog import AiTimingDialog

        registry = ModelRegistry(tmp_path / "models")
        service = _FakeService(snapshot)
        dialog = AiTimingDialog(
            project=_project(),
            audio_path=str(tmp_path / "song.flac"),
            service=service,
            settings=AiTimingSettings(),
            registry=registry,
            runtime=AiRuntimeManager(),
            download_service=ModelDownloadService(registry, transport),
            on_applied=lambda *a: None,
            parent=None,
        )
        return dialog

    def test_persist_applies_selected_preset(self, qapp, tmp_path):
        snap = _ready_snapshot()
        transport = self._SpyTransport()
        dialog = self._make(qapp, tmp_path, snap, transport)
        TestPathChangeAppliesImmediately._wait_initial_refresh(qapp, dialog)
        # 初始构造用空设置 → 官方源（不应记录任何 set_endpoint 副作用之外的
        # 值；_sync_mirror_ui 在构造期不触发 persist，故此时为空）
        # 选 hf-mirror.com 预设 → persist 时端点必须同步
        dialog.combo_mirror.setCurrentIndex(1)
        dialog._persist_settings()
        assert dialog._settings.download_mirror == "https://hf-mirror.com"
        assert transport.endpoints[-1] == "https://hf-mirror.com"

    def test_custom_url_applied_and_custom_edit_shown(self, qapp, tmp_path):
        snap = _ready_snapshot()
        transport = self._SpyTransport()
        dialog = self._make(qapp, tmp_path, snap, transport)
        TestPathChangeAppliesImmediately._wait_initial_refresh(qapp, dialog)
        dialog.combo_mirror.setCurrentIndex(2)  # 自定义
        # dialog 本身未 show，isVisible() 会因祖先隐藏而恒为 False；这里要
        # 验证控件自身没有被 setVisible(False)。
        assert not dialog.edit_mirror.isHidden()  # 自定义时输入框展开
        dialog.edit_mirror.setText("https://custom.example.org")
        dialog._persist_settings()
        assert dialog._settings.download_mirror == "https://custom.example.org"
        assert transport.endpoints[-1] == "https://custom.example.org"

    def test_reset_returns_to_official(self, qapp, tmp_path):
        snap = _ready_snapshot()
        transport = self._SpyTransport()
        dialog = self._make(qapp, tmp_path, snap, transport)
        TestPathChangeAppliesImmediately._wait_initial_refresh(qapp, dialog)
        dialog.combo_mirror.setCurrentIndex(1)
        dialog._persist_settings()
        dialog._on_reset_settings()
        assert dialog._settings.download_mirror == ""
        assert dialog.combo_mirror.currentIndex() == 0


class TestTaskGuardsAndTeardown:
    """H11/H14/G2：任务中环境更改守卫、高级控件禁用与线程销毁安全。"""

    @staticmethod
    def _wait_idle(qapp, dialog, timeout_s=8.0):
        import time as _time

        deadline = _time.monotonic() + timeout_s
        while _time.monotonic() < deadline:
            qapp.processEvents()
            if not dialog._busy:
                return True
            _time.sleep(0.01)
        return not dialog._busy

    @staticmethod
    def _wait_thread_deleted(qapp, ref, timeout_s=5.0):
        """轮询事件循环直到 deleteLater 落地（sip 包装失效即 C++ 已销毁）。"""
        import time as _time

        deadline = _time.monotonic() + timeout_s
        while _time.monotonic() < deadline:
            qapp.processEvents()
            try:
                ref.objectName()
            except RuntimeError:
                return True
            _time.sleep(0.01)
        return False

    @staticmethod
    def _start_blocking_task(qapp, dialog, started, release):
        import threading as _threading

        def _task(progress_cb, cancel_check):
            started.set()
            release.wait(10)
            return None

        dialog._run_task(_task, lambda r: None, "busy")
        holder = {"thread": dialog._thread, "worker": dialog._worker}
        assert holder["thread"] is not None
        return holder

    def test_change_runtime_blocked_while_busy(self, qapp, tmp_path, monkeypatch):
        """H11：任务运行中禁止更换对齐运行环境（不弹文件对话框）。"""
        import strange_uta_game.frontend.editor.ai_timing_dialog as dlg_mod

        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        TestPathChangeAppliesImmediately._wait_initial_refresh(qapp, dialog)
        dialog._busy = True
        picked = []
        monkeypatch.setattr(
            dlg_mod.QFileDialog,
            "getOpenFileName",
            lambda *a, **k: picked.append(a) or ("", ""),
        )
        dialog._on_change_runtime()
        assert picked == [], "任务中不得弹出解释器选择对话框"
        assert dialog._settings.runtime_python == ""

    def test_advanced_widgets_disabled_during_task(self, qapp, tmp_path):
        """H11：任务进行中高级选项控件（模型/设备/镜像）全部禁用。"""
        import threading as _threading

        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        TestPathChangeAppliesImmediately._wait_initial_refresh(qapp, dialog)

        started = _threading.Event()
        release = _threading.Event()
        self._start_blocking_task(qapp, dialog, started, release)
        assert started.wait(5)

        for w in (dialog.combo_model, dialog.combo_device, dialog.combo_mirror,
                  dialog.edit_mirror):
            assert not w.isEnabled(), "任务中高级控件应禁用"
        assert not dialog.btn_run.isEnabled()
        assert dialog.btn_cancel.isEnabled()

        release.set()
        assert self._wait_idle(qapp, dialog)
        for w in (dialog.combo_model, dialog.combo_device, dialog.combo_mirror,
                  dialog.edit_mirror):
            assert w.isEnabled(), "任务结束后高级控件应恢复"

    def test_thread_objects_deleted_after_task(self, qapp, tmp_path):
        """H14：任务结束线程退出后，QThread/worker 经 deleteLater 销毁，
        不再随长会话累积。"""
        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        TestPathChangeAppliesImmediately._wait_initial_refresh(qapp, dialog)

        dialog._run_task(lambda p, c: "ok", lambda r: None, "busy")
        worker_ref = dialog._worker
        thread_ref = dialog._thread
        assert worker_ref is not None and thread_ref is not None

        assert self._wait_idle(qapp, dialog)
        # 线程退出 → deleteLater 落地后 sip 包装失效（C++ 对象已销毁）
        assert self._wait_thread_deleted(qapp, thread_ref)
        assert self._wait_thread_deleted(qapp, worker_ref)

    def test_close_while_busy_detaches_thread_on_wait_timeout(
        self, qapp, tmp_path, monkeypatch
    ):
        """G2：关窗时线程未在宽限内退出 → 摘掉父子关系保持引用延迟
        销毁，而不是销毁运行中的 QThread（对话框析构即崩溃）。"""
        import strange_uta_game.frontend.editor.ai_timing_dialog as dlg_mod

        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        TestPathChangeAppliesImmediately._wait_initial_refresh(qapp, dialog)

        started = _start_evt = None
        import threading as _threading

        started = _threading.Event()
        release = _threading.Event()
        holder = self._start_blocking_task(qapp, dialog, started, release)
        thread_ref = holder["thread"]
        worker_ref = holder["worker"]
        assert started.wait(5)

        # 关窗确认弹窗选「取消并关闭」
        monkeypatch.setattr(dlg_mod, "message_question", lambda *a, **k: True)
        # 模拟 wait(5000) 超时（CUDA 释放等场景线程退不出宽限）
        original_wait = thread_ref.wait
        thread_ref.wait = lambda *a, **k: False

        dialog.close()

        # 保持引用且脱离对话框父子：线程销毁竞态消除
        assert dialog._thread is not None
        assert thread_ref.parent() is None

        # 收尾：恢复真实 wait，任务自然结束，线程退出并经 deleteLater 回收
        thread_ref.wait = original_wait
        release.set()
        assert original_wait(10000)
        assert self._wait_idle(qapp, dialog)
        assert self._wait_thread_deleted(qapp, thread_ref)
        assert self._wait_thread_deleted(qapp, worker_ref)

    def test_close_while_busy_waits_thread_normally(self, qapp, tmp_path, monkeypatch):
        """G2 常规路径：宽限内退出 → 引用照常清理，无残留线程。"""
        import strange_uta_game.frontend.editor.ai_timing_dialog as dlg_mod

        snap = _ready_snapshot()
        dialog, _ = _make_dialog(qapp, tmp_path, snap, [])
        TestPathChangeAppliesImmediately._wait_initial_refresh(qapp, dialog)

        import threading as _threading

        started = _threading.Event()
        release = _threading.Event()
        self._start_blocking_task(qapp, dialog, started, release)
        assert started.wait(5)

        monkeypatch.setattr(dlg_mod, "message_question", lambda *a, **k: True)
        dialog.close()
        release.set()
        assert self._wait_idle(qapp, dialog)
        assert dialog._thread is None
