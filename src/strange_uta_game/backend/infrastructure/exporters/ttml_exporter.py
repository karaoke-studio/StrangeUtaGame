"""TTML / AMLL TTML 歌词导出器。

与 :mod:`strange_uta_game.backend.infrastructure.parsers.ttml_parser` 成对：
导出器把 SUG 的逐字时间轴写成 AMLL TTML DB 可读的 ``itunes:timing="Word"``
文档，并用少量 ``ttm:role`` 扩展保留 SUG 的额外 checkpoint、句中停顿与
连词信息。第三方 TTML 渲染器可以忽略这些 role；SUG 自己重导入时可无损
还原。

辅助角色（翻译 / 音译 / 背景人声）在导入时已转成独立的
``Sentence``（``aux_role`` + ``aux_parent_id``），并分配给对应角色 Singer。
导出时只按演唱者/轴分组决定内容：副轴行若与父行同组，会合并回父行的
``x-translation`` / ``x-roman`` / ``x-bg`` span；父行不在组内时副轴单独成
``p``。不再提供独立的“辅助轨勾选”。

本模块不修改任何既有导出器；``TTML`` 只作为 :data:`ALL_EXPORTERS` 中新增的
一项注册。
"""

from __future__ import annotations

import re
import threading
from contextvars import ContextVar
from typing import Dict, Iterable, List, Optional, Sequence, Set
from xml.etree import ElementTree as ET

from strange_uta_game.backend.domain import (
    Character,
    Project,
    Sentence,
)

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
    format_time_style,
)

_XML_ID_RE = re.compile(r"^[A-Za-z_][\w.-]*$")
_TIME_FORMATTER: ContextVar = ContextVar(
    "ttml_time_formatter", default=format_time
)
_TIME_STEP: ContextVar = ContextVar("ttml_time_step_ms", default=1)


def _format_time(timestamp_ms: int) -> str:
    return _TIME_FORMATTER.get()(max(0, int(timestamp_ms)))


def _time_style_compatible(project: Project, step: int) -> bool:
    """当前项目所有时间是否能被源风格精度精确表示。"""
    if step <= 1:
        return True
    for sentence in project.sentences:
        for char in sentence.characters:
            for timestamp in _char_global_ts(char):
                if int(timestamp) % step:
                    return False
            for timestamp in char.timestamps:
                if timestamp is not None and int(timestamp) % step:
                    return False
        for char in sentence.characters:
            if char.global_sentence_end_ts is not None:
                if int(char.global_sentence_end_ts) % step:
                    return False
        for value in (sentence.aux_begin, sentence.aux_end):
            if value is not None and int(value) % step:
                return False
    return True

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


def _meta_values(extra: object, key: str) -> List[str]:
    """从 ProjectMetadata.extra 读取某个 AMLL meta 键的全部字符串值。

    多值固定字段优先取保留快照 ``_ttmlSourceMeta``；也兼容直接放在
    extra 顶层的值（历史/宿主写入）。
    """
    if not isinstance(extra, dict):
        return []
    value = extra.get(key)
    source_meta = extra.get("_ttmlSourceMeta")
    if isinstance(source_meta, dict) and key in source_meta:
        value = source_meta.get(key)
    values = value if isinstance(value, (list, tuple)) else [value]
    return [str(item) for item in values if item is not None and str(item) != ""]


