"""项目导入服务 — 将外部歌词文件转换为 :class:`Sentence` 列表。

该服务封装 "检测内联格式 / 选择解析器 / 调用 parser 工厂 / 组装 Sentence" 的完整流水线，
使前端无需直接触及 infrastructure.parsers 下的多个模块。

Public API
----------
- :class:`ProjectImportError` — 统一异常类型
- :class:`ProjectImportService.load_lyrics_from_file` — 从文件路径加载歌词为 Sentence 列表
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

from strange_uta_game.backend.domain.entities import Sentence


class ProjectImportError(Exception):
    """项目导入相关错误的统一异常类型。"""


# 内联格式特征： [<idx>|HH:MM:SS]（分钟段 \d{2,}：与 inline_format 的
# _TAG_RE 一致，长曲 ≥100 分钟的时间戳同样能被识别 —— F15）
_INLINE_PATTERN = re.compile(r"\[\d+\|\d{2,}:\d{2}:\d{2}\]")
# LRC 风格时间标签： [MM:SS.xx] 或 [MM:SS:xx]
_LRC_PATTERN = re.compile(r"\[\d{1,2}:\d{2}[.:]\d{2,3}\]")


class ProjectImportService:
    """歌词文件导入服务。"""

    @staticmethod
    def load_lyrics_from_file(path: str, default_singer_id: str) -> List[Sentence]:
        """从文件加载歌词并返回 Sentence 列表。

        流程：
        1. 读取 UTF-8 文本；
        2. 若命中内联格式特征，走 :func:`sentences_from_inline_text`；
        3. 否则交给 :class:`LyricParserFactory`；``.txt`` 且含 LRC 时间标签时强制 LRC 解析；
        4. 统一经 :func:`parse_to_sentences` 组装为 :class:`Sentence` 列表。

        Args:
            path: 歌词文件路径（.lrc/.ass/.srt/.txt/.kra/.ttml 等，内部 parser 工厂识别）。
            default_singer_id: 新 Sentence 默认归属的演唱者 ID。

        Returns:
            Sentence 列表（按文件顺序）。

        Raises:
            ProjectImportError: 读取或解析失败时抛出，原始异常附加为 ``__cause__``。
        """
        # 延迟导入，避免 application 层启动时牵扯 infrastructure 重依赖
        from strange_uta_game.backend.infrastructure.parsers.encoding import (
            decode_lyric_bytes,
        )
        from strange_uta_game.backend.infrastructure.parsers.inline_format import (
            sentences_from_inline_text,
        )
        from strange_uta_game.backend.infrastructure.parsers.kasugamuki_format import (
            sentences_from_kasugamuki,
        )
        from strange_uta_game.backend.infrastructure.parsers.lyric_parser import (
            LRCParser,
            LyricParserFactory,
            parse_to_sentences,
        )

        try:
            # 公共编码回退链（E1）：不再纯 utf-8 读一遍使后续回退失效
            content, _encoding = decode_lyric_bytes(Path(path).read_bytes())
        except OSError as e:
            raise ProjectImportError(f"无法读取歌词文件: {e}") from e
        except UnicodeDecodeError as e:
            raise ProjectImportError(f"无法解码歌词文件: {e}") from e

        try:
            suffix = Path(path).suffix.lower()
            if suffix == ".krl":
                return sentences_from_kasugamuki(content, default_singer_id)
            if suffix == ".ttml":
                # 该服务没有 Singer 列表，无法新建演唱者；所有 agent 先统一
                # 落到 default_singer_id。需要保留多演唱者时用 TTMLParser + 前端导入。
                from strange_uta_game.backend.infrastructure.parsers.ttml_parser import (
                    TTMLParser,
                    build_aux_singer_specs,
                    build_ttml_sentences,
                )

                document = TTMLParser().parse_document(content)
                name_to_id = {
                    agent.name: default_singer_id
                    for agent in document.agents.values()
                    if agent.name
                }
                aux_specs, aux_name_by_key = build_aux_singer_specs(document)
                for name, _role in aux_specs:
                    name_to_id.setdefault(name, default_singer_id)
                return build_ttml_sentences(
                    document,
                    default_singer_id,
                    name_to_id,
                    aux_name_by_key=aux_name_by_key,
                )
            if _INLINE_PATTERN.search(content):
                return sentences_from_inline_text(content, default_singer_id)

            parsed_lines = LyricParserFactory.parse_file(path)
            if Path(path).suffix.lower() == ".txt" and _LRC_PATTERN.search(content):
                parsed_lines = LRCParser().parse(content)
            return parse_to_sentences(parsed_lines, default_singer_id)
        except ProjectImportError:
            raise
        except Exception as e:  # parser 异常统一包装
            raise ProjectImportError(f"歌词解析失败: {e}") from e

    @staticmethod
    def load_lyrics_and_meta_from_file(
        path: str, default_singer_id: str
    ) -> Tuple[List[Sentence], Dict[str, object]]:
        """从文件加载歌词及元数据。

        在 :meth:`load_lyrics_from_file` 的基础上额外返回 metadata 字典。
        目前 ASS 格式会返回 ``{"title": ..., "generator": ...}`` 等字段；
        其他格式 metadata 为空字典。

        Args:
            path: 歌词文件路径
            default_singer_id: 默认演唱者 ID

        Returns:
            (sentences, metadata) 元组
        """
        from strange_uta_game.backend.infrastructure.parsers.ass_parser import (
            ASSParser,
        )
        from strange_uta_game.backend.infrastructure.parsers.encoding import (
            decode_lyric_bytes,
        )
        from strange_uta_game.backend.infrastructure.parsers.inline_format import (
            sentences_from_inline_text,
        )
        from strange_uta_game.backend.infrastructure.parsers.kasugamuki_format import (
            sentences_from_kasugamuki,
        )
        from strange_uta_game.backend.infrastructure.parsers.lyric_parser import (
            LRCParser,
            LyricParserFactory,
            parse_to_sentences,
        )

        try:
            # 公共编码回退链（E1）：不再纯 utf-8 读一遍使后续回退失效
            content, _encoding = decode_lyric_bytes(Path(path).read_bytes())
        except OSError as e:
            raise ProjectImportError(f"无法读取歌词文件: {e}") from e
        except UnicodeDecodeError as e:
            raise ProjectImportError(f"无法解码歌词文件: {e}") from e

        meta: Dict[str, object] = {}

        try:
            if Path(path).suffix.lower() == ".krl":
                return sentences_from_kasugamuki(content, default_singer_id), {
                    "format": "krl",
                }
            suffix = Path(path).suffix.lower()
            if _INLINE_PATTERN.search(content) and suffix != ".ttml":
                sentences = sentences_from_inline_text(content, default_singer_id)
                return sentences, meta

            if suffix == ".ttml":
                from strange_uta_game.backend.infrastructure.parsers.ttml_parser import (
                    TTMLParser,
                    build_aux_singer_specs,
                    build_ttml_sentences,
                )

                parser = TTMLParser()
                document = parser.parse_document(content)
                name_to_id = {
                    agent.name: default_singer_id
                    for agent in document.agents.values()
                    if agent.name
                }
                aux_specs, aux_name_by_key = build_aux_singer_specs(document)
                for name, _role in aux_specs:
                    name_to_id.setdefault(name, default_singer_id)
                sentences = build_ttml_sentences(
                    document,
                    default_singer_id,
                    name_to_id,
                    aux_name_by_key=aux_name_by_key,
                )
                return sentences, parser.parse_metadata()
            if suffix == ".ass":
                # ASS 走显式分支以拿到 metadata（Title / Generator 等）
                parser = ASSParser()
                parsed_lines = parser.parse(content)
                meta = parser.parse_metadata()
                sentences = parse_to_sentences(parsed_lines, default_singer_id)
                return sentences, meta

            parsed_lines = LyricParserFactory.parse_file(path)
            if suffix == ".txt" and _LRC_PATTERN.search(content):
                parsed_lines = LRCParser().parse(content)
            return parse_to_sentences(parsed_lines, default_singer_id), meta
        except ProjectImportError:
            raise
        except Exception as e:
            raise ProjectImportError(f"歌词解析失败: {e}") from e
