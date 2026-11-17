"""自动插入导唱符的非模态工具窗口。"""

from __future__ import annotations

from contextlib import suppress
from copy import deepcopy

from PyQt6.QtCore import QByteArray, QCoreApplication, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QIntValidator
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    CheckBox,
    ComboBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
)

from strange_uta_game.frontend.fluent_widgets import FluentGroupBox
from strange_uta_game.frontend.font_utils import ui_font
from strange_uta_game.frontend.settings.app_settings import AppSettings
from strange_uta_game.frontend.window_sizing import fit_to_screen

from .auto_guide import (
    AutoGuideCandidate,
    AutoGuideParams,
    candidate_preflight,
    scan_auto_guide_candidates,
)


class AutoGuideCandidateWidget(FluentGroupBox):
    locate_requested = pyqtSignal(int, int)

    def __init__(
        self,
        project,
        candidate: AutoGuideCandidate,
        params: AutoGuideParams,
        parent=None,
    ):
        super().__init__("", parent)
        self.candidate = candidate
        self.setFont(ui_font(10))
        layout = self.contentLayout
        layout.setSpacing(5)

        head = QHBoxLayout()
        self.check = CheckBox("", self)
        self.check.setChecked(candidate.existing_count == 0)
        head.addWidget(self.check)
        self.location = PushButton(
            _candidate_location_text(project, candidate),
            self,
        )
        self.location.setToolTip(self.tr("定位到 Karaoke 预览中的目标字符"))
        self.location.clicked.connect(
            lambda: self.locate_requested.emit(
                self.candidate.sentence_idx, self.candidate.char_idx
            )
        )
        head.addWidget(self.location, stretch=1)
        if candidate.target_ms is not None:
            head.addWidget(CaptionLabel(_format_ms(candidate.target_ms), self))
        layout.addLayout(head)

        badges = []
        if candidate.is_first:
            badges.append(self.tr("项目首个"))
        if candidate.forced_by_todo:
            badges.append(self.tr("导唱待办"))
        if candidate.existing_count:
            badges.append(
                self.tr("已有导唱 {n} 字符").format(n=candidate.existing_count)
            )
        if candidate.left_ms is None and not candidate.is_first:
            badges.append(self.tr("左边界未知"))
        elif candidate.gap_ms is not None:
            badges.append(self.tr("空隙 {n}ms").format(n=candidate.gap_ms))
        self.badges = CaptionLabel(" · ".join(badges), self)
        self.badges.setWordWrap(True)
        layout.addWidget(self.badges)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        controls.addWidget(CaptionLabel(self.tr("符号"), self))
        self.symbol = LineEdit(self)
        self.symbol.setText(params.symbol)
        self.symbol.setFixedWidth(90)
        controls.addWidget(self.symbol)

        controls.addWidget(CaptionLabel(self.tr("数量"), self))
        self.count = LineEdit(self)
        self.count.setValidator(QIntValidator(1, 999, self))
        self.count.setText(str(params.count))
        self.count.setFixedWidth(52)
        controls.addWidget(self.count)

        controls.addWidget(CaptionLabel(self.tr("时间"), self))
        self.mode = ComboBox(self)
        self.mode.addItem(self.tr("固定间隔"), userData="fixed")
        self.mode.addItem(self.tr("补足间隔"), userData="fill")
        self.mode.setCurrentIndex(1 if params.fill_gap else 0)
        self.mode.setFixedWidth(108)
        controls.addWidget(self.mode)

        self.duration = LineEdit(self)
        self.duration.setValidator(QIntValidator(100, 18_000_000, self))
        self.duration.setText(str(params.duration_ms))
        self.duration.setFixedWidth(78)
        controls.addWidget(self.duration)
        self.unit = CaptionLabel("ms", self)
        controls.addWidget(self.unit)
        controls.addStretch()
        layout.addLayout(controls)

        advanced = QHBoxLayout()
        self.reverse = CheckBox(self.tr("时间戳反向"), self)
        self.reverse.setChecked(params.reverse)
        advanced.addWidget(self.reverse)
        self.new_line = CheckBox(self.tr("另起一行"), self)
        self.new_line.setChecked(params.new_line)
        advanced.addWidget(self.new_line)
        self.existing_action = None
        if candidate.existing_count:
            advanced.addWidget(CaptionLabel(self.tr("已有导唱"), self))
            self.existing_action = ComboBox(self)
            self.existing_action.addItem(self.tr("替换"), userData="replace")
            self.existing_action.addItem(self.tr("追加"), userData="append")
            self.existing_action.setCurrentIndex(
                1 if params.existing_action == "append" else 0
            )
            self.existing_action.setFixedWidth(90)
            advanced.addWidget(self.existing_action)
        advanced.addStretch()
        layout.addLayout(advanced)

        self.warning = CaptionLabel("", self)
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)

        self.mode.currentIndexChanged.connect(self._refresh_state)
        self.symbol.textChanged.connect(self._refresh_state)
        self.count.textChanged.connect(self._refresh_state)
        self.duration.textChanged.connect(self._refresh_state)
        self.reverse.stateChanged.connect(self._refresh_state)
        self._refresh_state()

    def params(self) -> AutoGuideParams:
        try:
            count = int(self.count.text())
        except ValueError:
            count = 0
        try:
            duration = int(self.duration.text())
        except ValueError:
            duration = 0
        return AutoGuideParams(
            symbol=self.symbol.text().strip(),
            count=count,
            duration_ms=duration,
            fill_gap=self.mode.currentData() == "fill",
            reverse=self.reverse.isChecked(),
            new_line=self.new_line.isChecked(),
            existing_action=(
                self.existing_action.currentData()
                if self.existing_action is not None
                else "replace"
            ),
        )

    def _refresh_state(self, *_args):
        fill = self.mode.currentData() == "fill"
        self.duration.setEnabled(not fill)
        self.unit.setEnabled(not fill)
        check = candidate_preflight(self.candidate, self.params())
        reason = check.get("reason")
        messages = {
            "empty_symbol": self.tr("请输入导唱符"),
            "invalid_count": self.tr("数量必须大于 0"),
            "missing_target": self.tr("缺少目标时间戳，暂不可执行"),
            "missing_left": self.tr("补足间隔需要左边界"),
            "invalid_gap": self.tr("左右边界顺序无效"),
        }
        if not check["executable"]:
            self.warning.setText(messages.get(reason, self.tr("参数无效")))
            self.check.setChecked(False)
            self.check.setEnabled(False)
            return
        self.check.setEnabled(True)
        warnings = []
        if self.candidate.left_ms is None:
            warnings.append(self.tr("未找到左边界，无法检查越界"))
        if check.get("overrun_ms", 0):
            warnings.append(
                self.tr("超出左边界 {n}ms").format(n=check["overrun_ms"])
            )
        if check.get("clamped_count", 0):
            warnings.append(
                self.tr("{n} 个时间戳将限制到 00:00").format(
                    n=check["clamped_count"]
                )
            )
        self.warning.setText(" · ".join(warnings))


