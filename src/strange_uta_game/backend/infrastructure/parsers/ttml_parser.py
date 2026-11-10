"""TTML / AMLL TTML 歌词解析器（基础版）。

背景
====
TTML 是 W3C 的定时文本格式；AMLL TTML DB 在其上约定了一套逐词歌词写法
（``itunes:timing="Word"``、``<p ttm:agent=...>``、``<span begin end>``、
TTML2 ruby、``x-translation`` / ``x-roman`` / ``x-bg`` 辅助轨）。本模块只做
SUG 侧的第一层交换能力，不实现完整 TTML 渲染：

* 读取 ``<p>`` / ``<span>`` 的 begin/end（也支持 begin+dur）与逐词时间戳；
* 读取 TTML2 ruby（``container`` / ``base`` / ``textContainer`` / ``text``）；
* 读取 ``ttm:agent`` → 演唱者显示名；
* 读取 AMLL ``<amll:meta>`` 的歌曲名 / 艺人 / 专辑 / 语言；
* 忽略翻译、音译与背景人声辅助轨（``x-translation`` / ``x-roman`` /
  ``x-bg``），因为 SUG 当前的数据模型是一维逐行文本，无法无损承载
  重叠的背景人声；忽略项会记录在 :attr:`TTMLDocument.warnings`。

为支持 SUG 自身的「多 checkpoint / 句中停顿 / 连词 / 零节奏点」回环，
导出器会写入少量 ``ttm:role`` 扩展（``x-sug-checkpoint`` /
``x-sug-release`` / ``x-sug-link`` / ``x-sug-no-cp`` /
``x-sug-untimed`` / ``x-sug-empty``）。第三方 TTML 渲染器可以安全忽略
这些 role；本解析器读取它们后能还原 SUG 的完整时间轴结构。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from .lyric_parser import LyricParser, ParseError, ParsedLine

TTML_NS = "http://www.w3.org/ns/ttml"
TTM_NS = "http://www.w3.org/ns/ttml#metadata"
TTS_NS = "http://www.w3.org/ns/ttml#styling"
ITUNES_NS = "http://music.apple.com/lyric-ttml-internal"
AMLL_NS = "http://www.example.com/ns/amll"
XML_NS = "http://www.w3.org/XML/1998/namespace"

# SUG 扩展 role（导出器写入；第三方可忽略）
ROLE_SUG_CHECKPOINT = "x-sug-checkpoint"
ROLE_SUG_RELEASE = "x-sug-release"
ROLE_SUG_LINK = "x-sug-link"
ROLE_SUG_NO_CP = "x-sug-no-cp"
ROLE_SUG_UNTIMED = "x-sug-untimed"
ROLE_SUG_EMPTY = "x-sug-empty"

# 不进入 SUG 主文本的辅助轨 role
_AUX_ROLES = frozenset({"x-translation", "x-roman", "x-bg"})

_ROOT_TAG_RE = re.compile(r"<(?:[A-Za-z_][\w.-]*:)?tt(?:\s|>)")
_TIME_RE = re.compile(r"^\s*\d+(?:\.\d+)?s?\s*$", re.IGNORECASE)


@dataclass
class TTMLAgent:
    """TTML ``ttm:agent`` 的最小快照。"""

    id: str
    name: str = ""
    type: str = ""
    named: bool = False


@dataclass
class TTMLDocument:
    """一次 TTML 解析的完整结果。"""

    lines: List[ParsedLine] = field(default_factory=list)
    agents: Dict[str, TTMLAgent] = field(default_factory=dict)
    amll_meta: Dict[str, List[str]] = field(default_factory=dict)
    head_title: str = ""
    language: str = ""
    warnings: List[str] = field(default_factory=list)
    ignored_roles: Dict[str, int] = field(default_factory=dict)

    def meta_value(self, key: str, default: str = "") -> str:
        """返回某个 AMLL 元数据键的第一个非空值。"""
        for value in self.amll_meta.get(key, []):
            if value:
                return value
        return default

    @property
    def title(self) -> str:
        return self.meta_value("musicName") or self.head_title

    @property
    def artist(self) -> str:
        seen: List[str] = []
        for value in self.amll_meta.get("artists", []):
            value = (value or "").strip()
            if value and value not in seen:
                seen.append(value)
        return " / ".join(seen)

    @property
    def album(self) -> str:
        return self.meta_value("album")

    @property
    def has_ruby(self) -> bool:
        return any(line.ruby_map for line in self.lines)

    def extra_meta(self) -> Dict[str, object]:
        """返回未映射到 ProjectMetadata 固定字段的 AMLL meta。

        平台 ID / ISRC / 作者等键原样保留，供宿主联调和 .sug 持久化使用；
        单个值存字符串，多值（如多个 qqMusicId）存 list。
        """
        core = {"musicName", "artists", "album", "sugGenerator"}
        result: Dict[str, object] = {}
        for key, values in self.amll_meta.items():
            if key in core:
                continue
            clean = [value for value in values if value]
            if not clean:
                continue
            result[key] = clean[0] if len(clean) == 1 else clean
        return result


def _local(tag: str) -> str:
    """返回带命名空间 tag 的本地名。"""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _attr(elem: ET.Element, local_name: str, ns: Optional[str] = None) -> str:
    """按本地名读取属性；ns 给定时优先精确匹配该命名空间。"""
    if ns:
        value = elem.get(f"{{{ns}}}{local_name}")
        if value is not None:
            return value
    for key, value in elem.attrib.items():
        if _local(key) == local_name:
            return value
    return ""


def _roles(elem: ET.Element) -> Tuple[str, ...]:
    return tuple(part for part in _attr(elem, "role", TTM_NS).split() if part)


def _is_ruby_container(elem: ET.Element) -> bool:
    return _attr(elem, "ruby", TTS_NS) == "container"


def _ruby_kind(elem: ET.Element) -> str:
    return _attr(elem, "ruby", TTS_NS)


def parse_time(value: str) -> Optional[int]:
    """把 TTML 时间字符串转换为毫秒。

    支持规范中的 ``ss`` / ``ss.mmm``、``mm:ss`` / ``mm:ss.mmm``、
    ``hh:mm:ss`` / ``hh:mm:ss.mmm``。纯数字可超过 60（按秒解释）。
    非法值返回 ``None``，由调用方回退到父元素时间。
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) > 3:
        return None
    if not all(part.strip() for part in parts):
        return None
    if not _TIME_RE.match(parts[-1]):
        return None
    try:
        seconds_text = parts[-1].strip()
        if seconds_text.lower().endswith("s"):
            seconds_text = seconds_text[:-1]
        seconds = float(seconds_text)
        if len(parts) >= 2 and seconds >= 60:
            return None
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            if minutes >= 60:
                return None
            total = hours * 3600 + minutes * 60 + seconds
        elif len(parts) == 2:
            minutes = int(parts[0])
            total = minutes * 60 + seconds
        else:
            total = seconds
    except (TypeError, ValueError, OverflowError):
        return None
    if total < 0:
        return None
    try:
        return int(round(total * 1000))
    except (OverflowError, ValueError):
        return None


