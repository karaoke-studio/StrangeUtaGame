"""SugConcatWorker - 在后台线程中执行多个SUG的拼接。

使用标准的 QObject + moveToThread 模式，
通过信号将进度和结果传回主线程。
"""

from __future__ import annotations

import json
from pathlib import Path

import soundfile as sf  # noqa: F401  用于 sndfile probe
from PyQt6.QtCore import QObject, pyqtSignal

from strange_uta_game.backend.domain.models import get_ruby_pause_char
from strange_uta_game.backend.infrastructure.persistence.sug_io import (
    SugMigrator,
    SugProjectParser,
)
from strange_uta_game.frontend.editor.timing.sug_concat_dialog import SugEntry


def _probe_audio_duration_multi(media_path: str) -> int:
    """多渠道尝试读取音频/视频时长（毫秒），失败返回 0。

    按顺序尝试：soundfile → MP3 帧头 → BASS。
    """
    if not media_path or not Path(media_path).exists():
        return 0

    # 1. soundfile（支持 WAV / FLAC / OGG）
    try:
        import soundfile as _sf
        info = _sf.info(media_path)
        if info.duration > 0:
            return int(info.duration * 1000)
    except Exception:
        pass

    # 2. MP3 帧头
    ext = Path(media_path).suffix.lower()
    if ext == ".mp3":
        try:
            return _read_mp3_duration(media_path)
        except Exception:
            pass

    # 3. BASS（支持 MP3/MP4/M4A/AAC/WMA 等所有格式）
    try:
        return _read_duration_via_bass_probe(media_path)
    except Exception:
        pass

    return 0


def _read_mp3_duration(file_path: str) -> int:
    """通过读取 MP3 帧头估算时长。

    优先读首帧内的 Xing/Info 标签按帧数精确估算（VBR 必走此路径）；
    无标签时退回 CBR 估算（文件大小扣除 ID3v2 体积后按首帧码率折算）。
    """
    if not file_path or not Path(file_path).exists():
        return 0
    with open(file_path, "rb") as f:
        # 跳过 ID3v2，记录其总体积（CBR 估算时须从文件大小中扣除）
        id3_size = 0
        header = f.read(10)
        if header[:3] == b"ID3":
            size_bytes = header[6:10]
            id3_size = (
                (size_bytes[0] << 21)
                | (size_bytes[1] << 14)
                | (size_bytes[2] << 7)
                | size_bytes[3]
            ) + 10
            f.seek(id3_size)
        else:
            f.seek(0)

        data = f.read(4096)
        sync_pos = -1
        for i in range(len(data) - 1):
            if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
                sync_pos = i
                break
        if sync_pos >= 0:
            f.seek(id3_size + sync_pos)

        header_bytes = f.read(4)
        if len(header_bytes) < 4:
            return 0

        b1, b2, b3, b4 = header_bytes
        if b1 != 0xFF or (b2 & 0xE0) != 0xE0:
            return 0

        version_idx = (b2 >> 3) & 0x03
        layer_idx = (b2 >> 1) & 0x03  # 1=Layer3, 2=Layer2, 3=Layer1
        bitrate_idx = (b3 >> 4) & 0x0F
        sample_rate_idx = (b3 >> 2) & 0x03
        channel_mode = (b4 >> 6) & 0x03  # 3 = mono

        # 码率表（行 = 层）+ 采样率表（行 = 采样率族）按 MPEG 标准硬编码；
        # MPEG1 与 MPEG2/2.5 使用不同码率表，由 version_idx 选择。
        bitrate_tables = {
            # MPEG1：行 Layer1/2/3
            3: {
                3: [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0],
                2: [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0],
                1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0],
            },
            # MPEG2/2.5：行 Layer1/2（Layer3 与 Layer2 同表）
            2: {
                3: [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0],
                2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
                1: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
            },
        }
        # 采样率表：行 = 采样率族，列 = 版本（0=MPEG1, 1=MPEG2, 2=MPEG2.5）
        sample_rate_table = [
            [44100, 22050, 11025],
            [48000, 24000, 12000],
            [32000, 16000, 8000],
        ]

        version_key = 3 if version_idx == 3 else 2
        bitrate_row = bitrate_tables[version_key].get(layer_idx)
        bitrate_kbps = (
            bitrate_row[bitrate_idx]
            if bitrate_row is not None and bitrate_idx < 16
            else 0
        )
        # 版本号比特：11=MPEG1, 10=MPEG2, 00=MPEG2.5 → 采样率表列号
        version_col = {3: 0, 2: 1, 0: 2}.get(version_idx, -1)
        sample_rate = (
            sample_rate_table[sample_rate_idx][version_col]
            if sample_rate_idx < 3 and version_col >= 0
            else 0
        )

        if bitrate_kbps == 0 or sample_rate == 0:
            return 0

        # Xing/Info 标签优先：按帧数估算（VBR 不再按首帧码率系统性偏高）
        xing_frames = _read_xing_frame_count(f, version_idx, channel_mode)
        if xing_frames > 0:
            # MPEG1 Layer3 每帧 1152 采样；MPEG2/2.5 Layer3 为 576
            samples_per_frame = 1152 if version_idx == 3 else 576
            return int(xing_frames * samples_per_frame / sample_rate * 1000)

        # CBR 兜底：扣除 ID3v2 体积再按首帧码率折算
        file_size = Path(file_path).stat().st_size
        audio_size = max(0, file_size - id3_size)
        return int((audio_size * 8) / (bitrate_kbps * 1000) * 1000)