class AutoGuideDialog(QDialog):
    locate_requested = pyqtSignal(int, int)
    execute_requested = pyqtSignal(object)

    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.project = project
        self.rows: list[AutoGuideCandidateWidget] = []
        self._row_params: dict[int, AutoGuideParams] = {}
        self._scan_signature = None
        self.setWindowTitle(self.tr("自动插入导唱符"))
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        fit_to_screen(self, 620, 640)
        self.setMinimumSize(520, 420)
        self.setFont(ui_font(10))

        settings = AppSettings()
        self._settings = settings
        saved_geometry = settings.get("auto_guide.window_geometry", "")
        if saved_geometry:
            with suppress(Exception):
                self.restoreGeometry(QByteArray.fromHex(saved_geometry.encode("ascii")))
        auto_saved = settings.get("auto_guide.defaults", None)
        if not isinstance(auto_saved, dict):
            auto_saved = {
                "symbol": settings.get("timing.guide_symbol", "") or "●",
                "count": settings.get("timing.guide_count", 1),
                "duration_ms": settings.get("timing.guide_duration_ms", 1000),
                "fill_gap": settings.get("timing.guide_fill_gap", False),
                "reverse": settings.get("timing.guide_reverse", False),
                "new_line": False,
            }
        self.default_params = AutoGuideParams(**{
            k: auto_saved[k]
            for k in (
                "symbol", "count", "duration_ms", "fill_gap", "reverse", "new_line"
            )
            if k in auto_saved
        })

        root = QVBoxLayout(self)
        root.setSpacing(8)
        self.status = CaptionLabel("", self)
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        scan_group = FluentGroupBox(self.tr("扫描设置"), self)
        scan_row = QHBoxLayout()
        scan_row.addWidget(CaptionLabel(self.tr("最小空隙"), self))
        self.min_gap = LineEdit(self)
        self.min_gap.setValidator(QIntValidator(0, 600_000, self))
        self.min_gap.setText(str(settings.get("auto_guide.min_gap_ms", 3000)))
        self.min_gap.setFixedWidth(100)
        scan_row.addWidget(self.min_gap)
        scan_row.addWidget(CaptionLabel("ms", self))
        scan_row.addStretch()
        self.refresh_btn = PushButton(self.tr("重新扫描"), self)
        scan_row.addWidget(self.refresh_btn)
        scan_group.contentLayout.addLayout(scan_row)
        root.addWidget(scan_group)

        result_group = FluentGroupBox(self.tr("候选位置"), self)
        result_layout = result_group.contentLayout
        list_actions = QHBoxLayout()
        self.result_count = CaptionLabel("", self)
        list_actions.addWidget(self.result_count)
        list_actions.addStretch()
        self.select_all_btn = PushButton(self.tr("全选可执行项"), self)
        self.clear_all_btn = PushButton(self.tr("全部取消"), self)
        self.copy_first_btn = PushButton(self.tr("将首个选中项设置应用到所选项"), self)
        self.copy_first_btn.setToolTip(
            self.tr("按列表顺序，将第一项勾选候选的设置应用到全部勾选候选；未勾选项不变")
        )
        list_actions.addWidget(self.select_all_btn)
        list_actions.addWidget(self.clear_all_btn)
        list_actions.addWidget(self.copy_first_btn)
        result_layout.addLayout(list_actions)

        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.enableTransparentBackground()
        self.container = QWidget(self)
        self.rows_layout = QVBoxLayout(self.container)
        self.rows_layout.setContentsMargins(2, 2, 2, 2)
        self.rows_layout.setSpacing(6)
        self.rows_layout.addStretch()
        self.scroll.setWidget(self.container)
        result_layout.addWidget(self.scroll)
        root.addWidget(result_group, stretch=1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.execute_btn = PrimaryPushButton(self.tr("插入所选候选"), self)
        self.close_btn = PushButton(self.tr("关闭"), self)
        buttons.addWidget(self.execute_btn)
        buttons.addWidget(self.close_btn)
        root.addLayout(buttons)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self.min_gap.textChanged.connect(lambda: self._debounce.start())
        self._debounce.timeout.connect(self.scan)
        self.refresh_btn.clicked.connect(self.scan)
        self.select_all_btn.clicked.connect(self._select_all)
        self.clear_all_btn.clicked.connect(self._clear_all)
        self.copy_first_btn.clicked.connect(self._copy_first)
        self.execute_btn.clicked.connect(self._emit_execute)
        self.close_btn.clicked.connect(self.close)
        self.scan()

    def scan(self):
        text = self.min_gap.text().strip()
        if not text:
            return
        try:
            min_gap = int(text)
        except ValueError:
            return
        for row in self.rows:
            self._row_params[row.candidate.key] = row.params()
            row.setParent(None)
            row.deleteLater()
        self.rows.clear()

        candidates = scan_auto_guide_candidates(self.project, min_gap)
        for candidate in candidates:
            params = deepcopy(
                self._row_params.get(candidate.key, self.default_params)
            )
            row = AutoGuideCandidateWidget(
                self.project, candidate, params, self.container
            )
            row.locate_requested.connect(self.locate_requested)
            self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)
            self.rows.append(row)
        self.result_count.setText(
            self.tr("共 {n} 个候选").format(n=len(self.rows))
        )
        stats = self.project.get_timing_statistics()
        total = stats.get("total_lines", 0)
        done = stats.get("completed_lines", 0)
        if done < total:
            self.status.setText(
                self.tr("⚠ 当前仅有 {done}/{total} 行完成打轴，结果可能不完整。").format(
                    done=done, total=total
                )
            )
        else:
            self.status.setText(self.tr("打轴完成，可以执行自动导唱扫描。"))
        self._settings.set("auto_guide.min_gap_ms", min_gap)
        self._settings.save()
        self._scan_signature = self._project_signature()

    def selected_items(self):
        return [
            (row.candidate, row.params())
            for row in self.rows
            if row.check.isEnabled() and row.check.isChecked()
        ]

    def mark_stale(self):
        self._scan_signature = None
        self.status.setText(
            self.tr("⚠ 项目内容或时间戳已经变化，请重新扫描后再执行。")
        )

    def _select_all(self):
        for row in self.rows:
            if row.check.isEnabled():
                row.check.setChecked(True)

    def _clear_all(self):
        for row in self.rows:
            row.check.setChecked(False)

    def _copy_first(self):
        selected = [row for row in self.rows if row.check.isEnabled() and row.check.isChecked()]
        if not selected:
            return
        first = selected[0]
        params = first.params()
        for row in selected[1:]:
            row.symbol.setText(params.symbol)
            row.count.setText(str(params.count))
            row.mode.setCurrentIndex(1 if params.fill_gap else 0)
            row.duration.setText(str(params.duration_ms))
            row.reverse.setChecked(params.reverse)
            row.new_line.setChecked(params.new_line)
            if row.existing_action is not None:
                row.existing_action.setCurrentIndex(1 if params.existing_action == "append" else 0)

    def _emit_execute(self):
        if self._scan_signature != self._project_signature():
            self.scan()
            self.status.setText(
                self.tr("⚠ 项目内容或时间戳已经变化，候选已刷新，请重新确认。")
            )
            return
        items = self.selected_items()
        if items:
            self.execute_requested.emit(items)

    def _project_signature(self):
        return tuple(
            (
                id(sentence),
                tuple(
                    (
                        id(ch),
                        tuple(ch.timestamps),
                        ch.sentence_end_ts,
                        ch.is_sentence_end,
                        ch.needs_guide,
                        ch.is_guide,
                    )
                    for ch in sentence.characters
                ),
            )
            for sentence in self.project.sentences
        )

    def remember_first_executed(self, items):
        if not items:
            return
        first = sorted(
            items, key=lambda item: (item[0].sentence_idx, item[0].char_idx)
        )[0][1]
        self._settings.set(
            "auto_guide.defaults",
            {
                "symbol": first.symbol,
                "count": first.count,
                "duration_ms": first.duration_ms,
                "fill_gap": first.fill_gap,
                "reverse": first.reverse,
                "new_line": first.new_line,
            },
        )
        self._settings.save()

    def closeEvent(self, event):  # noqa: N802 - Qt virtual method name
        self._settings.set(
            "auto_guide.window_geometry",
            bytes(self.saveGeometry().toHex()).decode("ascii"),
        )
        self._settings.save()
        super().closeEvent(event)


