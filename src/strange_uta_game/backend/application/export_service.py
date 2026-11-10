"""导出服务。

提供统一的项目导出功能。
"""

import re
from typing import Any, Optional, Callable, List, Set, Dict
from dataclasses import dataclass
from pathlib import Path

from strange_uta_game.backend.domain import Project
from strange_uta_game.backend.infrastructure.exporters import (
    IExporter,
    ExportError,
    get_exporter_by_name,
    get_all_exporters,
)
from strange_uta_game.backend.infrastructure.parsers.romaji import RomajiOptions


_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_export_basename(name: str, fallback: str = "untitled") -> str:
    """返回可安全用作跨平台导出文件名的名称（不含扩展名）。

    Windows 不允许若干标点、控制字符、尾随空格/句点和设备保留名。
    音频文件名与项目标题会被自动用作导出名，因此必须在拼接路径前清理。
    名称还会按 UTF-16 code unit 限长，给扩展名和常见文件系统留出余量。
    """
    cleaned = _INVALID_FILENAME_CHARS.sub("_", str(name or "").strip())
    cleaned = cleaned.rstrip(" .")
    if not cleaned:
        cleaned = fallback
    # Windows 对设备名的判断只看第一个句点前的部分；CON.txt 也非法。
    if cleaned.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    # NTFS 单个路径分量通常最多 255 个 UTF-16 code unit。限制为 200，
    # 避免长标题再附加扩展名时触发 WinError 206 / OSError 22。
    encoded = cleaned.encode("utf-16-le")[:400]
    if len(encoded) % 2:
        encoded = encoded[:-1]
    while encoded:
        try:
            cleaned = encoded.decode("utf-16-le")
            break
        except UnicodeDecodeError:
            encoded = encoded[:-2]
    cleaned = cleaned.rstrip(" .")
    return cleaned or fallback


@dataclass
class ExportResult:
    """导出结果"""

    success: bool
    file_path: Optional[str] = None
    format_name: Optional[str] = None
    error_message: Optional[str] = None