def _time_or(value: str, fallback: Optional[int]) -> Optional[int]:
    """解析时间；显式 0 不能被 ``or`` 当作缺失。"""
    parsed = parse_time(value)
    return fallback if parsed is None else parsed


def _end_time(elem: ET.Element, begin: Optional[int]) -> Optional[int]:
    """读取 end；缺失时按 TTML ``dur`` 计算（begin + dur）。"""
    end = parse_time(_attr(elem, "end"))
    if end is not None:
        return end
    dur = parse_time(_attr(elem, "dur"))
    if begin is not None and dur is not None:
        return begin + dur
    return None


def format_time(timestamp_ms: int) -> str:
    """按 ``HH:MM:SS.mmm`` 输出 TTML 时间。"""
    timestamp_ms = max(0, int(round(timestamp_ms)))
    hours = timestamp_ms // 3_600_000
    remaining = timestamp_ms % 3_600_000
    minutes = remaining // 60_000
    remaining %= 60_000
    seconds = remaining // 1000
    millis = remaining % 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


_XML_DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)


def is_ttml_content(content: str) -> bool:
    """快速判断文本是否像 TTML（不替代完整 XML 校验）。

    只接受“根元素就是 <tt>”的内容（允许前置 XML 声明/注释），避免把
    正文中出现 `<tt` 的普通文本误判成 TTML。
    """
    if not content:
        return False
    head = content.lstrip("\ufeff \t\r\n")[:8192]
    head = _XML_DECL_RE.sub("", head, count=1).lstrip()
    while head.startswith("<!--"):
        end = head.find("-->")
        if end < 0:
            return False
        head = head[end + 3 :].lstrip()
    return bool(_ROOT_TAG_RE.match(head))


