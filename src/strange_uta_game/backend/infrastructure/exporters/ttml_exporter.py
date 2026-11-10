"""TTML / AMLL TTML 歌词导出器（基础版）。

与 :mod:`strange_uta_game.backend.infrastructure.parsers.ttml_parser` 成对：
导出器把 SUG 的逐字时间轴写成 AMLL TTML DB 可读的 ``itunes:timing="Word"``
文档，并用少量 ``ttm:role`` 扩展保留 SUG 的额外 checkpoint、句中停顿与
连词信息。第三方 TTML 渲染器可以忽略这些 role；SUG 自己重导入时可无损
还原。

本模块不修改任何既有导出器；``TTML`` 只作为 :data:`ALL_EXPORTERS` 中新增的
一项注册。
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional, Sequence
from xml.etree import ElementTree as ET

from strange_uta_game.backend.domain import Character, Project, Sentence

from .base import BaseExporter, ExportError
from ..parsers.ttml_parser import (
    AMLL_NS,
    ITUNES_NS,
    ROLE_SUG_CHECKPOINT,
    ROLE_SUG_EMPTY,
    ROLE_SUG_LINK,
    ROLE_SUG_NO_CP,
    ROLE_SUG_RELEASE,
    ROLE_SUG_UNTIMED,
    TTML_NS,
    TTM_NS,
    TTS_NS,
    XML_NS,
    format_time,
)

# ET 的 namespace 注册是进程级全局状态；只在序列化期间临时设置并恢复，
# 避免 embedded 宿主其它 ElementTree 输出被 SUG 的 TTML 前缀污染。
_NAMESPACE_LOCK = threading.Lock()
_NAMESPACES = (
    ("", TTML_NS),
    ("ttm", TTM_NS),
    ("tts", TTS_NS),
    ("itunes", ITUNES_NS),
    ("amll", AMLL_NS),
    ("xml", XML_NS),
)


def _qname(ns: str, local: str) -> str:
    return f"{{{ns}}}{local}"


def _char_global_ts(char: Character) -> List[int]:
    """取字符的全局时间戳；测试/宿主未调用 set_offset 时回退基础时间戳。"""
    if char.global_timestamps:
        return list(char.global_timestamps)
    return list(char.timestamps)


def _char_release_ts(sentence: Sentence, index: int) -> Optional[int]:
    """字符释放时间：停顿点优先，其次下一字起点，最后 +1ms 兜底。"""
    char = sentence.characters[index]
    release = (
        char.global_sentence_end_ts
        if char.global_sentence_end_ts is not None
        else char.sentence_end_ts
    )
    if release is not None:
        return max(0, int(release))
    if index + 1 < len(sentence.characters):
        next_ts = _char_global_ts(sentence.characters[index + 1])
        if next_ts:
            return max(0, int(next_ts[0]))
    own = _char_global_ts(char)
    if own:
        return max(0, int(own[-1]) + 1)
    return None


def _char_main_end(sentence: Sentence, index: int) -> Optional[int]:
    """主文本 span 的结束时间。

    多 checkpoint 字符的主 span 只覆盖第一个拍的区间（到第二个时间戳），
    后续拍点由相邻空 span 表示；否则主 span 会与 cp span 重叠，违反
    AMLL “同一演唱者的 span 不重叠”约束。
    """
    char = sentence.characters[index]
    timestamps = _char_global_ts(char)
    if len(timestamps) > 1:
        return max(0, int(timestamps[1]))
    return _char_release_ts(sentence, index)


def _sentence_start(sentence: Sentence) -> Optional[int]:
    # 只看普通 checkpoint 时间戳；sentence_end_ts 不能当行首，否则
    # “零节奏点 + 只有释放时刻”的行会被写成 begin=end 再被钳成 +1ms。
    for char in sentence.characters:
        own = _char_global_ts(char)
        if own:
            return max(0, int(own[0]))
    return None


def _sentence_end(sentence: Sentence) -> Optional[int]:
    end = None
    for index in range(len(sentence.characters)):
        release = _char_release_ts(sentence, index)
        if release is not None and (end is None or release > end):
            end = release
    if end is not None:
        return end
    start = _sentence_start(sentence)
    return None if start is None else start + 1


def _set_time(elem: ET.Element, begin: Optional[int], end: Optional[int]) -> None:
    if begin is None:
        return
    begin = max(0, int(begin))
    if end is None or end <= begin:
        end = begin + 1
    elem.set("begin", format_time(begin))
    elem.set("end", format_time(end))


class TTMLExporter(BaseExporter):
    """导出 AMLL TTML DB 风格的逐词 TTML。"""

    @property
    def name(self) -> str:
        return "TTML"

    @property
    def description(self) -> str:
        return "AMLL TTML 逐词歌词（交换格式）"

    @property
    def file_extension(self) -> str:
        return ".ttml"

    @property
    def file_filter(self) -> str:
        return "TTML 歌词文件 (*.ttml)"

    def export(self, project: Project, file_path: str) -> None:
        """导出项目到 .ttml 文件。"""
        file_path = self._ensure_extension(file_path)
        content = self.export_to_text(project)
        try:
            with open(file_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        except Exception as exc:
            raise ExportError(f"写入 TTML 文件失败: {exc}") from exc

    def export_to_text(self, project: Project) -> str:
        """把项目序列化为 TTML 字符串（供 embedded 宿主直接交接）。"""
        self._validate_project(project)
        ns_map = getattr(ET, "_namespace_map", None)
        original = dict(ns_map) if isinstance(ns_map, dict) else None
        with _NAMESPACE_LOCK:
            try:
                for prefix, uri in _NAMESPACES:
                    ET.register_namespace(prefix, uri)
                root = self._build_tree(project)
                xml = ET.tostring(
                    root, encoding="unicode", short_empty_elements=True
                )
            finally:
                if isinstance(ns_map, dict) and original is not None:
                    ns_map.clear()
                    ns_map.update(original)
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml

    # ── 文档树 ─────────────────────────────────────────────────

    def _build_tree(self, project: Project) -> ET.Element:
        root = ET.Element(_qname(TTML_NS, "tt"))
        root.set(_qname(XML_NS, "lang"), self._language(project))
        # 用 tts:extent 触发 tts 命名空间声明（AMLL 规范要求声明 tts）。
        root.set(_qname(TTS_NS, "extent"), "1920px 1080px")
        if any(
            _char_global_ts(char)
            for sentence in project.sentences
            for char in sentence.characters
        ):
            root.set(_qname(ITUNES_NS, "timing"), "Word")

        agent_map = self._agent_map(project)
        self._append_head(root, project, agent_map)
        self._append_body(root, project, agent_map)
        return root

    @staticmethod
    def _language(project: Project) -> str:
        language = ""
        if project.metadata is not None:
            language = str(project.metadata.language or "").strip()
        return language or "ja"

    @staticmethod
    def _agent_map(project: Project) -> Dict[str, str]:
        """singer.id → v1/v2...；保证每个实际出现的 singer 都有 agent。"""
        agent_map: Dict[str, str] = {}
        for index, singer in enumerate(project.singers, start=1):
            agent_map[singer.id] = f"v{index}"
        next_index = len(project.singers) + 1
        for sentence in project.sentences:
            for char in sentence.characters:
                singer_id = char.singer_id or sentence.singer_id
                if singer_id and singer_id not in agent_map:
                    agent_map[singer_id] = f"v{next_index}"
                    next_index += 1
        return agent_map

    def _append_head(
        self,
        root: ET.Element,
        project: Project,
        agent_map: Dict[str, str],
    ) -> None:
        head = ET.SubElement(root, _qname(TTML_NS, "head"))
        metadata = ET.SubElement(head, _qname(TTML_NS, "metadata"))

        singer_by_id = {singer.id: singer for singer in project.singers}
        for singer_id, agent_id in agent_map.items():
            singer = singer_by_id.get(singer_id)
            agent = ET.SubElement(metadata, _qname(TTM_NS, "agent"))
            agent.set(_qname(XML_NS, "id"), agent_id)
            agent.set("type", "person")
            name = (singer.name if singer is not None else "") or singer_id
            name_el = ET.SubElement(agent, _qname(TTM_NS, "name"))
            name_el.set("type", "full")
            name_el.text = name

        meta = project.metadata
        title = (meta.title if meta is not None else "") or "Untitled"
        artist = (meta.artist if meta is not None else "") or ""
        album = (meta.album if meta is not None else "") or ""
        self._append_meta(metadata, "musicName", title)
        if artist:
            self._append_meta(metadata, "artists", artist)
        if album:
            self._append_meta(metadata, "album", album)

        # AMLL 平台 ID / ISRC / 作者等附加元数据原样回写，便于联调时按
        # ncmMusicId 等匹配音源；固定字段不重复写。
        core_keys = {"musicName", "artists", "album", "sugGenerator"}
        extra = getattr(meta, "extra", None) if meta is not None else None
        if isinstance(extra, dict):
            for key, value in extra.items():
                key = str(key)
                if not key or key in core_keys:
                    continue
                values = value if isinstance(value, (list, tuple)) else [value]
                for item in values:
                    if item is None:
                        continue
                    self._append_meta(metadata, key, str(item))
        self._append_meta(metadata, "sugGenerator", "StrangeUtaGame")

    @staticmethod
    def _append_meta(parent: ET.Element, key: str, value: str) -> None:
        elem = ET.SubElement(parent, _qname(AMLL_NS, "meta"))
        elem.set("key", key)
        elem.set("value", value)

    def _append_body(
        self,
        root: ET.Element,
        project: Project,
        agent_map: Dict[str, str],
    ) -> None:
        body = ET.SubElement(root, _qname(TTML_NS, "body"))

        sentences = list(project.sentences)
        starts = [_sentence_start(sentence) for sentence in sentences]
        ends = [_sentence_end(sentence) for sentence in sentences]
        valid_starts = [value for value in starts if value is not None]
        valid_ends = [value for value in ends if value is not None]
        if valid_starts and valid_ends:
            body_start = min(valid_starts)
            body_end = max(valid_ends)
            if body_end <= body_start:
                body_end = body_start + 1
            body.set("dur", format_time(body_end))
            div = ET.SubElement(body, _qname(TTML_NS, "div"))
            div.set("begin", format_time(body_start))
            div.set("end", format_time(body_end))
        else:
            div = ET.SubElement(body, _qname(TTML_NS, "div"))

        line_no = 0
        for sentence, start, end in zip(sentences, starts, ends):
            line_no += 1
            p = ET.SubElement(div, _qname(TTML_NS, "p"))
            p.set(_qname(ITUNES_NS, "key"), f"L{line_no}")
            if not sentence.characters or not sentence.text:
                # SUG 的空行是排版意图；TTML 本身没有空行语义，用扩展 role
                # 保留，避免导出后行数变化。
                p.set(_qname(TTM_NS, "role"), ROLE_SUG_EMPTY)
                continue
            sentence_agent = self._sentence_agent(sentence, agent_map)
            p.set(_qname(TTM_NS, "agent"), sentence_agent)
            _set_time(p, start, end)

            chars = sentence.characters
            for index, char in enumerate(chars):
                self._append_char(
                    p,
                    sentence,
                    index,
                    char,
                    agent_map,
                    sentence_agent=sentence_agent,
                )

    @staticmethod
    def _sentence_agent(
        sentence: Sentence, agent_map: Dict[str, str]
    ) -> str:
        for char in sentence.characters:
            singer_id = char.singer_id or sentence.singer_id
            if singer_id in agent_map:
                return agent_map[singer_id]
        if sentence.singer_id in agent_map:
            return agent_map[sentence.singer_id]
        return next(iter(agent_map.values()), "v1")

    def _append_char(
        self,
        p: ET.Element,
        sentence: Sentence,
        index: int,
        char: Character,
        agent_map: Dict[str, str],
        *,
        sentence_agent: str,
    ) -> None:
        timestamps = _char_global_ts(char)
        begin = timestamps[0] if timestamps else None
        release = _char_release_ts(sentence, index)

        roles: List[str] = []
        if begin is None:
            roles.append(ROLE_SUG_UNTIMED)
        if char.check_count == 0:
            roles.append(ROLE_SUG_NO_CP)
        if char.linked_to_next:
            roles.append(ROLE_SUG_LINK)

        release_needed = False
        if (
            char.is_sentence_end
            and index + 1 < len(sentence.characters)
            and release is not None
        ):
            next_ts = _char_global_ts(sentence.characters[index + 1])
            next_begin = next_ts[0] if next_ts else None
            release_needed = next_begin is None or release < next_begin

        has_extra_checkpoints = len(timestamps) > 1 or char.check_count > max(
            1, len(timestamps)
        )
        singer_id = char.singer_id or sentence.singer_id
        char_agent = agent_map.get(singer_id)
        agent_override = (
            char_agent if char_agent and char_agent != sentence_agent else None
        )

        if char.ruby and char.ruby.parts:
            self._append_ruby_span(
                p,
                char,
                timestamps,
                release,
                roles,
                agent_override,
            )
            if release is not None and (
                release_needed or (begin is None and char.is_sentence_end)
            ):
                self._append_release_span(p, release, agent_override)
            return

        main_end = _char_main_end(sentence, index)
        if release_needed and not has_extra_checkpoints and begin is not None:
            roles.append(ROLE_SUG_RELEASE)
            main_end = release

        span = ET.SubElement(p, _qname(TTML_NS, "span"))
        span.text = char.char
        _set_time(span, begin, main_end)
        if roles:
            span.set(_qname(TTM_NS, "role"), " ".join(roles))
        if agent_override:
            span.set(_qname(TTM_NS, "agent"), agent_override)

        # 非 ruby 的多 checkpoint：主 span 只覆盖第一拍；后续拍点用相邻空
        # span 表示，避免同演唱者 span 重叠。已打轴 cp 写 begin/end；未打轴
        # cp 也写空 role span（无时间），保留 SUG 的半成品 check_count。
        for cp_index, timestamp in enumerate(timestamps[1:], start=1):
            cp_span = ET.SubElement(p, _qname(TTML_NS, "span"))
            cp_end = (
                timestamps[cp_index + 1]
                if cp_index + 1 < len(timestamps)
                else release
            )
            _set_time(cp_span, timestamp, cp_end)
            cp_span.set(_qname(TTM_NS, "role"), ROLE_SUG_CHECKPOINT)
        for _ in range(max(0, char.check_count - max(1, len(timestamps)))):
            cp_span = ET.SubElement(p, _qname(TTML_NS, "span"))
            cp_span.set(_qname(TTM_NS, "role"), ROLE_SUG_CHECKPOINT)

        # 句中停顿的释放时刻用独立空 span 表达，避免把 release 挤进某个
        # checkpoint span 的 end 后与下一拍重叠；也覆盖无普通时间戳的字符。
        if release is not None and (
            (begin is None and char.is_sentence_end)
            or (has_extra_checkpoints and release_needed)
        ):
            self._append_release_span(p, release, agent_override)

    @staticmethod
    def _append_release_span(
        p: ET.Element, release: int, agent: Optional[str]
    ) -> None:
        span = ET.SubElement(p, _qname(TTML_NS, "span"))
        _set_time(span, release, release + 1)
        span.set(_qname(TTM_NS, "role"), ROLE_SUG_RELEASE)
        if agent:
            span.set(_qname(TTM_NS, "agent"), agent)

    def _append_ruby_span(
        self,
        p: ET.Element,
        char: Character,
        timestamps: Sequence[int],
        release: Optional[int],
        roles: List[str],
        agent: Optional[str],
    ) -> None:
        assert char.ruby is not None
        container = ET.SubElement(p, _qname(TTML_NS, "span"))
        container.set(_qname(TTS_NS, "ruby"), "container")
        if roles:
            container.set(_qname(TTM_NS, "role"), " ".join(roles))
        if agent:
            container.set(_qname(TTM_NS, "agent"), agent)

        base = ET.SubElement(container, _qname(TTML_NS, "span"))
        base.set(_qname(TTS_NS, "ruby"), "base")
        base.text = char.char

        text_container = ET.SubElement(container, _qname(TTML_NS, "span"))
        text_container.set(_qname(TTS_NS, "ruby"), "textContainer")
        parts = char.ruby.parts
        for part_index, part in enumerate(parts):
            # 缺失时间戳的 part 保持“未打轴”，不要用前一个/最后一个时间戳
            # 伪造 begin，否则重导入会凭空多出重复 checkpoint。
            part_begin = (
                timestamps[part_index] if part_index < len(timestamps) else None
            )
            if part_begin is None:
                part_end = None
            elif part_index + 1 < len(parts) and part_index + 1 < len(timestamps):
                part_end = timestamps[part_index + 1]
            else:
                part_end = release
            text = ET.SubElement(text_container, _qname(TTML_NS, "span"))
            text.set(_qname(TTS_NS, "ruby"), "text")
            text.text = part.text
            _set_time(text, part_begin, part_end)