class ExportService:
    """导出服务

    管理项目导出到各种格式。
    """

    def __init__(self, progress_callback: Optional[Callable[[int, str], None]] = None):
        """
        Args:
            progress_callback: 进度回调函数 (progress_pct, message)
        """
        self._progress_callback = progress_callback

    def get_available_formats(self) -> List[dict]:
        """获取可用的导出格式列表

        Returns:
            格式信息列表，每项包含：
            - name: 格式名称
            - description: 描述
            - extension: 扩展名
            - filter: 文件选择器过滤字符串
        """
        formats = []

        for exporter in get_all_exporters():
            formats.append(
                {
                    "name": exporter.name,
                    "description": exporter.description,
                    "extension": exporter.file_extension,
                    "filter": exporter.file_filter,
                }
            )

        return formats

    def export(
        self,
        project: Project,
        format_name: str,
        file_path: str,
        offset_ms: int = 0,
        singer_ids: Optional[Set[str]] = None,
        insert_singer_tags: bool = False,
        insert_singer_each_line: bool = False,
        singer_map: Optional[Dict[str, str]] = None,
        export_romaji: bool = True,
        software_compensation_ms: int = 0,
        tag_data: Optional[Dict[str, Any]] = None,
        romaji_options: Optional[RomajiOptions] = None,
    ) -> ExportResult:
        """导出项目

        Args:
            project: 项目对象
            format_name: 格式名称 ('LRC', 'KRA', 'TXT', 等)
            file_path: 导出文件路径
            offset_ms: 已弃用。全局偏移由前端通过 Character.set_offset() 预先写入
                       global_timestamps，本参数保留只为向后兼容，不会被使用。
            singer_ids: 要输出的演唱者/角色 ID 集合（None=全部，仅
                       Nicokara/Kirakara/TTML 有效）
            insert_singer_tags: 是否在演唱者切换处插入【演唱者名】标签
            insert_singer_each_line: 是否在每行行首插入演唱者名称标签
            singer_map: singer_id → 演唱者显示名的映射
            export_romaji: Kirakara 是否输出罗马音层
            software_compensation_ms: 软件导出补偿（毫秒），导出时给时间戳加上此值
            tag_data: Nicokara 元数据标签快照（结构同 AppSettings 的
                      ``nicokara_tags``）。轴分组拆分导出时传入按组过滤后的
                      标签；None = 导出器自行回退读取 AppSettings（原行为）。
            romaji_options: Kirakara 罗马音显示与链接策略

        Returns:
            导出结果
        """
        try:
            # 获取导出器
            exporter = get_exporter_by_name(format_name)

            # 注：offset_ms 参数已弃用 —— 全局偏移由前端在导出前通过
            # Character.set_offset() 写入 global_timestamps / global_sentence_end_ts，
            # 各导出器从中直接读取，不再需要 service 层再次叠加。
            _ = offset_ms

            # 应用软件导出补偿
            if software_compensation_ms != 0:
                import copy
                project = copy.deepcopy(project)
                for sentence in project.sentences:
                    for ch in sentence.characters:
                        if ch.global_timestamps:
                            ch.global_timestamps = [
                                max(0, ts + software_compensation_ms)
                                for ts in ch.global_timestamps
                            ]
                        if ch.global_sentence_end_ts is not None:
                            ch.global_sentence_end_ts = max(
                                0, ch.global_sentence_end_ts + software_compensation_ms
                            )

            # 报告进度
            if self._progress_callback:
                self._progress_callback(0, f"开始导出为 {exporter.name} 格式...")

            # 执行导出（Nicokara 格式传递演唱者参数）
            from strange_uta_game.backend.infrastructure.exporters.nicokara_exporter import (
                NicokaraExporter,
                NicokaraWithRubyExporter,
            )
            from strange_uta_game.backend.infrastructure.exporters.kasugamuki_romaji_exporter import (
                KirakaraExporter,
            )
            from strange_uta_game.backend.infrastructure.exporters.ttml_exporter import (
                TTMLExporter,
            )

            # 翻译/音译/和声是 TTML 专用副轴行；LRC/ASS/Nicokara 等格式
            # 不应把它们当普通歌词输出。轴分组过滤不能兜住“未分组”导出，
            # 因此在这里统一剔除。
            export_project = project
            if not isinstance(exporter, TTMLExporter) and any(
                getattr(sentence, "aux_role", "")
                for sentence in project.sentences
            ):
                import copy

                export_project = copy.copy(project)
                export_project.sentences = [
                    sentence
                    for sentence in project.sentences
                    if not getattr(sentence, "aux_role", "")
                ]

            if isinstance(exporter, KirakaraExporter):
                exporter.export(
                    export_project,
                    file_path,
                    singer_ids=singer_ids,
                    insert_singer_tags=insert_singer_tags,
                    insert_singer_each_line=insert_singer_each_line,
                    singer_map=singer_map,
                    export_romaji=export_romaji,
                    romaji_options=romaji_options,
                )
            elif isinstance(exporter, NicokaraWithRubyExporter):
                exporter.export(
                    export_project,
                    file_path,
                    singer_ids=singer_ids,
                    insert_singer_tags=insert_singer_tags,
                    insert_singer_each_line=insert_singer_each_line,
                    singer_map=singer_map,
                    tag_data=tag_data,
                )
            elif isinstance(exporter, NicokaraExporter):
                exporter.export(
                    export_project,
                    file_path,
                    singer_ids=singer_ids,
                    insert_singer_tags=insert_singer_tags,
                    insert_singer_each_line=insert_singer_each_line,
                    singer_map=singer_map,
                )
            elif isinstance(exporter, TTMLExporter):
                # 轴分组导出：副轴行由所属 Singer 是否在组内决定去留。
                exporter.export(
                    export_project,
                    file_path,
                    singer_ids=singer_ids,
                    compensation_ms=software_compensation_ms,
                )
            else:
                exporter.export(export_project, file_path)

            # 报告完成
            if self._progress_callback:
                self._progress_callback(100, "导出完成")

            return ExportResult(
                success=True,
                file_path=file_path,
                format_name=exporter.name,
            )

        except ExportError as e:
            if self._progress_callback:
                self._progress_callback(0, f"导出失败: {e}")

            return ExportResult(
                success=False,
                error_message=str(e),
            )

        except Exception as e:
            if self._progress_callback:
                self._progress_callback(0, f"导出失败: {e}")

            return ExportResult(
                success=False,
                error_message=f"未知错误: {e}",
            )

    def validate_before_export(self, project: Project) -> List[str]:
        """验证项目是否可以导出

        Args:
            project: 项目对象

        Returns:
            错误信息列表（为空表示可以导出）
        """
        errors = []

        if not project:
            errors.append("项目为空")
            return errors

        if not project.sentences:
            errors.append("项目没有歌词行")

        # 检查是否有时间标签
        sentences_with_tags = sum(1 for s in project.sentences if s.has_timetags)
        if sentences_with_tags == 0:
            errors.append("没有时间标签，导出的歌词将没有时间信息")

        # 统计信息
        stats = project.get_timing_statistics()
        total_lines = stats.get("total_lines", 0)
        completed_lines = stats.get("completed_lines", 0)

        if completed_lines < total_lines:
            errors.append(f"只有 {completed_lines}/{total_lines} 行完成打轴")
            # 找出未完成打轴的行
            untimed_info = self._get_untimed_lines_info(project)
            if untimed_info:
                errors.append(untimed_info)

        return errors

    @staticmethod
    def _get_untimed_lines_info(project: Project) -> str:
        """获取未完成打轴的具体行信息

        返回缩略信息字符串，如 "未打轴行: 第1行「こんにちは」、第3行「さようなら」"
        超过 10 行时截断并显示总数。
        """
        untimed = []
        for i, s in enumerate(project.sentences):
            if getattr(s, "aux_role", ""):
                continue
            if any(c.total_timing_points > 0 for c in s.characters) and not s.is_fully_timed():
                untimed.append(f"第{i + 1}行「{s.text}」")

        if not untimed:
            return ""

        if len(untimed) <= 10:
            return "未打轴行: " + "、".join(untimed)
        else:
            return "未打轴行: " + "、".join(untimed[:10]) + f" 等{len(untimed)}行"

    def validate_ruby_parts(self, project: Project) -> List[dict]:
        """校验项目中所有字符的 rubyPart 数量与 checkCount 是否匹配

        Args:
            project: 项目数据

        Returns:
            不匹配的字符信息列表
        """
        from strange_uta_game.backend.infrastructure.exporters.nicokara_exporter import (
            NicokaraWithRubyExporter,
        )
        exporter = NicokaraWithRubyExporter()
        return exporter.validate_ruby_parts(project)

    def get_ruby_mismatch_detail(
        self, project: Project, max_display: int = 10
    ) -> dict:
        """获取注音分段不匹配详情及按字符/mora 均分预览。

        Args:
            project: 项目数据
            max_display: 预览最多显示的条目数

        Returns:
            {
                "mismatches": [dict, ...],
                "mismatch_lines": [str, ...],
                "char_preview_lines": [str, ...],
                "mora_preview_lines": [str, ...],
                "total": int,
            }
        """
        from strange_uta_game.backend.infrastructure.parsers.inline_format import (
            split_ruby_for_checkpoints,
            distribute_ruby_chars_evenly,
        )

        mismatches = self.validate_ruby_parts(project)

        mismatch_lines = []
        for m in mismatches[:max_display]:
            parts_str = ",".join(m["ruby_parts"])
            mismatch_lines.append(
                f"行 {m['sentence_idx'] + 1} 字符 '{m['char']}': "
                f"check_count={m['check_count']} "
                f"ruby_parts={m['ruby_parts_count']} "
                f"注音='{''.join(m['ruby_parts'])}' "
                f"拆分=[{parts_str}]"
            )
        if len(mismatches) > max_display:
            mismatch_lines.append(f"...还有 {len(mismatches) - max_display} 个不匹配")

        limited = mismatches[:max_display]
        char_lines = []
        mora_lines = []
        for m in limited:
            full_text = "".join(m["ruby_parts"])
            cc = m["check_count"]
            label = f"行 {m['sentence_idx'] + 1} 字符 '{m['char']}': check_count={cc}"

            clean_chars = list(full_text.replace(",", ""))
            if clean_chars and cc > 0:
                char_split = distribute_ruby_chars_evenly(clean_chars, cc)
                mora_split = split_ruby_for_checkpoints(full_text, cc)
            else:
                char_split = [full_text] if full_text else [""]
                mora_split = [full_text] if full_text else [""]

            char_lines.append(f"{label} 拆分=[{','.join(char_split)}]")
            mora_lines.append(f"{label} 拆分=[{','.join(mora_split)}]")

        if len(mismatches) > max_display:
            char_lines.append(f"...还有 {len(mismatches) - max_display} 个")
            mora_lines.append(f"...还有 {len(mismatches) - max_display} 个")

        return {
            "mismatches": mismatches,
            "mismatch_lines": mismatch_lines,
            "char_preview_lines": char_lines,
            "mora_preview_lines": mora_lines,
            "total": len(mismatches),
        }

    def apply_ruby_parts_split(self, project: Project, mode: str) -> None:
        """对所有不匹配字符按指定模式重新拆分注音分段。

        Args:
            project: 项目数据（原地修改）
            mode: "char" 或 "mora"
        """
        from strange_uta_game.backend.infrastructure.parsers.inline_format import (
            split_ruby_for_checkpoints,
            distribute_ruby_chars_evenly,
        )
        from strange_uta_game.backend.domain.models import (
            RubyPart,
            get_ruby_pause_char,
        )

        mismatches = self.validate_ruby_parts(project)
        for m in mismatches:
            sent = project.sentences[m["sentence_idx"]]
            ch = sent.characters[m["char_idx"]]
            if ch.ruby is None or ch.check_count <= 0:
                continue
            full_text = "".join(p.text for p in ch.ruby.parts)
            cc = ch.check_count
            if mode == "char":
                clean = list(full_text.replace(",", ""))
                parts = (
                    distribute_ruby_chars_evenly(clean, cc)
                    if clean
                    else [get_ruby_pause_char()] * max(1, cc)
                )
            else:
                parts = split_ruby_for_checkpoints(full_text, cc)
            ch.ruby.parts = [RubyPart(text=t) for t in parts]
            ch.push_to_ruby()