@dataclass
class _Segment:
    """解析过程中的一个文本/时间片段。"""

    text: str = ""
    begin: Optional[int] = None
    end: Optional[int] = None
    ruby_parts: Optional[List[Tuple[str, Optional[int], Optional[int]]]] = None
    roles: Tuple[str, ...] = ()
    agent_id: str = ""
    # 来自 span 之间的 tail/隐式空白，不是独立的歌词字符。
    implicit: bool = False


def _clean_text(text: str) -> str:
    """去掉 XML 格式化换行，但保留词间空格与词内空格。"""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\n" in text:
        # 缩进换行是 XML 美化产生的，不是歌词内容；同一 span 内的换行同理。
        text = re.sub(r"[ \t]*\n[ \t]*", "", text)
    return text


def _plain_text(elem: ET.Element) -> str:
    """收集元素内可进入 SUG 主文本的纯文本。

    跳过翻译/音译/背景人声子树；ruby 容器只取 base 文本，不把读音混进正文。
    """
    parts: List[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        roles = _roles(child)
        if _AUX_ROLES.intersection(roles):
            pass
        elif _is_ruby_container(child):
            base = _ruby_base_text(child)
            if base:
                parts.append(base)
        else:
            parts.append(_plain_text(child))
        if child.tail:
            parts.append(child.tail)
    return _clean_text("".join(parts))


def _ruby_base_text(container: ET.Element) -> str:
    """取 ruby 容器中的 base 文本；缺失时回退到非 textContainer 内容。"""
    for node in container.iter():
        if _ruby_kind(node) == "base":
            return _plain_text(node)
    fallback: List[str] = []
    for child in container:
        if _ruby_kind(child) == "textContainer":
            continue
        fallback.append(_plain_text(child))
    return _clean_text("".join(fallback))


def _ruby_parts(
    container: ET.Element,
) -> List[Tuple[str, Optional[int], Optional[int]]]:
    """读取 ruby 的每个 text 段及其 begin/end。"""
    parts: List[Tuple[str, Optional[int], Optional[int]]] = []
    for node in container.iter():
        if _ruby_kind(node) != "text":
            continue
        text = _plain_text(node)
        if not text:
            continue
        begin = parse_time(_attr(node, "begin"))
        parts.append((text, begin, _end_time(node, begin)))
    return parts


def _distribute(
    begin: Optional[int], end: Optional[int], count: int
) -> List[Optional[int]]:
    """把一段 begin/end 均分到 count 个字符的起始时间。"""
    if count <= 0:
        return []
    if begin is None:
        return [None] * count
    if end is None or end <= begin or count == 1:
        return [begin] * count
    span = end - begin
    return [begin + int(round(span * i / count)) for i in range(count)]


class TTMLParser(LyricParser):
    """TTML / AMLL TTML 解析器。"""

    def __init__(self) -> None:
        self._last_document: Optional[TTMLDocument] = None

    # ── 公开接口 ──────────────────────────────────────────────

    def parse(self, content: str) -> List[ParsedLine]:
        return self.parse_document(content).lines

    def parse_document(self, content: str) -> TTMLDocument:
        self._last_document = None
        if content:
            content = content.lstrip("\ufeff")
        if not content or not content.strip():
            raise ParseError("TTML 内容为空")
        if "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper():
            raise ParseError("TTML 不允许 DOCTYPE / ENTITY 声明")

        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise ParseError(f"TTML XML 解析失败: {exc}") from exc

        if _local(root.tag) != "tt":
            raise ParseError("不是 TTML 文件：根元素不是 <tt>")

        document = TTMLDocument(language=_attr(root, "lang", XML_NS))
        self._read_head(root, document)
        self._resolve_anonymous_agents(document)
        self._count_aux_roles(root, document)

        for p in root.iter():
            if _local(p.tag) != "p":
                continue
            try:
                line = self._parse_paragraph(p, document)
            except RecursionError as exc:
                raise ParseError("TTML 嵌套层级过深，无法安全解析") from exc
            if line is not None:
                document.lines.append(line)

        # body 中才出现、head 未定义的 agent 也补一次占位名。
        self._resolve_anonymous_agents(document)

        if document.ignored_roles:
            summary = "、".join(
                f"{role}×{count}" for role, count in sorted(document.ignored_roles.items())
            )
            self._warn(
                document,
                f"已忽略 SUG 当前无法承载的辅助轨/角色：{summary}",
            )

        self._last_document = document
        return document

    def parse_metadata(self) -> Dict[str, object]:
        """返回最近一次解析的元数据（供导入层写回项目）。"""
        document = self._last_document
        if document is None:
            return {"format": "ttml"}
        meta: Dict[str, object] = {
            "format": "ttml",
            "title": document.title,
            "artist": document.artist,
            "album": document.album,
            "language": document.language,
        }
        extra = document.extra_meta()
        if extra:
            meta["extra"] = extra
        if document.has_ruby:
            meta["prompt_ruby_choice"] = "1"
        return {key: value for key, value in meta.items() if value}

    # ── head / metadata ───────────────────────────────────────

    @staticmethod
    def _warn(document: TTMLDocument, message: str) -> None:
        if message not in document.warnings:
            document.warnings.append(message)

    @staticmethod
    def _count_aux_roles(root: ET.Element, document: TTMLDocument) -> None:
        """全树统计辅助轨（含嵌套 x-bg / 内联 x-translation）。"""
        for elem in root.iter():
            for role in _AUX_ROLES.intersection(_roles(elem)):
                document.ignored_roles[role] = (
                    document.ignored_roles.get(role, 0) + 1
                )
            local = _local(elem.tag)
            if local in ("translation", "transliteration"):
                key = f"head-{local}"
                document.ignored_roles[key] = (
                    document.ignored_roles.get(key, 0) + 1
                )

    @staticmethod
    def _resolve_anonymous_agents(document: TTMLDocument) -> None:
        """给缺 ttm:name 的 agent 一个可读占位名，并提示用户重命名。"""
        unnamed = [agent for agent in document.agents.values() if not agent.named]
        if not unnamed:
            return
        artists: List[str] = []
        for value in document.amll_meta.get("artists", []):
            value = (value or "").strip()
            if value and value not in artists:
                artists.append(value)
        for index, agent in enumerate(unnamed):
            if artists:
                artist = artists[index] if index < len(artists) else artists[0]
                agent.name = (
                    artist
                    if len(unnamed) == 1 and len(artists) == 1
                    else f"{artist} ({agent.id})"
                )
            else:
                agent.name = agent.id
        TTMLParser._warn(
            document,
            "部分 ttm:agent 未提供 ttm:name，已用 "
            + "、".join(agent.name for agent in unnamed)
            + " 作为占位名；请在演唱者面板中重命名。",
        )

    @staticmethod
    def _ensure_agent(document: TTMLDocument, agent_id: str) -> None:
        """p/span 引用但 head 未定义的 agent 也要能落到 Singer。"""
        if agent_id and agent_id not in document.agents:
            artist = document.artist
            name = f"{artist} ({agent_id})" if artist else agent_id
            document.agents[agent_id] = TTMLAgent(
                id=agent_id, name=name, type="", named=True
            )
            TTMLParser._warn(
                document,
                f"ttm:agent {agent_id} 未在 head 定义，已按 {name} 处理。",
            )

    @staticmethod
    def _single_agent_id(raw: str, document: TTMLDocument) -> str:
        """ttm:agent 允许 IDREFS；SUG 一行只能一个主演唱者，取第一个并提示。"""
        ids = [part for part in (raw or "").split() if part]
        if not ids:
            return ""
        if len(ids) > 1:
            TTMLParser._warn(
                document,
                f"ttm:agent 多值（{' '.join(ids)}）暂只使用第一个 {ids[0]}。",
            )
        return ids[0]

    @staticmethod
    def _read_head(root: ET.Element, document: TTMLDocument) -> None:
        for elem in root.iter():
            local = _local(elem.tag)
            if local == "agent":
                agent_id = _attr(elem, "id", XML_NS) or _attr(elem, "id")
                if not agent_id:
                    continue
                name = ""
                for child in elem.iter():
                    if _local(child.tag) == "name":
                        name = _clean_text("".join(child.itertext())).strip()
                        if name:
                            break
                document.agents[agent_id] = TTMLAgent(
                    id=agent_id,
                    name=name or agent_id,
                    type=_attr(elem, "type"),
                    named=bool(name),
                )
            elif local == "meta":
                key = _attr(elem, "key")
                value = _attr(elem, "value")
                if key:
                    document.amll_meta.setdefault(key, [])
                    if value:
                        document.amll_meta[key].append(value)
            elif local == "title" and not document.head_title:
                text = _clean_text("".join(elem.itertext())).strip()
                if text:
                    document.head_title = text

    # ── body ──────────────────────────────────────────────────

    def _parse_paragraph(
        self, p: ET.Element, document: TTMLDocument
    ) -> Optional[ParsedLine]:
        p_begin = parse_time(_attr(p, "begin"))
        p_end = _end_time(p, p_begin)
        if ROLE_SUG_EMPTY in _roles(p):
            return ParsedLine(
                text="",
                timetags=[],
                line_end_ts=p_end,
                line_end_bind_last=True,
            )
        p_agent = self._single_agent_id(
            _attr(p, "agent", TTM_NS) or _attr(p, "agent"), document
        )
        if p_agent:
            self._ensure_agent(document, p_agent)
        line_singer_name = ""
        if p_agent:
            line_agent = document.agents.get(p_agent)
            line_singer_name = line_agent.name if line_agent else p_agent

        segments: List[_Segment] = []
        self._collect_children(
            p,
            p_begin,
            p_end,
            p_agent,
            segments,
            document,
        )

        # 空 p（纯间奏）不进入 SUG 行列表：TTML 的空 p 不是 SUG 的排版空行，
        # 强行生成空 Sentence 会把间奏显示成一行空白歌词。
        text = "".join(seg.text for seg in segments)
        if not text:
            return None

        chars: List[str] = []
        timetags: List[Tuple[int, int]] = []
        ruby_map: Dict[int, Tuple[List[str], int]] = {}
        extra_checkpoints: Dict[int, List[int]] = {}
        empty_checkpoint_counts: Dict[int, int] = {}
        gap_pause: Dict[int, int] = {}
        char_singer: Dict[int, str] = {}
        linked_map: Dict[int, bool] = {}
        no_cp_indices: set = set()

        for segment in segments:
            roles = set(segment.roles)
            if not segment.text:
                if chars:
                    last_idx = len(chars) - 1
                    is_checkpoint = ROLE_SUG_CHECKPOINT in roles
                    if is_checkpoint:
                        if segment.begin is not None:
                            extra_checkpoints.setdefault(last_idx, []).append(
                                segment.begin
                            )
                        else:
                            empty_checkpoint_counts[last_idx] = (
                                empty_checkpoint_counts.get(last_idx, 0) + 1
                            )
                    if ROLE_SUG_RELEASE in roles:
                        # 纯 release span 用 begin 表示释放时刻；
                        # checkpoint+release 组合用 end 表示释放时刻。
                        if is_checkpoint and segment.end is not None:
                            gap_pause[last_idx] = segment.end
                        elif segment.begin is not None:
                            gap_pause[last_idx] = segment.begin
                        elif segment.end is not None:
                            gap_pause[last_idx] = segment.end
                continue

            start_idx = len(chars)
            text_chars = list(segment.text)
            implicit_blank = bool(segment.implicit and not segment.text.strip())

            if segment.ruby_parts and len(text_chars) == 1:
                # 单字多读音段：第一段 begin 是字符时间戳，其余段是额外 checkpoint。
                first_begin = segment.ruby_parts[0][1]
                if first_begin is None:
                    first_begin = segment.begin
                times = [first_begin]
                if first_begin is not None:
                    for _, part_begin, _ in segment.ruby_parts[1:]:
                        if part_begin is not None:
                            extra_checkpoints.setdefault(start_idx, []).append(
                                part_begin
                            )
                ruby_map[start_idx] = (
                    [part[0] for part in segment.ruby_parts],
                    1,
                )
            elif segment.ruby_parts and len(segment.ruby_parts) == 1:
                # 多字共享一段读音：SUG 用 anchor + linked_to_next 表示。
                ruby_map[start_idx] = ([segment.ruby_parts[0][0]], len(text_chars))
                times = _distribute(segment.begin, segment.end, len(text_chars))
            else:
                times = _distribute(segment.begin, segment.end, len(text_chars))
                if segment.ruby_parts:
                    # 多字多段读音无法在 SUG 单字 ruby 中无损表达；
                    # 第一字挂完整读音，其余字仅保留时间戳。
                    ruby_map[start_idx] = (
                        [part[0] for part in segment.ruby_parts],
                        1,
                    )

            for offset, char in enumerate(text_chars):
                idx = start_idx + offset
                chars.append(char)
                if ROLE_SUG_NO_CP in roles or implicit_blank:
                    no_cp_indices.add(idx)
                if offset < len(times) and times[offset] is not None:
                    timetags.append((idx, times[offset]))
                if segment.agent_id:
                    agent = document.agents.get(segment.agent_id)
                    char_singer[idx] = (
                        agent.name if agent is not None else segment.agent_id
                    )

            last_idx = len(chars) - 1
            if ROLE_SUG_RELEASE in roles and segment.end is not None:
                gap_pause[last_idx] = segment.end
            if ROLE_SUG_LINK in roles:
                linked_map[last_idx] = True

        if not chars:
            return None

        if p_end is None:
            ends = [
                segment.end
                for segment in segments
                if segment.end is not None
            ]
            if ends:
                p_end = max(ends)

        # 非 ruby 的额外 checkpoint 由空 span 表达（有时间 = 已打轴，
        # 无时间 = 尚未打轴）；必须显式告诉 parse_to_sentences 节奏点数量，
        # 否则导入后 check_count 仍为 1，半成品状态会丢失。
        checkpoint_indices = set(extra_checkpoints) | set(empty_checkpoint_counts)
        check_count_map = {
            idx: 1
            + len(extra_checkpoints.get(idx, []))
            + empty_checkpoint_counts.get(idx, 0)
            for idx in checkpoint_indices
            if idx not in ruby_map
        }
        for idx in no_cp_indices:
            if idx not in checkpoint_indices:
                check_count_map[idx] = 0

        return ParsedLine(
            text="".join(chars),
            timetags=timetags,
            line_end_ts=p_end,
            line_end_bind_last=True,
            ruby_map=ruby_map,
            extra_checkpoints_map=extra_checkpoints,
            gap_pause_map=gap_pause,
            char_singer_map=char_singer,
            linked_map=linked_map,
            check_count_map=check_count_map,
            line_singer_name=line_singer_name,
        )

    def _collect_children(
        self,
        parent: ET.Element,
        inherited_begin: Optional[int],
        inherited_end: Optional[int],
        inherited_agent: str,
        out: List[_Segment],
        document: TTMLDocument,
    ) -> None:
        if parent.text:
            text = _clean_text(parent.text)
            if text:
                out.append(
                    _Segment(
                        text=text,
                        begin=inherited_begin,
                        end=inherited_end,
                        agent_id=inherited_agent,
                    )
                )

        for child in parent:
            local = _local(child.tag)
            roles = _roles(child)
            agent_id = self._single_agent_id(
                _attr(child, "agent", TTM_NS) or _attr(child, "agent"), document
            )
            if agent_id:
                self._ensure_agent(document, agent_id)
                inherited_agent_for_child = agent_id
            else:
                inherited_agent_for_child = inherited_agent

            aux_hits = _AUX_ROLES.intersection(roles)
            if aux_hits:
                # 全树统计已由 _count_aux_roles 完成，此处只跳过文本。
                pass
            elif _is_ruby_container(child):
                if ROLE_SUG_UNTIMED in roles:
                    begin = parse_time(_attr(child, "begin"))
                    end = _end_time(child, begin)
                else:
                    begin = _time_or(_attr(child, "begin"), inherited_begin)
                    end = _end_time(child, begin)
                    if end is None:
                        end = inherited_end
                parts = _ruby_parts(child)
                base = _ruby_base_text(child)
                if base:
                    part_begins = [part[1] for part in parts if part[1] is not None]
                    part_ends = [part[2] for part in parts if part[2] is not None]
                    if part_begins:
                        begin = min(part_begins)
                    if part_ends:
                        end = max(part_ends)
                    out.append(
                        _Segment(
                            text=base,
                            begin=begin,
                            end=end,
                            ruby_parts=parts or None,
                            roles=roles,
                            agent_id=inherited_agent_for_child,
                        )
                    )
            elif local == "br":
                out.append(
                    _Segment(
                        text=" ",
                        begin=None,
                        end=None,
                        agent_id=inherited_agent_for_child,
                        implicit=True,
                    )
                )
            else:
                # 叶子 span 没写 begin/end 时保持“未打轴”，不要继承 <p> 的
                # 行首时间：SUG 导出器会用无时间的 span 表示空格等占用字符。
                # 真正嵌套的 span 目前按整段文本处理，时间由外层/自身给出。
                begin = parse_time(_attr(child, "begin"))
                text = _plain_text(child)
                # 通用 TTML 的 line-level 写法：<p begin/end><span>text</span></p>
                # 叶子 span 没时间时应继承行时间；SUG 导出器用
                # x-sug-untimed 明确标记“这个字符本来就没有时间戳”，
                # 不能继承（否则空格/未打轴标点会被误加时间）。
                if (
                    begin is None
                    and ROLE_SUG_UNTIMED not in roles
                    and text.strip()
                ):
                    begin = inherited_begin
                end = _end_time(child, begin)
                if end is None and ROLE_SUG_UNTIMED not in roles:
                    end = inherited_end
                out.append(
                    _Segment(
                        text=text,
                        begin=begin,
                        end=end,
                        roles=roles,
                        agent_id=inherited_agent_for_child,
                    )
                )

            if child.tail:
                tail = _clean_text(child.tail)
                if tail and (tail.strip() or " " in tail):
                    out.append(
                        _Segment(
                            text=tail,
                            begin=None,
                            end=None,
                            agent_id=inherited_agent_for_child,
                            implicit=True,
                        )
                    )


def parse_ttml_document(content: str) -> TTMLDocument:
    """便捷函数：解析 TTML 内容并返回 :class:`TTMLDocument`。"""
    return TTMLParser().parse_document(content)