def _read_xing_frame_count(f, version_idx: int, channel_mode: int) -> int:
    """从当前帧（文件指针位于 4 字节帧头之后）读取 Xing/Info 帧数。

    帧头后隔着 side info：MPEG1 单声道 17 字节 / 立体声 32 字节；
    MPEG2/2.5 单声道 9 字节 / 立体声 17 字节。读到 "Xing"/"Info" 且
    标志位声明帧数字段时返回帧数，否则返回 0。
    """
    side_info = 0
    if version_idx == 3:  # MPEG1
        side_info = 17 if channel_mode == 3 else 32
    else:  # MPEG2 / MPEG2.5
        side_info = 9 if channel_mode == 3 else 17
    f.seek(side_info, 1)  # 相对当前帧头之后跳过 side info

    tag = f.read(4)
    if tag not in (b"Xing", b"Info"):
        return 0
    flags_bytes = f.read(4)
    if len(flags_bytes) < 4:
        return 0
    flags = int.from_bytes(flags_bytes, "big")
    if not (flags & 0x01):  # FRAMES_FLAG 未置位
        return 0
    frames_bytes = f.read(4)
    if len(frames_bytes) < 4:
        return 0
    return int.from_bytes(frames_bytes, "big")


def _read_duration_via_bass_probe(file_path: str) -> int:
    """通过 BASS 临时初始化解码读取音频/视频时长。

    使用设备 0（无声音），解码模式打开文件，读取长度后释放。
    仅支持 Windows（BASS DLL）。
    """
    import ctypes as _ct
    import sys as _sys

    if _sys.platform != "win32":
        return 0

    try:
        from strange_uta_game.backend.infrastructure.audio.bass_engine import (
            _bass,
            _BASS_DIR,
        )

        # 常量
        BASS_STREAM_DECODE = 0x200000
        BASS_POS_BYTE = 0
        BASS_UNICODE = 0x80000000
        BASS_DEVICE_LATENCY = 0x100

        # 签名复用 bass_engine 导入时的配置（只读，不在 worker 里改写共享
        # 的 restype/argtypes）：BASS_ChannelBytes2Seconds 为两参调用、
        # 返回值即秒数。旧代码的 hasattr 守卫恒真、三参签名修正块永不
        # 执行，且按 c_int 误读 float 返回导致 dur 恒 0。
        # 尝试用设备 0 初始化（不同设备号以 0=无声音 最轻量）
        tried_devices = [0, -1]  # -1 = 不初始化任何设备
        bass_inited = False
        for dev in tried_devices:
            try:
                if _bass.BASS_Init(dev, 44100, 0, None, None):
                    bass_inited = True
                    break
            except Exception:
                continue

        if not bass_inited:
            return 0

        try:
            # BASS_StreamCreateFile（Unicode 路径，两参 offset/length 后
            # 直接跟 flags——多余的尾参 0 会触发 argtypes 校验失败）
            try:
                _bass.BASS_StreamCreateFile.restype = _ct.c_uint
                _bass.BASS_StreamCreateFile.argtypes = [
                    _ct.c_int, _ct.c_void_p, _ct.c_uint64, _ct.c_uint64, _ct.c_uint,
                ]
            except Exception:
                pass

            flags = BASS_STREAM_DECODE | BASS_UNICODE
            handle = _bass.BASS_StreamCreateFile(
                False, _ct.c_wchar_p(str(file_path)), 0, 0, flags
            )
            if not handle:
                return 0

            try:
                byte_len = _bass.BASS_ChannelGetLength(handle, BASS_POS_BYTE)
                if byte_len <= 0:
                    return 0
                # 两参调用：返回值即秒数（bass_engine 已配置 restype）
                secs = _bass.BASS_ChannelBytes2Seconds(handle, byte_len)
                if secs > 0:
                    return int(secs * 1000)
                return 0
            finally:
                _bass.BASS_StreamFree(handle)
        finally:
            _bass.BASS_Free()
    except Exception:
        return 0


