"""BPM detection and explicit tempo selection shared by guide dialogs."""

from __future__ import annotations

import math

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ComboBox, DoubleSpinBox, PushButton

from . import task_runner
from .guide_timing import beat_duration_ms


class GuideBpmControl(QWidget):
    value_changed = pyqtSignal(float)

    def __init__(self, editor=None, bpm=120.0, parent=None):
        super().__init__(parent)
        self._editor = editor
        self._worker = None
        self._source = None
        self._attempted = False
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(CaptionLabel(self.tr("BPM"), self))
        self.value = DoubleSpinBox(self)
        self.value.setRange(10.0, 600.0)
        self.value.setDecimals(2)
        self.value.setValue(bpm)
        row.addWidget(self.value)
        self.detect = PushButton(self.tr("重新检测"), self)
        self.detect.clicked.connect(self._detect_or_cancel)
        row.addWidget(self.detect)
        row.addStretch()
        root.addLayout(row)
        self.candidates = ComboBox(self)
        self.candidates.addItem(self.tr("检测后请选择 BPM，也可直接手动输入"))
        self.candidates.currentIndexChanged.connect(self._choose)
        root.addWidget(self.candidates)
        self.status = CaptionLabel(self.tr("每个导唱符占一个四分音符"), self)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.value.valueChanged.connect(self.value_changed)

    def bpm(self) -> float:
        return self.value.value()

    def activate(self):
        if not self._attempted:
            self.start_detection()

    def _audio_source(self):
        if getattr(self._editor, "_audio_loading", False):
            return None
        service = getattr(self._editor, "_timing_service", None)
        if service is None:
            return None
        mono = service.get_mono_samples()
        info = service.get_audio_info()
        if mono is None or len(mono) == 0 or info is None or info.sample_rate <= 0:
            return None
        return (
            mono, int(info.sample_rate),
            getattr(self._editor, "_audio_file_path", None),
        )

    def _source_is_current(self):
        source = self._audio_source()
        return (
            self._source is not None and source is not None
            and source[0] is self._source[0] and source[1:] == self._source[1:]
        )

    def _is_current(self, worker):
        if worker is not self._worker:
            return False
        current = self._source_is_current()
        if not current:
            self.cancel()
            self.status.setText(self.tr("音频已变化，请重新检测 BPM"))
        return current

    def _detect_or_cancel(self):
        if self._worker is None:
            self.start_detection()
        else:
            self.cancel()

    def start_detection(self):
        self.cancel()
        self._attempted = True
        self.candidates.clear()
        self.candidates.addItem(self.tr("请选择检测结果，也可手动输入 BPM"))
        self._source = self._audio_source()
        if self._source is None:
            self.status.setText(self.tr("请先加载音频，或手动输入 BPM"))
            return
        from strange_uta_game.frontend.workers import BpmDetectWorker

        worker = BpmDetectWorker(self._source[0], self._source[1])
        self._worker = worker
        self.detect.setText(self.tr("取消检测"))
        self.status.setText(self.tr("正在检测 BPM…结果需选择后才会应用"))
        task_runner.start_task(
            self, worker, on_finished=self._finished, on_error=self._error,
            is_current=self._is_current,
        )

    def cancel(self):
        if self._worker is not None:
            self._worker.request_cancel()
            self._worker = None
            self.status.setText(self.tr("检测已取消，可手动输入 BPM 或重新检测"))
            self._reset_button()

    def _reset_button(self):
        self.detect.setText(self.tr("重新检测"))

    def _finished(self, result):
        self._worker = None
        self._reset_button()
        try:
            bpm = float(result["bpm"])
            confidence = float(result.get("confidence", 0))
            beat_duration_ms(bpm)
            if not math.isfinite(confidence) or confidence < 0.05:
                raise ValueError
        except (TypeError, ValueError, KeyError):
            self.status.setText(self.tr("未能可靠识别 BPM，请手动输入或重新检测"))
            return
        for tempo, label in (
            (bpm / 2, self.tr("半速候选")),
            (bpm, self.tr("检测值")),
            (bpm * 2, self.tr("倍速候选")),
        ):
            if 10 <= tempo <= 600:
                self.candidates.addItem(f"{tempo:g} BPM（{label}）", userData=tempo)
        self.status.setText(self.tr("请选择适合的 BPM；半速和倍速由检测值换算，也可手动修改"))

    def _error(self, _message):
        self._worker = None
        self._reset_button()
        self.status.setText(self.tr("BPM 检测失败，请手动输入或重新检测"))

    def _choose(self, index):
        bpm = self.candidates.itemData(index) if index >= 0 else None
        if bpm is not None:
            if self._source is not None and not self._source_is_current():
                self.status.setText(self.tr("音频已变化，请重新检测 BPM"))
                self.candidates.setCurrentIndex(0)
                return
            self.value.setValue(bpm)