def _format_ms(value: int) -> str:
    value = max(0, int(value))
    minute, remain = divmod(value, 60_000)
    second, millis = divmod(remain, 1000)
    return f"{minute:02d}:{second:02d}.{millis:03d}"


class AutoGuidePreflightDialog(QDialog):
    """汇总所有可继续执行的风险；列表项可定位到目标字符。"""

    locate_requested = pyqtSignal(int, int)

    def __init__(self, project, items, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("自动导唱执行确认"))
        fit_to_screen(self, 560, 440)
        self.setFont(ui_font(10))
        self._continue = False

        root = QVBoxLayout(self)
        intro = CaptionLabel(
            self.tr("以下项目需要确认。点击候选条目可定位对应字符。"), self
        )
        intro.setWordWrap(True)
        root.addWidget(intro)
        self.list = QListWidget(self)
        root.addWidget(self.list, stretch=1)

        stats = project.get_timing_statistics()
        total = stats.get("total_lines", 0)
        done = stats.get("completed_lines", 0)
        if done < total:
            self.list.addItem(
                self.tr("项目状态：仅有 {done}/{total} 行完成打轴").format(
                    done=done, total=total
                )
            )

        for candidate, params in items:
            check = candidate_preflight(candidate, params)
            prefix = _candidate_location_text(project, candidate)
            details = []
            if candidate.left_ms is None:
                details.append(self.tr("找不到左边界，无法检查越界"))
            if check.get("overrun_ms", 0):
                details.append(
                    self.tr(
                        "导唱起点 {start}，左边界 {left}，越界 {overrun}ms"
                    ).format(
                        start=_format_ms(check["first_time_ms"]),
                        left=_format_ms(candidate.left_ms),
                        overrun=check["overrun_ms"],
                    )
                )
            if check.get("clamped_count", 0):
                details.append(
                    self.tr("{n} 个时间戳将限制到 00:00").format(
                        n=check["clamped_count"]
                    )
                )
            if not details:
                continue
            item = QListWidgetItem(prefix + "：" + "；".join(details))
            item.setData(
                Qt.ItemDataRole.UserRole,
                (candidate.sentence_idx, candidate.char_idx),
            )
            self.list.addItem(item)

        buttons = QHBoxLayout()
        buttons.addStretch()
        back = PushButton(self.tr("返回修改"), self)
        proceed = PrimaryPushButton(self.tr("仍然执行"), self)
        buttons.addWidget(back)
        buttons.addWidget(proceed)
        root.addLayout(buttons)
        back.clicked.connect(self.reject)
        proceed.clicked.connect(self._accept_continue)
        self.list.itemClicked.connect(self._locate_item)

    @property
    def has_warnings(self) -> bool:
        return self.list.count() > 0

    def _locate_item(self, item):
        position = item.data(Qt.ItemDataRole.UserRole)
        if position:
            self.locate_requested.emit(*position)

    def _accept_continue(self):
        self._continue = True
        self.accept()

    def should_continue(self) -> bool:
        return self._continue


def _candidate_location_text(project, candidate: AutoGuideCandidate) -> str:
    """沿用打轴界面底部信息栏的“行预览 + 字位置”描述口径。"""
    line_total = len(project.sentences)
    sentence = project.sentences[candidate.sentence_idx]
    line_text = sentence.text.replace("\n", " ")
    preview = line_text[:30] + "..." if len(line_text) > 30 else line_text
    target_text = candidate.target.char.replace("\n", " ")
    template = QCoreApplication.translate(
        "AutoGuideShared",
        "行 {line}/{line_total}: {preview} | 字 {char}/{char_total} | 「{text}」前",
    )
    return template.format(
        line=candidate.sentence_idx + 1,
        line_total=line_total,
        preview=preview,
        char=candidate.char_idx + 1,
        char_total=len(sentence.characters),
        text=target_text,
    )