class SugConcatWorker(QObject):
    """在后台线程中拼接多个 SUG 项目。

    信号:
        progress(stage, current, total): 进度更新
        finished(project, entries_count): 拼接成功
        error(message): 拼接失败
    """

    progress = pyqtSignal(str, int, int)  # (stage_text, current, total)
    finished = pyqtSignal(object, int)    # (Project, entries_count)
    error = pyqtSignal(str)               # (error_message)

    def __init__(self, entries: list[SugEntry], output_name: str, uniform_offset: int):
        super().__init__()
        self._entries = entries
        self._output_name = output_name
        self._uniform_offset = uniform_offset

    def run(self) -> None:
        try:
            self._concat()
        except Exception as e:
            self.error.emit(str(e))

    def _concat(self) -> None:
        total = len(self._entries)
        all_sentences = []
        accumulated_time_ms = 0

        for idx, entry in enumerate(self._entries):
            file_path = entry.file_path
            self.progress.emit(
                f"读取 {Path(file_path).name} ({idx + 1}/{total})",
                idx + 1,
                total,
            )

            if not file_path or not Path(file_path).exists():
                continue

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue

            version = data.get("version", "1.0")
            if version != SugMigrator.CURRENT_VERSION:
                try:
                    data = SugMigrator.migrate(data, version)
                except Exception:
                    continue

            sentences_data = data.get("sentences", [])
            # 停顿符循环外取一次传入（_dict_to_sentence 直调默认会逐句触发
            # 设置读取，见 sug_io._dict_to_project 的同类说明）
            pause_char = get_ruby_pause_char()
            for sentence_data in sentences_data:
                sentence = SugProjectParser._dict_to_sentence(sentence_data, pause_char)
                all_sentences.append(sentence)

            # 先撤销该 SUG 自身的偏移，还原到绝对音频时间轴
            # （global_offset_ms 会被引擎在显示时加上，所以存储时反扣）
            per_offset = entry.offset_ms
            newly_added = all_sentences[-len(sentences_data):]
            if per_offset != 0:
                for sentence in newly_added:
                    for char in sentence.characters:
                        char.timestamps = [ts - per_offset for ts in char.timestamps]
                        if char.sentence_end_ts is not None:
                            char.sentence_end_ts = char.sentence_end_ts - per_offset

            # 再应用累计时间偏移（从前序 SUG 的时长 + 间隔累加）
            if accumulated_time_ms > 0:
                for sentence in newly_added:
                    for char in sentence.characters:
                        char.timestamps = [ts + accumulated_time_ms for ts in char.timestamps]
                        if char.sentence_end_ts is not None:
                            char.sentence_end_ts = char.sentence_end_ts + accumulated_time_ms

            accumulated_time_ms += entry.duration_ms + entry.gap_ms

        if not all_sentences:
            self.error.emit("未能从任何 SUG 文件中读取到有效歌词数据。")
            return

        self.progress.emit("创建项目...", total, total)

        from strange_uta_game.backend.application import ProjectService

        project = ProjectService().create_project()
        project.metadata.title = self._output_name
        project.sentences.clear()
        for s in all_sentences:
            project.sentences.append(s)

        if self._uniform_offset != 0:
            project.global_offset_ms = self._uniform_offset

        self.finished.emit(project, total)