def _dedupe(values: Iterable[str]) -> List[str]:
    seen: List[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


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


def _sentence_start(sentence: Sentence, aux_offset: int = 0) -> Optional[int]:
    # 只看普通 checkpoint 时间戳；sentence_end_ts 不能当行首，否则
    # “零节奏点 + 只有释放时刻”的行会被写成 begin=end 再被钳成 +1ms。
    if sentence.aux_role:
        if sentence.aux_begin is not None:
            return max(0, int(sentence.aux_begin) + int(aux_offset))
        for char in sentence.characters:
            own = _char_global_ts(char)
            if own:
                return max(0, int(own[0]))
        return None
    for char in sentence.characters:
        own = _char_global_ts(char)
        if own:
            return max(0, int(own[0]))
    return None


def _sentence_end(sentence: Sentence, aux_offset: int = 0) -> Optional[int]:
    if sentence.aux_role and sentence.aux_end is not None:
        return max(0, int(sentence.aux_end) + int(aux_offset))
    end = None
    for index in range(len(sentence.characters)):
        release = _char_release_ts(sentence, index)
        if release is not None and (end is None or release > end):
            end = release
    if end is not None:
        return end
    start = _sentence_start(sentence, aux_offset)
    return None if start is None else start + 1


def _project_time_offset(
    project: Project,
    sentences: Sequence[Sentence],
    fallback_compensation_ms: int = 0,
) -> int:
    """主文本全局时间与原始时间的差（全局偏移 + 软件补偿）。

    辅助行时间以源文件绝对时间为准，导出时套用与 Character 相同的偏移。
    若项目完全没有带原始时间戳的字符（只有副轴行），回退到全局偏移 +
    调用方传入的软件补偿，避免副轴-only 项目丢失补偿。
    """
    for sentence in sentences:
        for char in sentence.characters:
            own = _char_global_ts(char)
            if own and char.timestamps:
                return int(own[0]) - int(char.timestamps[0])
    return int(project.global_offset_ms or 0) + int(fallback_compensation_ms or 0)


def _aux_segments(sentence: Sentence) -> List[tuple]:
    """按 ``is_sentence_end`` 把辅助行拆回源 span 段。

    返回 ``[(text, begin, end), ...]``；翻译/音译通常只有一段且无时间，
    背景人声则每个源逐词子 span 一段。
    """
    segments: List[tuple] = []
    start = 0
    characters = sentence.characters
    for index, char in enumerate(characters):
        if not char.is_sentence_end:
            continue
        text = "".join(item.char for item in characters[start : index + 1])
        begin: Optional[int] = None
        for item in characters[start : index + 1]:
            if item.timestamps:
                begin = item.timestamps[0]
                break
        end = (
            char.sentence_end_ts
            if char.sentence_end_ts is not None
            else char.global_sentence_end_ts
        )
        if text:
            segments.append((text, begin, end))
        start = index + 1
    if start < len(characters):
        text = "".join(item.char for item in characters[start:])
        begin = None
        for item in characters[start:]:
            if item.timestamps:
                begin = item.timestamps[0]
                break
        if text:
            segments.append((text, begin, None))
    return segments


def _quantized_end(begin: int, end: Optional[int]) -> int:
    """返回在源时间精度下能比 begin 显示得更晚的 end。"""
    begin = max(0, int(begin))
    if end is None or end <= begin:
        end = begin + 1
    begin_text = _format_time(begin)
    end_text = _format_time(end)
    step = max(1, int(_TIME_STEP.get()))
    guard = 0
    while end_text == begin_text and guard < 1000:
        end += step
        end_text = _format_time(end)
        guard += 1
    return end


def _set_time(elem: ET.Element, begin: Optional[int], end: Optional[int]) -> None:
    if begin is None:
        return
    begin = max(0, int(begin))
    end = _quantized_end(begin, end)
    elem.set("begin", _format_time(begin))
    elem.set("end", _format_time(end))


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

    def export(
        self,
        project: Project,
        file_path: str,
        *,
        singer_ids: Optional[Iterable[str]] = None,
        compensation_ms: int = 0,
    ) -> None:
        """导出项目到 .ttml 文件。

        Args:
            singer_ids: 只导出这些演唱者/角色所属的内容（None = 全部）。
                轴分组通过该参数决定每个文件包含哪些声部。
            compensation_ms: 副轴-only 项目的时间补偿兜底（主行字符已由
                ExportService 预先写入 global_timestamps 时无需重复叠加）。
        """
        file_path = self._ensure_extension(file_path)
        content = self.export_to_text(
            project,
            singer_ids=singer_ids,
            compensation_ms=compensation_ms,
        )
        try:
            with open(file_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        except Exception as exc:
            raise ExportError(f"写入 TTML 文件失败: {exc}") from exc

    def export_to_text(
        self,
        project: Project,
        *,
        singer_ids: Optional[Iterable[str]] = None,
        compensation_ms: int = 0,
    ) -> str:
        """把项目序列化为 TTML 字符串（供 embedded 宿主直接交接）。

        ``singer_ids`` 用于按轴分组导出：包含组内演唱者的主行/副轴行；
        副轴行若其主行也在本组，会合并回主行的 x-* span，否则单独成行。
        """
        self._validate_project(project)
        selected_ids = self._normalize_singer_ids(singer_ids)
        ns_map = getattr(ET, "_namespace_map", None)
        original = dict(ns_map) if isinstance(ns_map, dict) else None
        extra = (
            getattr(project.metadata, "extra", None)
            if project.metadata is not None
            else None
        )
        style = extra.get("_ttmlTimeStyle") if isinstance(extra, dict) else None
        precision = (
            max(0, min(3, int(style.get("precision", 3) or 0)))
            if isinstance(style, dict)
            else 3
        )
        step = 10 ** (3 - precision)
        if isinstance(style, dict) and style and not _time_style_compatible(
            project, step
        ):
            # 用户在 SUG 里打了毫秒级时间，源风格精度不足以无损表示；
            # 回退到完整的 HH:MM:SS.mmm，避免静默截断。
            style = None
            step = 1
        formatter = (
            (lambda value: format_time_style(value, style))
            if isinstance(style, dict) and style
            else format_time
        )
        token = _TIME_FORMATTER.set(formatter)
        step_token = _TIME_STEP.set(step)
        try:
            with _NAMESPACE_LOCK:
                try:
                    for prefix, uri in _NAMESPACES:
                        ET.register_namespace(prefix, uri)
                    root = self._build_tree(
                        project,
                        singer_ids=selected_ids,
                        compensation_ms=compensation_ms,
                    )
                    xml = ET.tostring(
                        root, encoding="unicode", short_empty_elements=True
                    )
                finally:
                    if isinstance(ns_map, dict) and original is not None:
                        ns_map.clear()
                        ns_map.update(original)
        finally:
            _TIME_FORMATTER.reset(token)
            _TIME_STEP.reset(step_token)
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml

    @staticmethod
    def _normalize_singer_ids(
        singer_ids: Optional[Iterable[str]],
    ) -> Optional[Set[str]]:
        if singer_ids is None:
            return None
        return {str(sid) for sid in singer_ids if sid}

    # ── 文档树 ─────────────────────────────────────────────────

    def _build_tree(
        self,
        project: Project,
        *,
        singer_ids: Optional[Set[str]] = None,
        compensation_ms: int = 0,
    ) -> ET.Element:
        plan = self._filter_sentences(project, singer_ids)
        if not plan:
            raise ExportError("导出范围内没有歌词行")
        all_sentences = [main for main, _aux in plan]
        all_sentences.extend(
            aux for _main, aux_list in plan for aux in aux_list
        )
        keep_singer_ids = (
            None
            if singer_ids is None
            else self._used_singer_ids(all_sentences) | set(singer_ids)
        )
        if keep_singer_ids is not None:
            # 副轴 span 的 ttm:agent 可能指向一个只唱和声的 Singer；即使它
            # 没有主歌词，也要保留 head 里的 agent 定义，避免过滤导出丢属性。
            by_source_agent = {
                singer.ttml_agent_id: singer
                for singer in project.singers
                if singer.ttml_agent_id and not singer.ttml_role
            }
            for _main, aux_list in plan:
                for aux in aux_list:
                    singer = by_source_agent.get(aux.aux_agent)
                    if singer is not None:
                        keep_singer_ids.add(singer.id)
        agent_map = self._agent_map(project, keep_singer_ids)

        root = ET.Element(_qname(TTML_NS, "tt"))
        root.set(_qname(XML_NS, "lang"), self._language(project))
        # 用 tts:extent 触发 tts 命名空间声明（AMLL 规范要求声明 tts）。
        root.set(_qname(TTS_NS, "extent"), "1920px 1080px")
        if any(
            _char_global_ts(char)
            for sentence in all_sentences
            for char in sentence.characters
        ):
            root.set(_qname(ITUNES_NS, "timing"), "Word")

        self._append_head(root, project, agent_map)
        self._append_body(
            root,
            project,
            plan,
            agent_map,
            compensation_ms=compensation_ms,
        )
        return root

    @staticmethod
    def _used_singer_ids(sentences: Sequence[Sentence]) -> Set[str]:
        used: Set[str] = set()
        for sentence in sentences:
            if sentence.singer_id:
                used.add(sentence.singer_id)
            for char in sentence.characters:
                singer_id = char.singer_id or sentence.singer_id
                if singer_id:
                    used.add(singer_id)
        return used

    @classmethod
    def _filter_sentences(
        cls, project: Project, singer_ids: Optional[Set[str]]
    ) -> List[tuple]:
        """按演唱者/角色过滤，并准备“主行 + 合并副轴”的导出计划。

        Returns:
            ``[(sentence, merged_aux_sentences), ...]``。主行若带同组内的
            副轴行，副轴放进第二项；父行不在本组的副轴行单独成行。
        """
        default_singer_id = (
            project.singers[0].id if project.singers else ""
        )
        # 按行索引而非 Sentence.id 建计划：旧 .sug / 拼接可能带重复 id，
        # 用 dict 会让后一句覆盖前一句并直接丢歌词。
        main_for_index: List[Optional[Sentence]] = [None] * len(
            project.sentences
        )
        main_indices_by_id: Dict[str, int] = {}
        aux_indices: Set[int] = set()
        for index, sentence in enumerate(project.sentences):
            if sentence.aux_role:
                if singer_ids is None or sentence.singer_id in singer_ids:
                    aux_indices.add(index)
                continue
            main: Optional[Sentence] = sentence
            if singer_ids is not None and sentence.characters:
                kept = [
                    char
                    for char in sentence.characters
                    if (char.singer_id or sentence.singer_id) in singer_ids
                ]
                if not kept:
                    continue
                singer_id = kept[0].singer_id or sentence.singer_id
                main = Sentence(
                    id=sentence.id,
                    singer_id=singer_id or default_singer_id,
                    characters=kept,
                    song_part=sentence.song_part,
                )
            main_for_index[index] = main
            main_indices_by_id.setdefault(sentence.id, index)

        merged_by_main: Dict[int, List[Sentence]] = {}
        standalone_aux: Dict[int, Sentence] = {}
        for index, sentence in enumerate(project.sentences):
            if not sentence.aux_role or index not in aux_indices:
                continue
            parent_index = main_indices_by_id.get(sentence.aux_parent_id)
            if parent_index is not None and main_for_index[parent_index] is not None:
                merged_by_main.setdefault(parent_index, []).append(sentence)
            else:
                standalone_aux[index] = sentence

        plan: List[tuple] = []
        for index, sentence in enumerate(project.sentences):
            if sentence.aux_role:
                if index in standalone_aux:
                    plan.append((standalone_aux[index], []))
                continue
            main = main_for_index[index]
            if main is None:
                continue
            merged = sorted(
                merged_by_main.get(index, []),
                key=lambda item: item.aux_order,
            )
            plan.append((main, merged))
        return plan

    @staticmethod
    def _language(project: Project) -> str:
        language = ""
        if project.metadata is not None:
            language = str(project.metadata.language or "").strip()
        return language or "ja"

    @staticmethod
    def _agent_map(
        project: Project, keep_singer_ids: Optional[Set[str]] = None
    ) -> Dict[str, str]:
        """singer.id → TTML agent id。

        优先复用源 TTML 的 ``xml:id``（保证 round-trip 不换 id/顺序），非法
        或冲突时回退 ``vN``。``keep_singer_ids`` 用于轴分组导出，只写组内
        实际出现的演唱者。
        """
        agent_map: Dict[str, str] = {}
        used: Set[str] = set()
        next_index = 1

        def allocate(singer_id: str, source_id: str = "") -> str:
            nonlocal next_index
            source_id = (source_id or "").strip()
            if source_id and _XML_ID_RE.match(source_id) and source_id not in used:
                agent_id = source_id
            else:
                while f"v{next_index}" in used:
                    next_index += 1
                agent_id = f"v{next_index}"
                next_index += 1
            used.add(agent_id)
            agent_map[singer_id] = agent_id
            return agent_id

        for singer in project.singers:
            if singer.ttml_role:
                # 翻译/音译/和声等辅助角色 Singer 不写 ttm:agent 表。
                continue
            if keep_singer_ids is not None and singer.id not in keep_singer_ids:
                continue
            allocate(singer.id, singer.ttml_agent_id)

        for sentence in project.sentences:
            if sentence.aux_role:
                continue
            for char in sentence.characters:
                singer_id = char.singer_id or sentence.singer_id
                if keep_singer_ids is not None and singer_id not in keep_singer_ids:
                    continue
                if singer_id and singer_id not in agent_map:
                    singer = next(
                        (s for s in project.singers if s.id == singer_id), None
                    )
                    if singer is not None and singer.ttml_role:
                        continue
                    allocate(
                        singer_id,
                        singer.ttml_agent_id if singer is not None else "",
                    )
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
            agent.set(
                "type",
                (singer.ttml_agent_type if singer is not None else "") or "person",
            )
            # 源 agent 没有 ttm:name 且用户没重命名时不要凭空写名字；
            # 否则重导入会把占位显示名固化成源文件里不存在的名字。
            if singer is None:
                name = singer_id
            elif (
                singer.ttml_agent_id
                and not singer.ttml_agent_named
                and (
                    not singer.ttml_agent_display_name
                    or singer.name == singer.ttml_agent_display_name
                )
            ):
                continue
            elif (
                singer.ttml_agent_id
                and singer.ttml_agent_named
                and singer.ttml_agent_display_name
                and singer.name == singer.ttml_agent_display_name
                and singer.ttml_agent_source_name
            ):
                # 同名 agent 的展示名可能带 id 后缀；未重命名时回写源 ttm:name。
                name = singer.ttml_agent_source_name
            else:
                name = singer.name or singer_id
            name_el = ET.SubElement(agent, _qname(TTM_NS, "name"))
            name_el.set("type", "full")
            name_el.text = name

        meta = project.metadata
        title = (meta.title if meta is not None else "") or ""
        artist = (meta.artist if meta is not None else "") or ""
        album = (meta.album if meta is not None else "") or ""
        extra = getattr(meta, "extra", None) if meta is not None else None
        source_music = _meta_values(extra, "musicName")
        source_artists = _meta_values(extra, "artists")
        source_albums = _meta_values(extra, "album")

        # 固定字段未被用户修改时按源文件多值顺序回写，保住多个 artists /
        # album 条目；修改过则只写当前项目值，避免源值覆盖用户编辑。
        if source_music and (not title or title == source_music[0]):
            for value in source_music:
                self._append_meta(metadata, "musicName", value)
        elif title:
            self._append_meta(metadata, "musicName", title)

        if artist:
            source_artist_joined = " / ".join(_dedupe(source_artists))
            if source_artists and artist == source_artist_joined:
                for value in source_artists:
                    self._append_meta(metadata, "artists", value)
            else:
                self._append_meta(metadata, "artists", artist)

        if album:
            if source_albums and album == source_albums[0]:
                for value in source_albums:
                    self._append_meta(metadata, "album", value)
            else:
                self._append_meta(metadata, "album", album)

        # AMLL 平台 ID / ISRC / 作者等附加元数据原样回写，便于联调时按
        # ncmMusicId 等匹配音源；固定字段不重复写。
        core_keys = {
            "musicName",
            "artists",
            "album",
            "sugGenerator",
            "_ttmlSourceMeta",
        }
        if isinstance(extra, dict):
            for key, value in extra.items():
                key = str(key)
                if not key or key in core_keys or key.startswith("_ttml"):
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

    def _append_aux_sentence(
        self,
        parent: ET.Element,
        sentence: Sentence,
        source_agent_map: Dict[str, str],
        aux_offset: int,
    ) -> None:
        """把一条副轴 Sentence 写回 x-* span（和声保留逐词子 span）。"""
        role = sentence.aux_role or "x-aux"
        span = ET.SubElement(parent, _qname(TTML_NS, "span"))
        span.set(_qname(TTM_NS, "role"), role)
        if sentence.aux_lang:
            span.set(_qname(XML_NS, "lang"), sentence.aux_lang)
        if sentence.aux_agent:
            mapped = source_agent_map.get(sentence.aux_agent)
            if mapped:
                span.set(_qname(TTM_NS, "agent"), mapped)
        begin = (
            max(0, sentence.aux_begin + aux_offset)
            if sentence.aux_begin is not None
            else None
        )
        end = (
            max(0, sentence.aux_end + aux_offset)
            if sentence.aux_end is not None
            else None
        )
        if "x-bg" in role.split():
            segments = _aux_segments(sentence)
            if len(segments) > 1:
                _set_time(span, begin, end)
                for text, seg_begin, seg_end in segments:
                    child = ET.SubElement(span, _qname(TTML_NS, "span"))
                    child.text = text
                    child_begin = (
                        max(0, seg_begin + aux_offset)
                        if seg_begin is not None
                        else None
                    )
                    child_end = (
                        max(0, seg_end + aux_offset)
                        if seg_end is not None
                        else None
                    )
                    _set_time(child, child_begin, child_end)
            else:
                _set_time(span, begin, end)
                span.text = sentence.text
            return
        _set_time(span, begin, end)
        span.text = sentence.text

    def _append_body(
        self,
        root: ET.Element,
        project: Project,
        plan: Sequence[tuple],
        agent_map: Dict[str, str],
        *,
        compensation_ms: int = 0,
    ) -> None:
        body = ET.SubElement(root, _qname(TTML_NS, "body"))

        all_sentences = [main for main, _aux in plan]
        all_sentences.extend(
            aux for _main, aux_list in plan for aux in aux_list
        )
        aux_offset = _project_time_offset(
            project, all_sentences, compensation_ms
        )

        entries: List[tuple] = []
        for sentence, merged_aux in plan:
            # 主行 p 的时间保持主文本自身范围；副轴 span 可以超出 p 的
            # begin/end（AMLL 允许），不要把主行释放时间撑到和声末尾，
            # 否则二次导入会把 p end 写回主行最后一个字造成漂移。
            start = _sentence_start(sentence, aux_offset)
            end = _sentence_end(sentence, aux_offset)
            entries.append((sentence, merged_aux, start, end))

        valid_starts = [
            value
            for value in (
                _sentence_start(sentence, aux_offset)
                for sentence in all_sentences
            )
            if value is not None
        ]
        valid_ends = [
            value
            for value in (
                _sentence_end(sentence, aux_offset)
                for sentence in all_sentences
            )
            if value is not None
        ]
        if valid_starts and valid_ends:
            body_start = min(valid_starts)
            body_end = _quantized_end(body_start, max(valid_ends))
            body.set("dur", _format_time(body_end))

        source_agent_map = {
            singer.ttml_agent_id: agent_map[singer.id]
            for singer in project.singers
            if singer.id in agent_map and singer.ttml_agent_id
        }

        # 按连续相同的 song_part 分组为 <div itunes:song-part="...">。
        groups: List[tuple] = []
        for entry in entries:
            part = entry[0].song_part
            if groups and groups[-1][0] == part:
                groups[-1][1].append(entry)
            else:
                groups.append((part, [entry]))

        line_no = 0
        for part, group_entries in groups:
            div = ET.SubElement(body, _qname(TTML_NS, "div"))
            if part:
                div.set(_qname(ITUNES_NS, "song-part"), part)
            group_starts: List[int] = []
            group_ends: List[int] = []
            for sentence, merged_aux, start, end in group_entries:
                if start is not None:
                    group_starts.append(start)
                if end is not None:
                    group_ends.append(end)
                for aux in merged_aux:
                    aux_start = _sentence_start(aux, aux_offset)
                    aux_end = _sentence_end(aux, aux_offset)
                    if aux_start is not None:
                        group_starts.append(aux_start)
                    if aux_end is not None:
                        group_ends.append(aux_end)
            if group_starts and group_ends:
                div_start = min(group_starts)
                div_end = max(group_ends)
                _set_time(div, div_start, div_end)

            for sentence, merged_aux, start, end in group_entries:
                line_no += 1
                p = ET.SubElement(div, _qname(TTML_NS, "p"))
                p.set(_qname(ITUNES_NS, "key"), f"L{line_no}")
                if sentence.aux_role:
                    # 父行不在本组：副轴行单独成 p（仍用 x-* span 标 role）。
                    _set_time(p, start, end)
                    self._append_aux_sentence(
                        p, sentence, source_agent_map, aux_offset
                    )
                    continue
                if not sentence.characters or not sentence.text:
                    if merged_aux:
                        _set_time(p, start, end)
                        p.set(_qname(TTM_NS, "role"), ROLE_SUG_EMPTY)
                        if sentence.singer_id in agent_map:
                            p.set(
                                _qname(TTM_NS, "agent"),
                                agent_map[sentence.singer_id],
                            )
                        for aux in merged_aux:
                            self._append_aux_sentence(
                                p, aux, source_agent_map, aux_offset
                            )
                        continue
                    # SUG 的空行是排版意图；TTML 本身没有空行语义，用扩展 role
                    # 保留，避免导出后行数变化。
                    if sentence.singer_id in agent_map:
                        p.set(
                            _qname(TTM_NS, "agent"),
                            agent_map[sentence.singer_id],
                        )
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
                for aux in merged_aux:
                    self._append_aux_sentence(
                        p, aux, source_agent_map, aux_offset
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
