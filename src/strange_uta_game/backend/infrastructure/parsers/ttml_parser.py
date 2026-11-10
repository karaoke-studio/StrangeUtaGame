"""TTML / AMLL TTML 歌词解析器。

背景
====
TTML 是 W3C 的定时文本格式；AMLL TTML DB 在其上约定了一套逐词歌词写法
（``itunes:timing="Word"``、``<p ttm:agent=...>``、``<span begin end>``、
TTML2 ruby、``x-translation`` / ``x-roman`` / ``x-bg`` 辅助轨）。本模块做
SUG 侧的第一层交换能力，不实现完整 TTML 渲染：

* 读取 ``<p>`` / ``<span>`` 的 begin/end（也支持 begin+dur）与逐词时间戳；
* 读取 TTML2 ruby（``container`` / ``base`` / ``textContainer`` / ``text``）；
* 读取 ``ttm:agent`` → 演唱者显示名，并保留源 ``xml:id`` / ``type`` /
  是否原本带 ``ttm:name``（导出时原样回写，未命名 agent 不凭空加名字）；
* 读取 AMLL ``<amll:meta>`` 的歌曲名 / 艺人 / 专辑 / 语言；
* 把翻译、音译、背景人声等辅助轨转换成**独立的 Sentence 行**，分配给
  对应角色 Singer（翻译/音译/和声）；``aux_parent_id`` 指向主行，
  ``aux_role`` 保留源 role。时间戳允许与主行重叠/逆序。

为支持 SUG 自身的「多 checkpoint / 句中停顿 / 连词 / 零节奏点」回环，
导出器会写入少量 ``ttm:role`` 扩展（``x-sug-checkpoint`` /
``x-sug-release`` / ``x-sug-link`` / ``x-sug-no-cp`` /
``x-sug-untimed`` / ``x-sug-empty``）。第三方 TTML 渲染器可以安全忽略
这些 role；本解析器读取它们后能还原 SUG 的完整时间轴结构。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from strange_uta_game.backend.domain import AuxTrack, Character, Sentence

from .lyric_parser import LyricParser, ParseError, ParsedLine, parse_to_sentences


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

# 不进入 SUG 主文本的辅助轨 role（AMLL 约定 + 通用 x-* role）
_AUX_ROLES = frozenset({"x-translation", "x-roman", "x-bg"})
# SUG 私有 role 必须留在主文本解析路径里，不能当作第三方辅助轨。
_SUG_ROLES = frozenset(
    {
        ROLE_SUG_CHECKPOINT,
        ROLE_SUG_RELEASE,
        ROLE_SUG_LINK,
        ROLE_SUG_NO_CP,
        ROLE_SUG_UNTIMED,
        ROLE_SUG_EMPTY,
    }
)

_AUX_ROLE_LABELS = {
    "x-translation": "翻译",
    "x-roman": "音译",
    "x-bg": "和声",
}


def _primary_aux_role(track: AuxTrack) -> str:
    """取辅助轨的主要 role（多 role 时按已知 role 优先）。"""
    roles = [role for role in track.roles if role not in _SUG_ROLES]
    for preferred in ("x-translation", "x-roman", "x-bg"):
        if preferred in roles:
            return preferred
    return roles[0] if roles else "x-aux"


def _aux_role_label(role: str) -> str:
    return _AUX_ROLE_LABELS.get(role, f"辅助轨 {role}")


def aux_singer_key(track: AuxTrack) -> Tuple[str, str]:
    """辅助角色 Singer 的归并键：role + 语言（和声不按语言拆分）。"""
    role = _primary_aux_role(track)
    lang = "" if role == "x-bg" else (track.lang or "")
    return role, lang


def _split_nested_aux(
    track: AuxTrack,
) -> Tuple[Optional[AuxTrack], List[AuxTrack]]:
    """拆出 track 自身与嵌套在它里面的其他辅助 role 子轨。"""
    nested: List[AuxTrack] = []

    def clean(node: AuxTrack) -> AuxTrack:
        children: List[AuxTrack] = []
        for child in node.children:
            if child.roles and child.roles[0] not in _SUG_ROLES:
                nested.append(child)
            else:
                children.append(clean(child))
        return AuxTrack(
            role=node.role,
            text=node.text,
            lang=node.lang,
            begin=node.begin,
            end=node.end,
            agent=node.agent,
            children=children,
        )

    cleaned = clean(track)
    if not cleaned.role and not cleaned.text and not cleaned.children:
        cleaned = None
    return cleaned, nested


def _flatten_aux_tracks(tracks: List[AuxTrack]) -> List[AuxTrack]:
    """把嵌套在辅助轨里的其他辅助 role 摊平成独立轨，便于建 Sentence。"""
    result: List[AuxTrack] = []
    for track in tracks or []:
        cleaned, nested = _split_nested_aux(track)
        if cleaned is not None:
            result.append(cleaned)
        result.extend(_flatten_aux_tracks(nested))
    return result


def build_aux_singer_specs(
    document: "TTMLDocument",
) -> Tuple[List[Tuple[str, str]], Dict[Tuple[str, str], str]]:
    """为 TTML 辅助轨生成 Singer 规格。

    Returns:
        (specs, name_by_key)：
        - specs: ``[(singer_name, aux_role), ...]``，按首次出现顺序；
        - name_by_key: ``{(role, lang): singer_name}``，供建 Sentence 时查名。
    """
    used = {agent.name for agent in document.agents.values() if agent.name}
    specs: List[Tuple[str, str]] = []
    name_by_key: Dict[Tuple[str, str], str] = {}

    def add(track: AuxTrack) -> None:
        key = aux_singer_key(track)
        if key in name_by_key:
            return
        role, lang = key
        base = _aux_role_label(role)
        if lang and role in ("x-translation", "x-roman"):
            base = f"{base} ({lang})"
        name = base
        suffix = 2
        while name in used:
            name = f"{base} {suffix}"
            suffix += 1
        used.add(name)
        name_by_key[key] = name
        specs.append((name, role))

    for line in document.lines:
        for track in _flatten_aux_tracks(line.aux_tracks):
            add(track)
    return specs, name_by_key


_ROOT_TAG_RE = re.compile(r"<(?:[A-Za-z_][\w.-]*:)?tt(?:\s|>)")
_TIME_RE = re.compile(r"^\s*\d+(?:\.\d+)?s?\s*$", re.IGNORECASE)


@dataclass
class TTMLAgent:
    """TTML ``ttm:agent`` 的最小快照。

    ``name`` 是展示名（无名 agent 会用 artists/id 生成占位名；同名 agent
    会加 id 后缀以便 UI 区分），``source_name`` 才是源 ``ttm:name`` 原文。
    导出时优先回写 ``source_name``，只有用户改过名才写 Singer.name。
    """

    id: str
    name: str = ""
    type: str = ""
    named: bool = False
    name_type: str = ""
    name_lang: str = ""
    source_name: str = ""


@dataclass
class TTMLDocument:
    """一次 TTML 解析的完整结果。"""

    lines: List[ParsedLine] = field(default_factory=list)
    agents: Dict[str, TTMLAgent] = field(default_factory=dict)
    amll_meta: Dict[str, List[str]] = field(default_factory=dict)
    head_title: str = ""
    language: str = ""
    warnings: List[str] = field(default_factory=list)
    # 兼容字段：历史上记录被忽略的辅助轨。现在辅助轨会完整保留，不再使用；
    # 保留字段避免宿主/旧测试属性访问报错。
    ignored_roles: Dict[str, int] = field(default_factory=dict)
    # 全树辅助轨 role 计数（诊断用，不影响导入内容）。
    aux_role_counts: Dict[str, int] = field(default_factory=dict)
    # 当前 SUG 无法承载/编辑、导出会丢弃的内容（按人类可读类别去重）。
    dropped_features: List[str] = field(default_factory=list)
    # 源文件主导的时间书写风格（用于导出时尽量保持原格式）。
    time_style: Dict[str, object] = field(default_factory=dict)

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

    @property
    def has_aux_tracks(self) -> bool:
        return any(line.aux_tracks for line in self.lines)

    def agent_for_name(self, name: str) -> Optional[TTMLAgent]:
        """按显示名查找 agent（导入层建 Singer 时用）。"""
        for agent in self.agents.values():
            if agent.name == name:
                return agent
        return None

    def extra_meta(self) -> Dict[str, object]:
        """返回随项目持久化的 AMLL meta 快照。

        平台 ID / ISRC / 作者等键原样保留（单值字符串，多值 list）。固定
        字段（musicName / artists / album）通常已映射到 ProjectMetadata；
        只有源文件给了**多个不同值**时才额外保留到保留键
        ``_ttmlSourceMeta``，供导出器在用户未改字段时按原多值回写。
        """
        core = {"musicName", "artists", "album", "sugGenerator"}
        result: Dict[str, object] = {}
        duplicate_core: Dict[str, List[str]] = {}
        for key, values in self.amll_meta.items():
            clean: List[str] = []
            for value in values:
                if value and value not in clean:
                    clean.append(value)
            if not clean:
                continue
            if key in core:
                if key != "sugGenerator" and len(clean) > 1:
                    duplicate_core[key] = clean
                continue
            result[key] = clean[0] if len(clean) == 1 else clean
        if duplicate_core:
            result["_ttmlSourceMeta"] = duplicate_core
        if self.time_style:
            result["_ttmlTimeStyle"] = dict(self.time_style)
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


def _song_part(elem: ET.Element) -> str:
    """读取 ``itunes:songPart`` / ``itunes:song-part``（大小写不敏感）。"""
    for key, value in elem.attrib.items():
        local = _local(key).replace("_", "-").lower()
        if local in ("songpart", "song-part") and value:
            return str(value).strip()
    return ""


def _is_aux_element(elem: ET.Element) -> bool:
    """是否是需要挂到辅助轨、而不是主文本的 span。

    兼容 AMLL 的 ``x-translation`` / ``x-roman`` / ``x-bg``，也保留未知
    ``x-*`` role；SUG 私有 ``x-sug-*`` role 属于主文本结构，必须排除。
    """
    roles = _roles(elem)
    if not roles or _SUG_ROLES.intersection(roles):
        return False
    return any(role.startswith("x-") for role in roles)


def _aux_track_from_element(elem: ET.Element) -> AuxTrack:
    """把一个带辅助 role 的元素递归转换为 :class:`AuxTrack`。

    子 span 之间的 tail 文本（例如和声词之间的空格）会作为一个无 role
    的结构子节点插在对应 span 之后，保证 ``Sentence.text`` 与源顺序一致。
    """
    begin = parse_time(_attr(elem, "begin"))
    end = _end_time(elem, begin)
    agent = _attr(elem, "agent", TTM_NS) or _attr(elem, "agent")
    children: List[AuxTrack] = []

    def append_tail(node: ET.Element) -> None:
        tail = _clean_text(node.tail or "")
        if tail:
            children.append(AuxTrack(text=tail))

    for child in elem:
        if _local(child.tag) == "br":
            children.append(AuxTrack(text=" "))
            append_tail(child)
            continue
        if _local(child.tag) not in ("span", "p", "div"):
            # TTML 里辅助轨一般只含 span；未知元素也保留其文本，避免丢词。
            text = _clean_text("".join(child.itertext()))
            if text:
                children.append(AuxTrack(text=text))
            append_tail(child)
            continue
        child_track = _aux_track_from_element(child)
        if (
            child_track.text
            or child_track.children
            or child_track.begin is not None
            or child_track.role
        ):
            children.append(child_track)
        append_tail(child)
    return AuxTrack(
        role=" ".join(_roles(elem)),
        text=_clean_text(elem.text or ""),
        lang=_attr(elem, "lang", XML_NS),
        begin=begin,
        end=end,
        agent=agent,
        children=children,
    )


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


def detect_time_style(values: List[str]) -> Dict[str, object]:
    """从源文件时间字符串推断主导书写风格。

    只求“大多数时间长这样”，用于导出时尽量保持源格式；同一文件混用
    ``3:14.734`` 与 ``00:03:14.734`` 时无法逐值还原，仍会归一化到多数派。
    """
    kind_counts: Counter = Counter()
    precision_counts: Counter = Counter()
    pad_counts: Counter = Counter()
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        lower = text.lower()
        if lower.endswith("s"):
            kind = "seconds_suffix"
            body = text[:-1]
        elif text.count(":") == 2:
            kind = "hms"
            body = text
        elif text.count(":") == 1:
            kind = "minutes"
            body = text
        else:
            kind = "seconds"
            body = text
        kind_counts[kind] += 1
        if "." in body:
            frac = body.rsplit(".", 1)[1]
            precision_counts[len(frac)] += 1
        else:
            precision_counts[0] += 1
        first = body.split(":", 1)[0]
        pad_counts[bool(re.fullmatch(r"\d{2,}", first))] += 1
    if not kind_counts:
        return {}
    kind = kind_counts.most_common(1)[0][0]
    precision = precision_counts.most_common(1)[0][0]
    pad_first = pad_counts.most_common(1)[0][0]
    return {
        "kind": kind,
        "precision": int(precision),
        "pad_first": bool(pad_first),
    }


def format_time_style(timestamp_ms: int, style: Optional[Dict[str, object]]) -> str:
    """按 ``detect_time_style`` 推断的风格格式化时间。"""
    if not style:
        return format_time(timestamp_ms)
    timestamp_ms = max(0, int(round(timestamp_ms)))
    kind = str(style.get("kind") or "hms")
    precision = max(0, min(3, int(style.get("precision", 3) or 0)))
    pad_first = bool(style.get("pad_first", True))

    if precision == 0:
        frac = ""
    else:
        frac = f".{timestamp_ms % 1000:03d}"[: precision + 1]

    if kind == "seconds_suffix":
        return f"{timestamp_ms / 1000:.{precision}f}s"
    if kind == "seconds":
        total_seconds = timestamp_ms // 1000
        return f"{total_seconds}{frac}"
    if kind == "minutes":
        total_minutes, rem = divmod(timestamp_ms, 60_000)
        seconds, millis = divmod(rem, 1000)
        frac = "" if precision == 0 else f".{millis:03d}"[: precision + 1]
        first = f"{total_minutes:02d}" if pad_first else str(total_minutes)
        return f"{first}:{seconds:02d}{frac}"
    hours, rem = divmod(timestamp_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    frac = "" if precision == 0 else f".{millis:03d}"[: precision + 1]
    first = f"{hours:02d}" if pad_first else str(hours)
    return f"{first}:{minutes:02d}:{seconds:02d}{frac}"


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
        if _is_aux_element(child):
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
        time_values: List[str] = []
        for elem in root.iter():
            for key, value in elem.attrib.items():
                if _local(key) in ("begin", "end", "dur"):
                    time_values.append(value)
        document.time_style = detect_time_style(time_values)
        self._read_head(root, document)
        self._resolve_anonymous_agents(document)
        self._dedupe_agent_names(document)
        self._count_aux_roles(root, document)
        self._collect_dropped_features(root, document)

        def walk(elem: ET.Element, song_part: str = "") -> None:
            local = _local(elem.tag)
            if local == "div":
                song_part = _song_part(elem) or song_part
            if local == "p":
                try:
                    line = self._parse_paragraph(
                        elem, document, song_part=song_part
                    )
                except RecursionError as exc:
                    raise ParseError(
                        "TTML 嵌套层级过深，无法安全解析"
                    ) from exc
                if line is not None:
                    document.lines.append(line)
                return
            for child in elem:
                walk(child, song_part)

        walk(root)

        # body 中才出现、head 未定义的 agent 也补一次占位名。
        self._resolve_anonymous_agents(document)
        self._dedupe_agent_names(document)

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
        if document.dropped_features:
            meta["dropped_features"] = list(document.dropped_features)
        return {key: value for key, value in meta.items() if value}

    # ── head / metadata ───────────────────────────────────────

    @staticmethod
    def _warn(document: TTMLDocument, message: str) -> None:
        if message not in document.warnings:
            document.warnings.append(message)

    @staticmethod
    def _collect_dropped_features(
        root: ET.Element, document: TTMLDocument
    ) -> None:
        """记录 SUG 当前无对应编辑能力、导出会丢弃的 TTML 内容。"""
        labels: List[str] = []

        def note(label: str) -> None:
            if label not in labels:
                labels.append(label)

        for elem in root.iter():
            local = _local(elem.tag).lower()
            for key in elem.attrib:
                namespace = key[1:].split("}", 1)[0] if key.startswith("{") else ""
                attr = _local(key)
                if namespace == TTS_NS and attr != "ruby":
                    if elem is root and attr == "extent":
                        continue
                    note("字体、颜色、位置、图片等 tts 样式")
            if local == "itunesmetadata":
                note("Apple Music 头部元数据（songwriters / 翻译 / 音译 sidecar 等）")
            elif local in ("translations", "transliterations"):
                note("Apple Music 头部翻译 / 音译 sidecar")
            elif local in ("styling", "style"):
                note("TTML 样式表（styling / style）")
            elif local in ("layout", "region"):
                note("TTML 布局 / 区域（layout / region）")
            elif local in ("image", "audio", "font"):
                note("TTML 外部资源（图片 / 音频 / 字体）")
            elif local == "meta":
                key_name = _attr(elem, "key")
                if key_name in ("obscene", "empty-beat"):
                    note(f"AMLL 元数据 {key_name}")
            elif local in ("copyright", "desc", "set", "metadata-item"):
                note("其他 ttm 元数据（版权 / 描述等）")
            if _is_aux_element(elem) and any(
                _is_aux_element(child)
                for child in elem.iter()
                if child is not elem
            ):
                note(
                    "嵌套在辅助轨内的其他辅助 role（导入后会提升为主行同级 span）"
                )
        document.dropped_features = labels

    @staticmethod
    def _count_aux_roles(root: ET.Element, document: TTMLDocument) -> None:
        """全树统计辅助轨（含嵌套 x-bg / 内联 x-translation）。

        辅助轨现在会完整保留，这里只做诊断计数；``ignored_roles`` 保持
        为空是为了兼容仍读取该字段的旧宿主。
        """
        for elem in root.iter():
            if not _is_aux_element(elem):
                continue
            for role in _roles(elem):
                document.aux_role_counts[role] = (
                    document.aux_role_counts.get(role, 0) + 1
                )

    @staticmethod
    def _resolve_anonymous_agents(document: TTMLDocument) -> None:
        """给缺 ttm:name 的 agent 一个可读显示名（不写入导出）。

        源文件里无名的 agent 依然保持 ``named=False``；导出器据此省略
        ``ttm:name``，避免 round-trip 时凭空生成名字。显示名只用于 SUG
        演唱者面板，优先取 ``amll:meta`` 的 artists，否则用 agent id。
        """
        unnamed = [agent for agent in document.agents.values() if not agent.named]
        if not unnamed:
            return
        artists: List[str] = []
        for value in document.amll_meta.get("artists", []):
            value = (value or "").strip()
            if value and value not in artists:
                artists.append(value)
        for index, agent in enumerate(unnamed):
            # 已在 p/span 现场生成过可读名（_ensure_agent）的 agent 不再覆盖，
            # 否则早先解析的行按旧名映射会落到默认演唱者。
            if agent.name and agent.name != agent.id:
                continue
            if artists:
                artist = artists[index] if index < len(artists) else artists[0]
                agent.name = (
                    artist
                    if len(unnamed) == 1 and len(artists) == 1
                    else f"{artist} ({agent.id})"
                )
            else:
                agent.name = agent.id

    @staticmethod
    def _dedupe_agent_names(document: TTMLDocument) -> None:
        """确保每个 agent 的展示名唯一（源文件允许同名 agent）。

        AMLL 里有 ``type=person`` 与 ``type=group`` 同名的场景（如个人
        张靓颖 + 组合张靓颖）。SUG 导入按显示名建 Singer，若不去重会合并
        成同一位演唱者，导出的 agent 数也会变少。这里只改展示名，源
        ``ttm:name`` 仍保存在 ``source_name``，导出时原样回写。
        """
        used: Dict[str, int] = {}
        for agent in document.agents.values():
            base = agent.name or agent.id
            if base not in used:
                used[base] = 1
                agent.name = base
                continue
            suffix = f"{base} ({agent.id})"
            candidate = suffix
            index = 2
            while candidate in used:
                candidate = f"{suffix} #{index}"
                index += 1
            used[base] = used.get(base, 1) + 1
            used[candidate] = 1
            agent.name = candidate

    @staticmethod
    def _ensure_agent(document: TTMLDocument, agent_id: str) -> None:
        """p/span 引用但 head 未定义的 agent 也要能落到 Singer。

        不把它当作“有名字”的 agent：导出时仍按未命名 agent 处理，尽量
        接近源文件（虽然 TTML 规范通常要求先声明）。
        """
        if agent_id and agent_id not in document.agents:
            artist = document.artist
            name = f"{artist} ({agent_id})" if artist else agent_id
            document.agents[agent_id] = TTMLAgent(
                id=agent_id,
                name=name,
                type="",
                named=False,
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
                name_type = ""
                name_lang = ""
                for child in elem.iter():
                    if _local(child.tag) == "name":
                        name = _clean_text("".join(child.itertext())).strip()
                        if name:
                            name_type = _attr(child, "type")
                            name_lang = _attr(child, "lang", XML_NS)
                            break
                document.agents[agent_id] = TTMLAgent(
                    id=agent_id,
                    name=name or agent_id,
                    type=_attr(elem, "type"),
                    named=bool(name),
                    name_type=name_type,
                    name_lang=name_lang,
                    source_name=name,
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
        self, p: ET.Element, document: TTMLDocument, *, song_part: str = ""
    ) -> Optional[ParsedLine]:
        p_begin = parse_time(_attr(p, "begin"))
        p_end = _end_time(p, p_begin)
        if ROLE_SUG_EMPTY in _roles(p):
            empty_agent = self._single_agent_id(
                _attr(p, "agent", TTM_NS) or _attr(p, "agent"), document
            )
            empty_name = ""
            if empty_agent:
                self._ensure_agent(document, empty_agent)
                empty_name = document.agents[empty_agent].name
            empty_aux = self._collect_aux_tracks(p)
            for track in empty_aux:
                if track.begin is None:
                    track.begin = p_begin
                if track.end is None:
                    track.end = p_end
                if not track.agent:
                    track.agent = empty_agent
            return ParsedLine(
                text="",
                timetags=[],
                line_end_ts=p_end,
                line_end_bind_last=True,
                aux_tracks=empty_aux,
                line_singer_name=empty_name,
                song_part=song_part,
                keep_empty_main=bool(empty_aux),
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

        # 辅助轨（翻译 / 音译 / 背景人声）先摘出来；主文本收集会跳过它们。
        aux_tracks = self._collect_aux_tracks(p)

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
        # 强行生成空 Sentence 会把间奏显示成一行空白歌词。只有携带辅助轨的
        # 空 p 才保留，否则翻译/背景轨会随空行一起丢失。
        text = "".join(seg.text for seg in segments)
        if not text:
            if not aux_tracks:
                return None
            for track in aux_tracks:
                if track.begin is None:
                    track.begin = p_begin
                if track.end is None:
                    track.end = p_end
                if not track.agent:
                    track.agent = p_agent
            return ParsedLine(
                text="",
                timetags=[],
                line_end_ts=p_end,
                line_end_bind_last=True,
                aux_tracks=aux_tracks,
                line_singer_name=line_singer_name,
                song_part=song_part,
            )

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
            aux_tracks=aux_tracks,
            song_part=song_part,
        )

    def _collect_aux_tracks(self, p: ET.Element) -> List[AuxTrack]:
        """收集段落内所有最外层辅助轨（递归查找包裹 span 中的 aux）。"""
        tracks: List[AuxTrack] = []

        def walk(parent: ET.Element) -> None:
            for child in parent:
                if _is_aux_element(child):
                    track = _aux_track_from_element(child)
                    if (
                        track.text
                        or track.children
                        or track.begin is not None
                        or track.role
                    ):
                        tracks.append(track)
                    # 辅助轨内部由 _aux_track_from_element 处理，不再重复收集。
                    continue
                if _is_ruby_container(child):
                    continue
                walk(child)

        walk(p)
        return tracks

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

            aux_hits = _is_aux_element(child)
            if aux_hits:
                # 辅助轨由 _collect_aux_tracks 统一保留，这里只跳过主文本。
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
                            agent_id=inherited_agent,
                            implicit=True,
                        )
                    )


def _aux_leaf_segments(track: AuxTrack) -> List[AuxTrack]:
    """把背景人声子 span 展平成带时间轴的叶子段（不递归嵌套 aux role）。"""
    if not track.children:
        return [track] if track.text else []
    segments: List[AuxTrack] = []
    if track.text:
        segments.append(
            AuxTrack(
                text=track.text,
                begin=track.begin,
                end=track.end,
                agent=track.agent,
                lang=track.lang,
            )
        )
    for child in track.children:
        segments.extend(_aux_leaf_segments(child))
    return segments


def _aux_sentence_from_track(
    track: AuxTrack,
    *,
    singer_id: str,
    parent_id: str,
    order: int,
) -> Optional[Sentence]:
    """把一个辅助轨转换成独立 Sentence（翻译/音译为文本行，和声带时间戳）。"""
    primary = _primary_aux_role(track)
    role = " ".join(track.roles) or "x-aux"
    common = {
        "singer_id": singer_id,
        "aux_role": role,
        "aux_parent_id": parent_id,
        "aux_lang": track.lang,
        "aux_agent": track.agent,
        "aux_order": order,
    }
    if primary == "x-bg":
        characters: List[Character] = []
        begins: List[int] = []
        ends: List[int] = []
        if track.begin is not None:
            begins.append(int(track.begin))
        if track.end is not None:
            ends.append(int(track.end))
        segments = _aux_leaf_segments(track)
        previous_end: Optional[int] = None
        for seg_index, segment in enumerate(segments):
            text = segment.text or segment.plain_text
            if not text:
                continue
            seg_begin = segment.begin
            if seg_begin is None:
                seg_begin = previous_end if previous_end is not None else track.begin
            seg_end = segment.end
            if seg_end is None:
                for following in segments[seg_index + 1 :]:
                    if following.begin is not None:
                        seg_end = following.begin
                        break
                if seg_end is None:
                    seg_end = track.end
            if seg_begin is not None:
                begins.append(int(seg_begin))
            if seg_end is not None:
                ends.append(int(seg_end))
                previous_end = int(seg_end)
            times = _distribute(seg_begin, seg_end, len(text))
            for char_index, char_text in enumerate(text):
                is_segment_end = char_index == len(text) - 1
                timestamp = times[char_index]
                char = Character(
                    char=char_text,
                    check_count=1,
                    singer_id=singer_id,
                    is_line_end=(
                        seg_index == len(segments) - 1 and is_segment_end
                    ),
                    is_sentence_end=(
                        is_segment_end and seg_end is not None
                    ),
                    sentence_end_ts=seg_end if is_segment_end else None,
                )
                if timestamp is not None:
                    char.add_timestamp(timestamp)
                characters.append(char)
        if not characters:
            return None
        sentence = Sentence(
            characters=characters,
            aux_begin=min(begins) if begins else None,
            aux_end=max(ends) if ends else None,
            **common,
        )
        for char in sentence.characters:
            char.set_offset(0)
        return sentence

    text = track.plain_text
    if not text:
        return None
    sentence = Sentence.from_text(text, singer_id)
    sentence.aux_role = role
    sentence.aux_parent_id = parent_id
    sentence.aux_lang = track.lang
    sentence.aux_agent = track.agent
    sentence.aux_order = order
    sentence.aux_begin = track.begin
    sentence.aux_end = track.end
    for char in sentence.characters:
        char.singer_id = singer_id
        char.set_offset(0)
    return sentence


def build_ttml_sentences(
    document: TTMLDocument,
    default_singer_id: str,
    singer_name_to_id: Optional[Dict[str, str]] = None,
    *,
    aux_name_by_key: Optional[Dict[Tuple[str, str], str]] = None,
) -> List[Sentence]:
    """把 TTMLDocument 转成 SUG Sentence 列表（含辅助角色独立行）。

    - 主歌词行由 :func:`parse_to_sentences` 生成；
    - 翻译/音译/和声等辅助轨转成紧跟主行的独立 Sentence，分配对应角色
      Singer；时间戳允许与主行重叠/逆序；
    - 每条辅助行保留 ``aux_parent_id``，导出时据此合并回主行的 x-* span。
    """
    name_to_id = singer_name_to_id or {}
    if aux_name_by_key is None:
        _specs, aux_name_by_key = build_aux_singer_specs(document)
    main_sentences = parse_to_sentences(
        document.lines,
        default_singer_id,
        singer_name_to_id=name_to_id,
    )
    result: List[Sentence] = []
    for line, main_sentence in zip(document.lines, main_sentences):
        # 纯辅助 p（没有主文本，例如单独承载翻译）不要造一条空主行；
        # 否则导出为 aux-only p 后重导入会多出一行空白歌词。
        parent_id = ""
        if line.text or not line.aux_tracks or line.keep_empty_main:
            result.append(main_sentence)
            parent_id = main_sentence.id
        order = 0
        for track in _flatten_aux_tracks(line.aux_tracks):
            singer_name = aux_name_by_key.get(aux_singer_key(track), "")
            singer_id = name_to_id.get(singer_name, default_singer_id)
            aux_sentence = _aux_sentence_from_track(
                track,
                singer_id=singer_id,
                parent_id=parent_id,
                order=order,
            )
            if aux_sentence is not None:
                aux_sentence.song_part = line.song_part
                result.append(aux_sentence)
                order += 1
    return result


def parse_ttml_document(content: str) -> TTMLDocument:
    """便捷函数：解析 TTML 内容并返回 :class:`TTMLDocument`。"""
    return TTMLParser().parse_document(content)


def apply_agent_metadata(document: TTMLDocument, singers) -> None:
    """把 TTML agent 来源信息复制到已创建的 Singer 上。

    导入层按显示名建 Singer 后调用；只填空字段，不覆盖已有来源信息。
    """
    by_name = {}
    for agent in document.agents.values():
        by_name.setdefault(agent.name, agent)
    for singer in singers:
        agent = by_name.get(getattr(singer, "name", ""))
        if agent is None or getattr(singer, "ttml_agent_id", ""):
            continue
        singer.ttml_agent_id = agent.id
        singer.ttml_agent_type = agent.type
        singer.ttml_agent_named = agent.named
        singer.ttml_agent_display_name = agent.name
        singer.ttml_agent_source_name = agent.source_name
