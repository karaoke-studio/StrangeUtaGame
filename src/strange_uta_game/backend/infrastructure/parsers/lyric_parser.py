"""歌词文件解析器

支持 TXT、LRC（逐行/逐字/增强型）、KRA、ASS、SRT、Nicokara、TTML 格式的解析。
"""

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from pathlib import Path

from strange_uta_game.backend.domain import (
    Sentence,
    Character,
    Ruby,
    RubyPart,
    AuxTrack,
)
from strange_uta_game.backend.infrastructure.parsers.encoding import decode_lyric_bytes

logger = logging.getLogger(__name__)

# ASCII 半角空白字符集（用于 .strip() 时保留全角空格 U+3000）
_HALF_WS = " \t\n\r\f\v"


def _cjk_strip(s: str) -> str:
    """去除 ASCII 空白字符，保留全角空格（U+3000）。"""
    return s.strip(_HALF_WS)


def _cjk_lstrip(s: str) -> str:
    """仅去除左侧 ASCII 空白字符，保留全角空格（U+3000）。"""
    return s.lstrip(_HALF_WS)


class ParseError(Exception):
    """解析错误"""

    pass


@dataclass
class ParsedLine:
    """解析后的歌词行数据

    Attributes:
        text: 行文本
        timetags: [(char_idx, timestamp_ms), ...] 列表（每字首 ts）
        line_end_ts: 行尾释放时间戳（停顿点拖音终止点，毫秒）。
            ASS 解析器会把最后一个 \\k 的尾部时长写到这里，
            转换时绑给末字符的 sentence_end_ts。
        ruby_map: char_idx → (parts_list, span_length)。
            - parts_list: ruby 分段文本列表（多 part 单字 = 多元素；
              单 part 单字/多字共享 reading = 单元素）。
            - span_length: ruby 管辖的连续字符数（首字算第 1 个）。
              单字多 part: span=1, parts=N；多字单 part: span=N, parts=1。
            来源：ASS 的 `汉字|<かな`（含 `#|` 续段）语法。
        extra_checkpoints_map: char_idx → [额外 checkpoint ts, ...]
            ASS `#|` 续段给同字追加的内部 checkpoint 时间戳（不含首 ts）。
            parse_to_sentences 会全部 add_timestamp 进去，让 check_count 增长，
            使导出器能按 part 复原 `{\\k}` 时长。
        gap_pause_map: char_idx → 句中停顿释放 ts。
            ASS 非末尾空文本 \\k 段（`{\\k<N>}` 后无文字，SUG 导出器用它
            表达断句轴点后的间隙）的段起点，绑给最近的有 ts 字符。
            parse_to_sentences 会还原为该字的 sentence_end_ts
            （is_sentence_end=True）。
        char_singer_map: char_idx → singer 显示名。
            ASS 的 `{\\sing_<name>}` per-char 演唱者切换标记解析产物。
            parse_to_sentences 在外部 (frontend lyric_loader) 把显示名映射成
            singer_id 后赋给 Character.singer_id。
    """

    text: str
    timetags: List[Tuple[int, int]]  # (char_idx, timestamp_ms) 列表
    line_end_ts: Optional[int] = None
    # True = line_end_ts 绑到行末字符（SRT/无 \k 的 ASS 等行级时间轴格式，
    # Dialogue End 即整行末尾）；False = 沿 linked_to_next 链走到连词组
    # 尾字符（\k 链格式，与 SUG 导出契约对齐）
    line_end_bind_last: bool = False
    ruby_map: Dict[int, Tuple[List[str], int]] = field(default_factory=dict)
    extra_checkpoints_map: Dict[int, List[int]] = field(default_factory=dict)
    gap_pause_map: Dict[int, int] = field(default_factory=dict)
    char_singer_map: Dict[int, str] = field(default_factory=dict)
    # char_idx → 是否与下一字连词（True）。目前仅 TTML 的 SUG 扩展 role
    # 使用；其他格式的解析结果保持默认（不改动既有行为）。
    linked_map: Dict[int, bool] = field(default_factory=dict)
    # char_idx → 显式节奏点数量。仅当格式本身能无歧义给出 checkpoint 数时
    # 使用（TTML 的空 checkpoint span）；既有格式留空，行为不变。
    check_count_map: Dict[int, int] = field(default_factory=dict)
    # 行级演唱者显示名（TTML p 的 ttm:agent）；空 = 沿用 default_singer_id。
    # 仅用于把 Sentence.singer_id 一并还原，避免只有 Character 带 singer。
    line_singer_name: str = ""
    # 本行携带的辅助轨（TTML 的翻译 / 音译 / 背景人声等）。非 TTML 格式为空。
    aux_tracks: List[AuxTrack] = field(default_factory=list)
    # AMLL/Apple Music 段落（Verse / Chorus / ...）；空 = 未标注。
    # 块级段落元数据，不是逐字演唱者分配。
    song_part: str = ""
    # 空 p 同时带 x-sug-empty 与辅助轨时，仍要保留空主行（否则重导入丢失）。
    keep_empty_main: bool = False


@dataclass
class NicokaraParsedLine:
    """Nicokara 解析后的歌词行数据（含演唱者信息）"""

    text: str
    timetags: List[Tuple[int, int]]  # (char_idx, timestamp_ms) 列表 - 仅起始 ts
    # char_idx → singer_key 映射（singer_key 如 "sv1"、"sv9"）
    char_singer_map: Dict[int, str] = field(default_factory=dict)
    line_singer_key: str = ""  # 行级别默认演唱者 key
    line_end_ts: Optional[int] = None  # 行末未消费的时间戳（停顿点 ts）
    # char_idx → 释放 ts（句中双 ts 模式，绑给 linked group 尾字符）
    release_ts_map: Dict[int, int] = field(default_factory=dict)


@dataclass
class NicokaraRubyEntry:
    """Nicokara @Ruby 条目"""

    kanji: str  # 漢字原文
    reading: str  # 読み（可能含相对时间戳）
    positions: List[str] = field(default_factory=list)  # 出现位置时间戳


@dataclass
class NicokaraParseResult:
    """Nicokara 文件完整解析结果"""

    lines: List[NicokaraParsedLine]
    ruby_entries: List[NicokaraRubyEntry]
    # singer_key → singer 显示名 映射（从 @Emoji 解析）
    singer_definitions: Dict[str, str]
    metadata: Dict[str, str] = field(default_factory=dict)


class LyricParser(ABC):
    """歌词解析器抽象基类"""

    @abstractmethod
    def parse(self, content: str) -> List[ParsedLine]:
        """解析歌词内容

        Args:
            content: 歌词文本内容

        Returns:
            解析后的行列表
        """
        pass

    def parse_file(self, file_path: str) -> List[ParsedLine]:
        """从文件解析歌词

        Args:
            file_path: 文件路径

        Returns:
            解析后的行列表

        Raises:
            ParseError: 文件不存在或无法读取
        """
        path = Path(file_path)
        if not path.exists():
            raise ParseError(f"文件不存在: {file_path}")

        try:
            raw = path.read_bytes()
        except Exception as e:
            raise ParseError(f"读取文件失败: {e}")

        try:
            # 公共编码回退链：BOM → utf-8-sig → cp932 → gb18030 → big5（E1）
            content, _encoding = decode_lyric_bytes(raw)
        except UnicodeDecodeError as e:
            raise ParseError(f"无法解码文件: {e}")

        return self.parse(content)


class TXTParser(LyricParser):
    """TXT 纯文本歌词解析器

    每行文本成为一个歌词行，没有时间标签。
    """

    def parse(self, content: str) -> List[ParsedLine]:
        """解析 TXT 格式

        支持换行符分割，自动过滤空行和纯标点行。
        """
        lines = []

        for line_text in content.split("\n"):
            line_text = _cjk_strip(line_text)

            # 跳过空行
            if not line_text:
                continue

            # 跳过纯数字行
            if line_text.isdigit():
                continue

            # 跳过纯标点和特殊符号行（注意 \s 是空白类；写成 \\s 会把
            # 「反斜杠+s」当标点，单字符行 "s" 被误跳过 —— E10）
            if re.match(
                r"^[\[\]【】（）(){}<>\"\u2018\u2019`~!@#$%^&*+=|\\:;,.?/\s\-]+$",
                line_text,
            ):
                continue

            # 跳过 HTML/XML 标签行
            if line_text.startswith("<") and line_text.endswith(">"):
                continue

            # 跳过纯时间戳行
            if re.match(r"^\[\d{1,2}:\d{2}[.:]\d{2,3}\]$", line_text):
                continue

            lines.append(ParsedLine(text=line_text, timetags=[]))

        return lines


class LRCParser(LyricParser):
    """LRC 歌词格式解析器

    LRC 格式: [mm:ss.xx]歌词文本
    示例: [00:12.34]歌词内容

    支持逐行格式、逐字格式和增强型格式：
    - 逐行: [00:12.34]这是一整行歌词
    - 逐字: [00:12.34]这[00:13.00]是[00:14.00]逐[00:15.00]字
    - 增强型: [00:12.34]<00:12.34>这<00:13.00>是<00:14.00>增<00:15.00>强
    """

    # 标准 LRC 时间标签正则: [mm:ss.xx] 或 [mm:ss.xxx]
    TIME_TAG_PATTERN = re.compile(r"\[(\d{1,2}):(\d{2})[:.](\d{2,3})\]")

    # 增强型 LRC 时间标签正则: <mm:ss.xx> 或 <mm:ss.xxx>
    ENHANCED_TAG_PATTERN = re.compile(r"<(\d{1,2}):(\d{2})[:.](\d{2,3})>")

    # ID标签（元数据）: [xx:xx]
    ID_TAG_PATTERN = re.compile(r"^\[([a-zA-Z]+):(.*)\]$")

    def parse(self, content: str) -> List[ParsedLine]:
        """解析 LRC 格式"""
        # 去除 UTF-8 BOM（Python str.strip() 不会移除 \ufeff）
        content = content.lstrip("\ufeff")

        # [offset:±ms] 全局偏移标签（E13）：LRC 元数据可整体平移时间轴。
        # 取文件中最后一次出现的值；按 LRC 常见约定（foobar2000/MiniLyrics
        # 等）正值 = 歌词提前显示（时间戳整体前移），负值 = 延后；
        # 平移后不产生负时间戳。
        offset_ms = 0
        for offset_match in re.finditer(r"\[offset:([+-]?\d+)\]", content, re.IGNORECASE):
            offset_ms = int(offset_match.group(1))

        lines = []

        for line_text in content.split("\n"):
            line_text = _cjk_strip(line_text)

            if not line_text:
                continue

            # 跳过纯数字行
            if line_text.isdigit():
                continue

            # 跳过纯标点和特殊符号行（同 E10：\s 是空白类，不能写成 \\s）
            if re.match(
                r"^[\[\]【】（）(){}<>\"\u2018\u2019`~!@#$%^&*+=|\\:;,.?/\s\-]+$",
                line_text,
            ):
                continue

            # 检查是否是纯 ID 标签行（如 [ti:标题] [ar:艺术家]）
            if self.ID_TAG_PATTERN.match(line_text):
                continue

            # 检查是否是纯时间戳行（没有时间标签后接文本）
            if re.match(r"^\[\d{1,2}:\d{2}[.:]\d{2,3}\]+$", line_text):
                continue

            # 查找所有时间标签 [mm:ss.xx]
            matches = list(self.TIME_TAG_PATTERN.finditer(line_text))

            if not matches:
                # 没有时间标签，但有其他内容
                # 检查是否是纯歌词文本（没有时间标签）
                # 如果是纯文本，可以作为无时间标签的歌词行
                if len(line_text) > 0 and not line_text.startswith("["):
                    lines.append(ParsedLine(text=line_text, timetags=[]))
                continue

            # 检查是否为增强型 LRC 格式（含 <mm:ss.xx> 标签）
            enhanced_matches = list(self.ENHANCED_TAG_PATTERN.finditer(line_text))
            if enhanced_matches:
                lyric_text, timetags, end_ts = self._parse_enhanced_lrc(
                    line_text, matches, enhanced_matches
                )
                if lyric_text:
                    lines.append(
                        ParsedLine(
                            text=lyric_text,
                            timetags=timetags,
                            line_end_ts=end_ts,
                            # 行尾释放绑行末字符（同逐字格式，理由见下文）
                            line_end_bind_last=end_ts is not None,
                        )
                    )
                continue

            # LRC 重复段惯例（E2）：行首连续多个时间标签后接正文
            # （[t1][t2][t3]歌词）表示同一段歌词在多个时间点重复出现，
            # 展开为多行。必须先于逐字/逐词判定——这类行标签之间无正文、
            # 末标签之后才有正文，会被逐字判定（末标签后带文本）错误吞掉。
            if self._has_only_adjacent_tags(line_text, matches):
                repeat_text = _cjk_strip(line_text[matches[-1].end():])
                if repeat_text:
                    for match in matches:
                        lines.append(
                            ParsedLine(
                                text=repeat_text,
                                timetags=[(0, self._parse_timestamp(match))],
                            )
                        )
                    continue

            # 判断是逐行格式还是逐字/逐词格式
            # 逐字/逐词格式特征：时间标签后面跟着文本，然后又是时间标签
            # 例如：[00:00.000]春[00:01.086]日[00:01.629]影
            #       [00:01.00]Wir [00:01.50]im [00:01.90]fernen
            is_word_by_word = self._is_word_by_word_format(line_text, matches)

            if is_word_by_word:
                # 逐字/逐词格式：提取所有字符和时间标签
                lyric_text, timetags, end_ts = self._parse_word_by_word(
                    line_text, matches
                )
                if lyric_text:
                    lines.append(
                        ParsedLine(
                            text=lyric_text,
                            timetags=timetags,
                            line_end_ts=end_ts,
                            # 行尾释放绑行末字符：链尾绑定会落在最后一个
                            # 有 ts 的字符上（行末是全角空格等无 ts 排版
                            # 字符时不在行末）
                            line_end_bind_last=end_ts is not None,
                        )
                    )
            else:
                # 逐行格式：提取最后一个时间标签后的文本作为歌词
                last_match = matches[-1]
                lyric_text = _cjk_strip(line_text[last_match.end():])

                # 行尾空时间标签：最后一个标签后无文本（[start]歌词[end]），
                # 该标签即行末释放点（与 SRT/ASS 的 End 同义）
                line_end_ts: Optional[int] = None
                if not lyric_text and len(matches) >= 2:
                    end_ts = self._parse_timestamp(last_match)
                    if end_ts > self._parse_timestamp(matches[0]):
                        line_end_ts = end_ts

                if not lyric_text:
                    # 尝试提取时间标签之间的文本: [start]歌词[end]
                    if len(matches) >= 2:
                        first_end = matches[0].end()
                        last_start = matches[-1].start()
                        lyric_text = _cjk_strip(line_text[first_end:last_start])
                    if not lyric_text:
                        continue

                # 标准 LRC 格式，只取第一个时间标签作为整行时间
                first_match = matches[0]
                timestamp_ms = self._parse_timestamp(first_match)

                lines.append(
                    ParsedLine(
                        text=lyric_text,
                        timetags=[(0, timestamp_ms)],  # 整行时间标签放在第一个字符
                        # 行级时间轴：行尾释放绑行末字符（与 SRT/无 \k ASS 一致）
                        line_end_ts=line_end_ts,
                        line_end_bind_last=line_end_ts is not None,
                    )
                )

        # 应用 [offset:] 全局偏移（E13）：正值前移（提前）、负值后移
        if offset_ms:
            for parsed in lines:
                parsed.timetags = [
                    (ci, max(0, ts - offset_ms)) for ci, ts in parsed.timetags
                ]
                if parsed.line_end_ts is not None:
                    parsed.line_end_ts = max(0, parsed.line_end_ts - offset_ms)

        return lines

    @staticmethod
    def _has_only_adjacent_tags(line_text: str, matches: List[re.Match]) -> bool:
        """行首多个时间标签是否连续相邻（标签之间无正文）→ LRC 重复段写法。"""
        for i in range(len(matches) - 1):
            gap = line_text[matches[i].end(): matches[i + 1].start()]
            if gap.strip():
                return False
        return True

    def _is_word_by_word_format(self, line_text: str, matches: List[re.Match]) -> bool:
        """判断是否是逐字/逐词格式

        逐字/逐词格式特征（E2）：
        1. 时间标签数量 >= 3；
        2. 第一个时间标签在行首（标签前无正文）；
        3. 至少有一个时间标签之后带正文（标签之间或被末标签之后）。

        兼容两种粒度：
        - 逐字：每个标签只带 1 个字符（[t]春[t]日[t]影）；
        - 逐词：每个标签带一个词（[t]Wir [t]im [t]fernen …），
          标签间文本可达若干字符。

        判定依据是「标签后面跟着文本」这一事实，而不再限制标签间隙长度：
        旧的「间隙 ≤ 2 字符」硬限制会把词级标签（间隙 5～10 字符）误判为
        逐行格式，导致中间标签之后的文本被整段丢弃（只留最后一个词）。

        纯相邻标签行（[t1][t2][t3]歌词）标签之间无正文、末标签之后才有
        正文，虽满足「末标签之后带文本」，但由调用方在重复段展开
        （`_has_only_adjacent_tags`）之前拦截，不会走到这里。
        """
        if len(matches) < 3:
            return False

        # 检查第一个时间标签位置：标签前有正文 → 不是逐字/逐词格式
        first_match = matches[0]
        if first_match.start() > 0:
            prefix = _cjk_strip(line_text[: first_match.start()])
            if len(prefix) > 0:
                return False

        # 至少有一个标签之后带正文：标签之间，或被末标签之后。
        for i in range(len(matches) - 1):
            gap = line_text[matches[i].end(): matches[i + 1].start()]
            if _cjk_strip(gap):
                return True

        trailing = line_text[matches[-1].end():]
        if _cjk_strip(trailing):
            return True

        # 只有连续的裸标签（无任何正文），无法作为歌词使用
        return False

    def _parse_word_by_word(
        self, line_text: str, matches: List[re.Match]
    ) -> Tuple[str, List[Tuple[int, int]], Optional[int]]:
        """解析逐字/逐词格式

        格式：[00:00.000]春[00:01.086]日[00:01.629]影[00:02.500]
                                                    ^ 末尾无文字的时间戳
                                                      = 停顿点 line_end_ts

        每个时间标签只给「该段首个非空白字符」落一个轴，段内其余字符
        原样保留、不补轴。逐字格式下每段恰好 1 个字符，等价于逐字落轴；
        逐词格式下每段是一个词，轴落在词首字符，词内其余字母无轴。

        Returns:
            (歌词文本, 时间标签列表, line_end_ts)
        """
        lyric_chars = []
        timetags = []
        char_idx = 0
        line_end_ts: Optional[int] = None

        # 遍历所有时间标签
        for i, match in enumerate(matches):
            timestamp_ms = self._parse_timestamp(match)

            # 获取时间标签后的字符（直到下一个时间标签或行尾）
            start_pos = match.end()
            if i + 1 < len(matches):
                end_pos = matches[i + 1].start()
            else:
                end_pos = len(line_text)

            # 提取字符
            chars = line_text[start_pos:end_pos]

            # 末尾时间戳后只有空白（含全角空格）仍表示停顿点。
            # 行中间的全角空格继续作为歌词字符保留。
            if i == len(matches) - 1 and timetags and (
                not chars or chars.isspace()
            ):
                line_end_ts = timestamp_ms
                continue

            # 只给段内首个非空白字符落轴，其余字符原样保留（不补轴、
            # 不做 +10ms 递推）。旧实现给段内每个字符都补轴并按 10ms
            # 递推，词级输入下会把轴铺满整个词且时刻被污染。
            anchored = False
            for char in chars:
                if char.strip():
                    if not anchored:
                        timetags.append((char_idx, timestamp_ms))
                        anchored = True
                lyric_chars.append(char)
                char_idx += 1

        raw_text = "".join(lyric_chars)
        lyric_text = _cjk_strip(raw_text)

        # 去除前导空白后重新计算索引
        leading_spaces = len(raw_text) - len(_cjk_lstrip(raw_text))
        if leading_spaces > 0:
            timetags = [
                (ci - leading_spaces, ts) for ci, ts in timetags if ci >= leading_spaces
            ]

        return lyric_text, timetags, line_end_ts

    def _parse_timestamp(self, match: re.Match) -> int:
        """从正则匹配解析时间戳（毫秒）"""
        minutes = int(match.group(1))
        seconds = int(match.group(2))
        centis = match.group(3)

        # 统一转换为毫秒
        if len(centis) == 2:
            # 百分秒（0-99）
            millis = int(centis) * 10
        else:
            # 毫秒（0-999）
            millis = int(centis)

        return (minutes * 60 + seconds) * 1000 + millis

    def _parse_enhanced_lrc(
        self,
        line_text: str,
        bracket_matches: List[re.Match],
        angle_matches: List[re.Match],
    ) -> Tuple[str, List[Tuple[int, int]], Optional[int]]:
        """解析增强型 LRC 格式

        格式: [00:20.799]<00:20.799>い<00:21.367>ま<00:21.598>私<00:25.000>
                                                              ^ 末尾空标签
                                                                = line_end_ts
        [mm:ss.xx] 是行级时间标签（忽略），<mm:ss.xx> 是逐字时间标签。

        Returns:
            (歌词文本, 时间标签列表, line_end_ts)
        """
        lyric_chars: List[str] = []
        timetags: List[Tuple[int, int]] = []
        char_idx = 0
        line_end_ts: Optional[int] = None

        # 合并所有标签位置（方括号和尖括号），按位置排序
        all_tag_spans: List[Tuple[int, int]] = []
        for m in bracket_matches:
            all_tag_spans.append((m.start(), m.end()))
        for m in angle_matches:
            all_tag_spans.append((m.start(), m.end()))
        all_tag_spans.sort(key=lambda s: s[0])

        # 遍历增强标签，提取字符和时间戳
        for i, match in enumerate(angle_matches):
            timestamp_ms = self._parse_timestamp(match)

            # 获取该标签后到下一个标签之前的文本
            start_pos = match.end()
            # 找到下一个任意标签的起始位置
            end_pos = len(line_text)
            for tag_start, _tag_end in all_tag_spans:
                if tag_start > match.end():
                    end_pos = tag_start
                    break

            chars = line_text[start_pos:end_pos]

            # 末尾增强标签后只有空白（含全角空格）仍是停顿点，
            # 不把尾随排版空格变成歌词；中间的全角空格仍保留。
            if i == len(angle_matches) - 1 and timetags and (
                not chars or chars.isspace()
            ):
                line_end_ts = timestamp_ms
                continue

            if not _cjk_strip(chars):
                continue

            # 为第一个非空白字符添加时间标签
            tag_assigned = False
            for ch in chars:
                lyric_chars.append(ch)
                if not tag_assigned and not ch.isspace():
                    timetags.append((char_idx, timestamp_ms))
                    tag_assigned = True
                char_idx += 1

        raw_text = "".join(lyric_chars)
        lyric_text = _cjk_strip(raw_text)

        # 去除前导空白后重新计算索引
        leading_spaces = len(raw_text) - len(_cjk_lstrip(raw_text))
        if leading_spaces > 0:
            timetags = [
                (ci - leading_spaces, ts) for ci, ts in timetags if ci >= leading_spaces
            ]

        return lyric_text, timetags, line_end_ts


class KRAParser(LRCParser):
    """KRA 格式解析器

    KRA 格式与 LRC 完全相同，只是文件扩展名不同。
    """

    pass


UTATEN_RUBY_MARKER = "[tool:utaten-ruby]"


class UtatenRubyParser(LyricParser):
    """UtaTen ruby 标记歌词解析器。

    主程序从 UtaTen 生成的无时间戳 LRC 形如：
        [tool:utaten-ruby]
        {国道||こくどう}{沿||ぞ}いのホテルを

    `{原文||读音}` 代表 UtaTen 页面上的一个 ruby span。多字 ruby 作为
    一个连词块导入：首字承载整段读音，块内字符 linked_to_next=True。
    """

    METADATA_PATTERN = re.compile(r"^\[[A-Za-z][A-Za-z0-9_-]*:.*\]$")

    @staticmethod
    def is_utaten_format(content: str) -> bool:
        return any(_cjk_strip(line) == UTATEN_RUBY_MARKER for line in content.splitlines())

    def parse(self, content: str) -> List[ParsedLine]:
        lines: List[ParsedLine] = []
        for raw_line in content.lstrip("\ufeff").splitlines():
            line_text = _cjk_strip(raw_line)
            if not line_text:
                continue
            if line_text == UTATEN_RUBY_MARKER or self.METADATA_PATTERN.match(line_text):
                continue
            parsed = self._parse_annotated_line(line_text)
            if parsed.text:
                lines.append(parsed)
        return lines

    def _parse_annotated_line(self, line_text: str) -> ParsedLine:
        raw_chars: List[str] = []
        ruby_map: Dict[int, Tuple[List[str], int]] = {}
        i = 0
        while i < len(line_text):
            if line_text[i] != "{":
                raw_chars.append(line_text[i])
                i += 1
                continue
            close = line_text.find("}", i + 1)
            if close < 0:
                raw_chars.append(line_text[i])
                i += 1
                continue

            content = line_text[i + 1 : close]
            if "||" not in content:
                raw_chars.extend(content)
                i = close + 1
                continue

            text_part, reading_part = content.split("||", 1)
            start_idx = len(raw_chars)
            raw_chars.extend(text_part)
            if text_part and reading_part:
                if "," in reading_part:
                    for offset, reading_group in enumerate(reading_part.split(",")):
                        parts = [part for part in reading_group.split("|") if part]
                        if parts and offset < len(text_part):
                            ruby_map[start_idx + offset] = (parts, 1)
                else:
                    parts = [part for part in reading_part.split("|") if part]
                    if parts:
                        ruby_map[start_idx] = (parts, len(text_part))
            i = close + 1
        return ParsedLine(text=_cjk_strip("".join(raw_chars)), timetags=[], ruby_map=ruby_map)


class NicokaraParser:
    """Nicokara LRC 格式解析器

    解析 RhythmicaLyrics 风格的 Nicokara 逐字 LRC 格式，包括：
    - 【svN】演唱者标签（行首和行内切换）
    - [MM:SS:CC] 时间戳（冒号分隔的厘秒格式）
    - @Ruby 注音元数据
    - @Emoji 演唱者定义

    Nicokara 样例格式:
        [00:02:50]【sv1】この色...       # 头部：singer 声明行
        【sv1】[00:18:74]♪[00:19:74]押...  # 正文：【svN】开头，per-char timestamp
        【sv1】[00:29:78]Fight [00:30:08]【sv9】[00:30:23]fight  # 行内 singer 切换
        @Emoji=【sv1】,透明画像1x1.png,...   # 尾部：singer 定义
        @Ruby1=押,お                       # @Ruby: 汉字→假名映射
    """

    # Nicokara 时间戳: [MM:SS:CC] 冒号分隔。
    # SHINTA 2025 规格严格要求每段 2 位数字；解析端宽松接受 \d{1,2}（向后兼容
    # 早期手写文件），但 NicokaraParser.parse() 会对违规第 1 段做 warning，
    # 不阻断解析（默认宽松策略）。
    NICOKARA_TS_PATTERN = re.compile(r"\[(\d{1,2}):(\d{2}):(\d{2})\]")
    # 严格规格匹配（仅用于诊断 warning，不替换主匹配）
    NICOKARA_TS_STRICT_PATTERN = re.compile(r"\[(\d{2}):(\d{2}):(\d{2})\]")
    # 标准 LRC 时间戳: [MM:SS.CC] 或 [MM:SS:CC]
    FLEXIBLE_TS_PATTERN = re.compile(r"\[(\d{1,2}):(\d{2})[:.](\d{2,3})\]")
    # 演唱者标签: 【svN】或【演唱者名】
    SINGER_TAG_PATTERN = re.compile(r"【([^】]+)】")
    # @Ruby 条目。编号可省略：Kirakara/KRL 风格统一写 `@Ruby=`，条目靠
    # 后面的位置时间戳消歧（numbered 的 SHINTA 规格仍兼容）。
    RUBY_PATTERN = re.compile(r"^@Ruby(\d*)=(.+)$")
    # @Emoji 条目（演唱者定义）
    EMOJI_PATTERN = re.compile(r"^@Emoji=(.+)$")
    # 元数据标签
    META_PATTERN = re.compile(r"^@(\w+)=(.*)$")

    @staticmethod
    def is_nicokara_format(content: str) -> bool:
        """检测内容是否为 Nicokara 格式

        特征：含有 【svN】 标签 或 @Ruby/@Emoji 元数据
        """
        # 检查 【svN】 模式
        if re.search(r"【sv\d+】", content):
            return True
        # 检查 @Ruby 或 @Emoji 元数据（编号可省略）
        if re.search(r"^@(Ruby\d*|Emoji)=", content, re.MULTILINE):
            return True
        # 检查 [MM:SS:CC] 冒号分隔的时间戳（Nicokara 特有）
        if re.search(r"\[\d{1,2}:\d{2}:\d{2}\]", content):
            # 排除内联格式 [N|MM:SS:CC]
            if not re.search(r"\[\d+\|\d{2}:\d{2}:\d{2}\]", content):
                return True
        return False

    def parse(self, content: str) -> NicokaraParseResult:
        """解析 Nicokara 格式文件内容

        Returns:
            NicokaraParseResult 含歌词行、ruby 条目和演唱者定义
        """
        # 去除 UTF-8 BOM（Python str.strip() 不会移除 \ufeff）
        content = content.lstrip("\ufeff")

        # Kirakara/KRL 常在文件头部带一个 JSON 风格的 `config { ... }` 块
        # （字体、配色等渲染配置）。它不属于歌词，必须剥离，否则会被当成
        # 正文行解析为一堆字体/配置“歌词”。仅剥离文件最前面的该块。
        from strange_uta_game.backend.infrastructure.parsers.kasugamuki_format import (
            strip_krl_config,
        )

        content = strip_krl_config(content)

        # SHINTA 2025 规格诊断（宽松+warning）：
        #   - 严格 ts 形如 [MM:SS:CC]（M/S/C 均 2 位）
        #   - 解析仍走 NICOKARA_TS_PATTERN（接受 \d{1,2}:\d{2}:\d{2}）
        #   - 此处仅统计违规并 log warning，不阻断
        loose_ts_count = 0
        for _m in self.NICOKARA_TS_PATTERN.finditer(content):
            mm = _m.group(1)
            if len(mm) != 2:
                loose_ts_count += 1
        if loose_ts_count:
            logger.warning(
                "Nicokara: 检测到 %d 个非规格 ts (分钟段非 2 位)，"
                "SHINTA 2025 规格要求 [MM:SS:CC] 每段均为 2 位数字；"
                "已宽松接受。",
                loose_ts_count,
            )

        raw_lines = content.split("\n")
        body_lines: List[str] = []
        ruby_entries: List[NicokaraRubyEntry] = []
        ruby_indices: List[int] = []  # 用于 SHINTA 规格连号校验（@Ruby1..N）
        singer_definitions: Dict[str, str] = {}
        metadata: Dict[str, str] = {}

        for raw_line in raw_lines:
            # 去掉行尾 \r（Windows 换行），但保留有意义的 trailing 空格作为 body 排版
            raw_line = raw_line.rstrip("\r")
            stripped = _cjk_strip(raw_line)

            # 解析 @Ruby 元数据
            ruby_match = self.RUBY_PATTERN.match(stripped) if stripped else None
            if ruby_match:
                index_str = ruby_match.group(1)
                # 仅编号条目参与连号校验；无编号的 Kirakara 条目跳过校验。
                if index_str:
                    ruby_indices.append(int(index_str))
                entry = self._parse_ruby_entry(ruby_match.group(2))
                if entry:
                    ruby_entries.append(entry)
                continue

            # 解析 @Emoji 元数据（演唱者定义）
            emoji_match = self.EMOJI_PATTERN.match(stripped) if stripped else None
            if emoji_match:
                emoji_value = emoji_match.group(1)
                defs = self._parse_emoji_line(emoji_value)
                singer_definitions.update(defs)
                # 保留完整 @Emoji 行以支持 round-trip（图片/Zoom/NoDecor/Margin 等）。
                # 用 _EmojiN 唯一键存储，_sync_nicokara_metadata_to_settings 中还原为 @Emoji=。
                idx = len([k for k in metadata if k.startswith("_Emoji")])
                metadata[f"_Emoji{idx}"] = emoji_value
                continue

            # 解析其他 @ 元数据
            meta_match = self.META_PATTERN.match(stripped) if stripped else None
            if meta_match:
                key = meta_match.group(1)
                value = meta_match.group(2)
                if key not in ("Ruby", "Emoji"):
                    metadata[key] = value
                continue

            # 非元数据：进入 body（含空行，作为用户排版意图保留）
            # 但 body 开始前的纯空行/BOM 行跳过
            if not stripped and not body_lines:
                continue
            # body 用原始行（保留 trailing 空格），但开头空行已剥
            body_lines.append(raw_line if stripped else "")

        # 去除 body 尾部的纯空行：正文与尾部 @ 标签（@Emoji/@Ruby/@Title…）之间、
        # 或文件末尾的空行只是分隔/排版残留，不应被解析成正文空行（空 Sentence）。
        # 注意：只 trim **尾部**——verse 之间的空行（后面还有真正正文行）仍保留。
        # 这里 "" 仅来自纯空/纯空白行；含 ts 的停顿行（如 `[ts1] [ts2]`）stripped 非空，
        # 存的是 raw_line（truthy），不会被误删。
        while body_lines and not body_lines[-1]:
            body_lines.pop()

        # 解析正文行
        parsed_lines = []
        for line_text in body_lines:
            if not line_text:
                # 空行：保留作为用户排版意图
                parsed_lines.append(
                    NicokaraParsedLine(
                        text="",
                        timetags=[],
                        line_singer_key="",
                        char_singer_map={},
                    )
                )
                continue
            parsed = self._parse_body_line(line_text)
            if parsed is not None:
                parsed_lines.append(parsed)

        # SHINTA 2025 规格：@RubyN 编号应从 1 连号递增、不跳号、不重复。
        # 宽松模式：仅 warning，不阻断；解析顺序仍按文件中出现顺序。
        if ruby_indices:
            expected = list(range(1, len(ruby_indices) + 1))
            if ruby_indices != expected:
                logger.warning(
                    "Nicokara: @RubyN 编号违规：实际 %s，规格期望 %s "
                    "(从 1 连号递增、不跳号、不重复)。已宽松接受。",
                    ruby_indices,
                    expected,
                )

        return NicokaraParseResult(
            lines=parsed_lines,
            ruby_entries=ruby_entries,
            singer_definitions=singer_definitions,
            metadata=metadata,
        )

    def parse_file(self, file_path: str) -> NicokaraParseResult:
        """从文件解析 Nicokara 格式"""
        path = Path(file_path)
        if not path.exists():
            raise ParseError(f"文件不存在: {file_path}")

        try:
            raw = path.read_bytes()
        except Exception as e:
            raise ParseError(f"读取文件失败: {e}")

        try:
            # 公共编码回退链：BOM → utf-8-sig → cp932 → gb18030 → big5（E1）
            content, _encoding = decode_lyric_bytes(raw)
        except UnicodeDecodeError as e:
            raise ParseError(f"无法解码文件: {e}")

        return self.parse(content)

    def _parse_body_line(self, line_text: str) -> Optional[NicokaraParsedLine]:
        """解析一行正文歌词

        处理 【svN】 演唱者标签和 [MM:SS:CC] 时间戳
        """
        # 查找所有 token（时间戳和演唱者标签）
        tokens: List[Tuple[int, int, str, str]] = []
        # (start, end, type, value)  type: 'ts' or 'singer'

        for m in self.FLEXIBLE_TS_PATTERN.finditer(line_text):
            ts_ms = self._parse_nicokara_timestamp(m)
            tokens.append((m.start(), m.end(), "ts", str(ts_ms)))

        for m in self.SINGER_TAG_PATTERN.finditer(line_text):
            tokens.append((m.start(), m.end(), "singer", m.group(1)))

        # 按位置排序
        tokens.sort(key=lambda t: t[0])

        # 提取纯文本字符和对应的时间戳/演唱者
        lyric_chars: List[str] = []
        timetags: List[Tuple[int, int]] = []
        char_singer_map: Dict[int, str] = {}
        line_singer_key = ""
        current_singer = ""
        char_idx = 0

        # 分段处理文本
        prev_end = 0
        pending_ts: Optional[int] = None
        release_ts_map: Dict[int, int] = {}

        for start, end, token_type, value in tokens:
            # 处理 token 之前的纯文本字符
            text_between = line_text[prev_end:start]
            for ch in text_between:
                lyric_chars.append(ch)
                if pending_ts is not None:
                    # 空格不接收起始 ts：将 pending_ts 转为前一字符的停顿点 ts
                    if ch.isspace() and len(lyric_chars) >= 2:
                        release_ts_map[len(lyric_chars) - 2] = pending_ts
                        pending_ts = None
                    else:
                        timetags.append((char_idx, pending_ts))
                        pending_ts = None
                if current_singer:
                    char_singer_map[char_idx] = current_singer
                char_idx += 1

            if token_type == "ts":
                # 若上一个 pending_ts 未被任何字符消费（即连续两个 ts 中间无文字），
                # 它是"句中双 ts 模式"的释放 ts，绑给紧邻其前的最后一个字符
                # （即 linked group 的尾字符，不论该字符是否有起始 ts）
                if pending_ts is not None and lyric_chars:
                    release_ts_map[len(lyric_chars) - 1] = pending_ts
                pending_ts = int(value)
            elif token_type == "singer":
                current_singer = value
                if not line_singer_key:
                    line_singer_key = value

            prev_end = end

        # 处理最后一段文本
        remaining = line_text[prev_end:]
        for ch in remaining:
            lyric_chars.append(ch)
            if pending_ts is not None:
                if ch.isspace() and len(lyric_chars) >= 2:
                    release_ts_map[len(lyric_chars) - 2] = pending_ts
                    pending_ts = None
                else:
                    timetags.append((char_idx, pending_ts))
                    pending_ts = None
            if current_singer:
                char_singer_map[char_idx] = current_singer
            char_idx += 1

        # 如果最后有未消费的时间戳（行末时间戳），作为整行的停顿点 ts
        # 由 nicokara_result_to_sentences 绑定到最后一个有起始 ts 的字符
        line_end_ts: Optional[int] = pending_ts if pending_ts is not None else None

        # 注意：仅做空判断，不能 strip 掉尾随空格（行末空格是用户有意保留的排版/停顿）
        raw = "".join(lyric_chars)
        text = _cjk_strip(raw)
        if not text:
            # 纯空格 + ts 的"停顿行"（如 `[ts1] [ts2]`）：
            # - 若包含 ts，保留为含 1 个空格字符的 Sentence，携带起始 ts + 行末释放 ts，
            #   使导出回原文 `[ts1] [ts2]`。
            # - 完全空行（无 ts、无字符）：text="" stub 以保留行号。
            if timetags or line_end_ts is not None or raw:
                return NicokaraParsedLine(
                    text=raw,  # 保留原始空格（通常 " "）
                    timetags=timetags,
                    char_singer_map=char_singer_map,
                    line_singer_key=line_singer_key,
                    line_end_ts=line_end_ts,
                    release_ts_map=release_ts_map,
                )
            return NicokaraParsedLine(
                text="",
                timetags=[],
                line_singer_key=line_singer_key,
                char_singer_map={},
            )
        # 前导/尾随空格都原样保留为文本字符（用户排版意图 + Nicokara round-trip）。
        # 注意：timetags / char_singer_map / release_ts_map 的下标是按**含空格的**
        # lyric_chars 计数的，text 既然保留了前导空格，下标天然与字符一一对齐，
        # **不能**再减去前导空格数——否则会把整行的「时间戳→字符」绑定左移一格
        # （历史 bug：曾经 text 会 strip 前导空格，故此处减偏移；改为保留后该减法成了错位源）。
        text = raw.rstrip("\r\n")

        return NicokaraParsedLine(
            text=text,
            timetags=timetags,
            char_singer_map=char_singer_map,
            line_singer_key=line_singer_key,
            line_end_ts=line_end_ts,
            release_ts_map=release_ts_map,
        )

    def _parse_nicokara_timestamp(self, match: re.Match) -> int:
        """从正则匹配解析 Nicokara 时间戳（毫秒）

        支持 [MM:SS:CC]（厘秒）和 [MM:SS.xxx]（毫秒）
        """
        minutes = int(match.group(1))
        seconds = int(match.group(2))
        sub = match.group(3)

        if len(sub) == 2:
            millis = int(sub) * 10  # 厘秒 → 毫秒
        else:
            millis = int(sub)  # 已经是毫秒

        return (minutes * 60 + seconds) * 1000 + millis

    def _parse_ruby_entry(self, entry_text: str) -> Optional[NicokaraRubyEntry]:
        """解析 @Ruby 条目

        格式: 漢字,読み[相対時間],位置1,位置2,...
        例:   押,お
              奪,う[00:00:22]ば
              者,しゃ,,[00:27:01]

        位置字段从右往左消费（F11）：只有空串或完整时间戳的字段才算
        位置，其余并回読み——避免正文/读音自带半角逗号时被盲目按
        split(",") 拆坏。漢字与読み之间仍以第一个逗号为界。
        """
        parts = entry_text.split(",")
        if len(parts) < 2:
            return None

        # 从右往左消费位置字段：空串或可解析为 [MM:SS:CC] 的时间戳
        positions: List[str] = []
        while len(parts) > 2:
            tail = parts[-1]
            if tail and _parse_nicokara_ts_str(tail) is None:
                break
            positions.insert(0, parts.pop())

        kanji = parts[0]
        reading = ",".join(parts[1:]) if len(parts) > 2 else parts[1]

        return NicokaraRubyEntry(
            kanji=kanji,
            reading=reading,
            positions=positions,
        )

    def _parse_emoji_line(self, emoji_text: str) -> Dict[str, str]:
        """解析 @Emoji 行提取演唱者定义

        格式: @Emoji=【sv1】,透明画像1x1.png,...
        提取 【svN】 标签作为 singer_key
        """
        defs: Dict[str, str] = {}
        parts = emoji_text.split(",")
        for part in parts:
            part = part.strip()
            m = self.SINGER_TAG_PATTERN.match(part)
            if m:
                singer_key = m.group(1)
                # 默认用 singer_key 作为显示名
                defs[singer_key] = singer_key
        return defs


def nicokara_result_to_sentences(
    result: NicokaraParseResult,
    singer_key_to_id: Dict[str, str],
    default_singer_id: str,
    progress_cb=None,
) -> List[Sentence]:
    """将 NicokaraParseResult 转换为 Sentence 对象列表

    解析规则（与导出器对齐）：
    - 每个字符的第一个时间戳是起始时间戳
    - 如果一个字符有2个时间戳，第二个是停顿点时间戳（is_sentence_end=True）
    - 行末的未消费时间戳是最后一个字符的停顿点时间戳

    Args:
        result: Nicokara 解析结果
        singer_key_to_id: singer_key (如 "sv1") → Singer.id 的映射
        default_singer_id: 默认演唱者 ID（无 singer 标签的行/字符使用）

    Returns:
        Sentence 对象列表
    """
    sentences: List[Sentence] = []
    # 行级 singer 在 nicokara 中跨行延续（空行不重置，与导出器 prev_singer_id 行为对齐）
    # 跟踪上一非空行**末字符**的 singer（与导出器 prev_singer_id 同步）：
    #   - 行内混合 singer 切换时，行尾 singer 决定下一无标签行的继承值
    #   - 无显式 【svN】 标签的行继承此值；首行无标签 → default
    last_emitted_singer_id: str = default_singer_id

    # 预计算所有 @Ruby entry 的 reading_parts 和 position_range，在句子循环外执行一次。
    # 原来每行都重复解析，对 N 条 entry × M 行的文件会产生 N×M 次 regex 调用；
    # 改为只调用 N 次（主要热点：_parse_reading_with_timestamps 内部含 regex）。
    precomputed_ruby: List[Tuple] = []
    for _entry in result.ruby_entries:
        _reading_parts = _parse_reading_with_timestamps(_entry.reading)
        _pos_start, _pos_end = _parse_position_range(_entry.positions)
        precomputed_ruby.append((_entry, _reading_parts, _pos_start, _pos_end))

    _total_lines = len(result.lines)
    for _line_idx, parsed in enumerate(result.lines):
        if progress_cb is not None and _total_lines > 0:
            progress_cb(f"正在转换数据 {_line_idx + 1}/{_total_lines} 行...")
        # 确定行级别演唱者（显式标签 > 继承上一非空行末字符 singer > default）
        if parsed.line_singer_key and parsed.line_singer_key in singer_key_to_id:
            line_singer_id = singer_key_to_id[parsed.line_singer_key]
        else:
            line_singer_id = last_emitted_singer_id
        # 空 Sentence 不更新 last_emitted_singer_id；非空行在末尾按其末字符 singer 更新

        # 空行：保留为空 Sentence（用户排版意图）
        if not parsed.text:
            sentences.append(Sentence(singer_id=line_singer_id, characters=[]))
            continue

        # 创建句子（from_text 设置默认 checkpoint 配置）
        sentence = Sentence.from_text(
            text=parsed.text,
            singer_id=line_singer_id,
        )

        # is_sentence_end（演唱停顿/释放）必须严格反映文件事实：
        # from_text 默认把末字标为 is_sentence_end=True，但 Nicokara 行可能没有
        # 行末释放 ts（如以「ちゃ」「many？」结尾且其后无 [ts]）。此处先全部清零，
        # 再由下面三条 body 事实重新置位：双 ts / release_ts_map / line_end_ts。
        # is_line_end（行结构事实）保持 from_text 的「末字为行尾」不动。
        for _ch in sentence.characters:
            _ch.is_sentence_end = False
            _ch.sentence_end_ts = None

        # 先为所有字符按 char_singer_map 设置 singer_id（含无 ts 的字符：空格、英文词中间字符等）
        # 这样导出时能正确识别 singer 切换边界
        for char_idx, char_singer_key in parsed.char_singer_map.items():
            if 0 <= char_idx < len(sentence.characters):
                if char_singer_key and char_singer_key in singer_key_to_id:
                    sentence.characters[char_idx].singer_id = singer_key_to_id[char_singer_key]

        # 按字符分组时间戳
        char_ts_map: Dict[int, List[int]] = {}
        for char_idx, timestamp_ms in parsed.timetags:
            if char_idx < 0 or char_idx >= len(sentence.characters):
                continue
            if char_idx not in char_ts_map:
                char_ts_map[char_idx] = []
            char_ts_map[char_idx].append(timestamp_ms)

        # 添加时间标签（含 per-char 演唱者）
        for char_idx, ts_list in char_ts_map.items():
            # 获取该字符的演唱者
            char_singer_key = parsed.char_singer_map.get(char_idx, "")
            if char_singer_key and char_singer_key in singer_key_to_id:
                tag_singer_id = singer_key_to_id[char_singer_key]
            else:
                tag_singer_id = line_singer_id

            char = sentence.characters[char_idx]
            char.singer_id = tag_singer_id

            # 第一个时间戳是起始时间戳
            char.add_timestamp(ts_list[0])

            # 如果有第二个时间戳，是演唱停顿释放时间戳
            # 注意：is_sentence_end 是命名遗留，真实语义是"演唱时的停顿"
            # 而非语义句末。详见 models.Character.is_sentence_end 注释。
            if len(ts_list) >= 2:
                char.is_sentence_end = True
                char.sentence_end_ts = ts_list[1]
                # 设置 check_count = 1（如果还没有的话）
                if char.check_count == 0:
                    char.check_count = 1
                char.push_to_ruby()

        # 句中双 ts 模式的释放 ts：绑给 linked group 尾字符（可能无起始 ts）
        for char_idx, release_ts in parsed.release_ts_map.items():
            if 0 <= char_idx < len(sentence.characters):
                ch_obj = sentence.characters[char_idx]
                ch_obj.is_sentence_end = True
                ch_obj.sentence_end_ts = release_ts
                if ch_obj.check_count == 0:
                    ch_obj.check_count = 1
                ch_obj.push_to_ruby()

        # 行末释放 ts：绑定到 sentence 的最后一个字符（对齐导出器的行末单 ts 语义）
        # 即使最后字符是 linked group 的尾字符（无起始 ts），也绑在它上面
        if parsed.line_end_ts is not None and sentence.characters:
            last_char = sentence.characters[-1]
            last_char.is_sentence_end = True
            last_char.sentence_end_ts = parsed.line_end_ts
            if last_char.check_count == 0:
                last_char.check_count = 1
            last_char.push_to_ruby()

        # 应用 @Ruby 注音（基于文本匹配）
        _apply_precomputed_ruby_entries(sentence, precomputed_ruby)

        # 连读 follower 收敛 cc=0（与导出器 body「无 ts → 連読」语义一致）：
        # 凡是「自身没有起始 ts 且没有 ruby」的字符，都是连读到前一字的 follower，
        # 不该占自己的节奏点。覆盖 from_text 默认的 cc=1，以及 release_ts_map /
        # line_end_ts 分支为「无起始 ts 的释放字」临时设的 cc=1。
        # 例：ちゃ 的 ゃ、how 的 o/w、many 的 a/n/y、さん 的 ん、行首空格。
        # 停顿点（is_sentence_end + sentence_end_ts）与此独立：follower 仍可携带
        # 释放 ts（total_timing_points = 0 + 1），导出时只写释放 ts、不写起始 ts。
        for _ch in sentence.characters:
            if not _ch.timestamps and _ch.ruby is None and _ch.check_count != 0:
                _ch.check_count = 0

        # 触发 global_timestamps / global_sentence_end_ts 派生：
        # exporter 读取的是 global_* 字段，未调用 set_offset 时它们为空，
        # 导致 sentence-end ts 与逐字 ts 全部丢失。以 offset=0 派生与原始 ts 等价。
        for _ch in sentence.characters:
            _ch.set_offset(0)

        # 更新 last_emitted_singer_id：与导出器 prev_singer_id 同步 = 行末字符的有效 singer
        if sentence.characters:
            tail = sentence.characters[-1]
            tail_singer = tail.singer_id or sentence.singer_id or last_emitted_singer_id
            last_emitted_singer_id = tail_singer

        sentences.append(sentence)

    return sentences


# 兼容别名
nicokara_result_to_lyric_lines = nicokara_result_to_sentences


def _apply_ruby_entries(sentence: Sentence, ruby_entries: List[NicokaraRubyEntry]) -> None:
    """将 @Ruby 注音条目应用到句子（公开接口，每次调用按需计算 reading_parts）。

    外部调用（如测试）使用此函数。批量处理多行时请用 ``_apply_precomputed_ruby_entries``
    配合在循环外预计算，以避免对每行重复执行 regex。
    """
    precomputed: List[Tuple] = []
    for entry in ruby_entries:
        pos_start_ms, pos_end_ms = _parse_position_range(entry.positions)
        precomputed.append((
            entry,
            _parse_reading_with_timestamps(entry.reading),
            pos_start_ms,
            pos_end_ms,
        ))
    _apply_precomputed_ruby_entries(sentence, precomputed)


def _apply_precomputed_ruby_entries(
    sentence: Sentence,
    precomputed_ruby: List[Tuple],
) -> None:
    """将预计算好的 @Ruby 注音条目应用到句子（高性能内部版本）。

    ``precomputed_ruby`` 由调用方在句子循环**外**预计算，格式为
    ``[(entry, reading_parts, pos_start_ms, pos_end_ms), ...]``，
    避免对每一行都重复执行 regex（_parse_reading_with_timestamps /
    _parse_position_range）。

    当条目携带位置范围（positions）时，利用字符时间戳精确定位到正确的出现，
    避免同一词组在不同句子/位置有不同读音时被错误匹配。
    严格区间 [pos_start, pos_end]（左闭右闭，符合 SHINTA 2025 规格
    「適用開始時刻 ≤ t ≤ 適用終了時刻」）匹配失败的 entry 直接忽略——
    源文件手写偏差视为可接受的数据噪声，不做容差回退。
    """
    text = sentence.text
    for entry, reading_parts, pos_start_ms, pos_end_ms in precomputed_ruby:
        # 快速跳过：此句中不含该汉字，无需进入内层循环
        if entry.kanji not in text:
            continue

        has_position_filter = pos_start_ms is not None or pos_end_ms is not None

        # 在文本中查找漢字位置：按 text.find 顺序，遇到第一个通过过滤
        # 且未被占用的出现就 break（与原版一致）
        start = 0
        while True:
            pos = text.find(entry.kanji, start)
            if pos == -1:
                break
            end_pos = pos + len(entry.kanji)

            # 如果有位置范围，检查该出现的时间戳是否在范围内
            if has_position_filter:
                if pos >= len(sentence.characters):
                    start = end_pos
                    continue
                ch = sentence.characters[pos]
                if not ch.global_timestamps:
                    start = end_pos
                    continue
                char_ms = ch.global_timestamps[0]
                if pos_start_ms is not None and char_ms < pos_start_ms:
                    start = end_pos
                    continue
                if pos_end_ms is not None and char_ms > pos_end_ms:
                    start = end_pos
                    continue

            # SHINTA 2025 规格：
            #   - ルビ留空 (entry.reading=="") ⇒ 取消区间内已存在的 ruby（清除）。
            #   - 后到的 @RubyN 覆盖先到的（ruby_entries 已按文件顺序追加，
            #     for-loop 自然实现 N 大者覆盖 N 小者）。
            actual_end = min(end_pos, len(sentence.characters))
            if entry.reading == "":
                # 清除区间内所有字符的 ruby
                for ci in range(pos, actual_end):
                    sentence.characters[ci].set_ruby(None)
                    sentence.characters[ci].linked_to_next = False
            else:
                block_len = end_pos - pos
                _distribute_reading_to_chars(
                    sentence, pos, block_len, reading_parts, followers_cc_zero=True
                )
                # linked_to_next 判定（与导出语义对齐，2026-05-30 用户修订）：
                #   一个多字 @RubyN tag **本身**就是「连词」声明，块内全部字符
                #   构成同一个连词，无论块内后字在 body 是否有独立 timestamp。
                #   导出器 `_collect_ruby_entries` 严格按 linked_to_next 切段，
                #   因此要让 `@Ruby=アドベンチャー,...`/`世界,...`/`大冒険,...`
                #   这类多字条目 round-trip 回单一条目，必须把块内相邻字全部 link，
                #   而不能因「后字有独立 body ts」就断链（那会把一个连词拆成多条
                #   @RubyN，如 アドベンチャー→ア/ド/ベン/チャー）。
                #   块尾字保持 linked_to_next=False（由 range 上界 actual_end-1 保证）。
                for ci in range(pos, actual_end - 1):
                    sentence.characters[ci].linked_to_next = True
            # 不 break：n3 spec 中一个 @Ruby entry 的区间 [pos1, pos2] 可覆盖
            # 区间内 kanji 的全部出现（同 reading）。继续向后扫描。
            start = end_pos


def _distribute_reading_to_chars(
    sentence: Sentence,
    start_pos: int,
    block_len: int,
    reading_parts: List[Tuple[str, int]],
    *,
    followers_cc_zero: bool = False,
) -> None:
    """将带时间戳的 reading parts 按 body ts 边界分配到每个汉字字符。

    新规则（per-char ruby）：
      - 每个 kanji 字符独立持有自己的 ruby（不再"首字承载全部"）。
      - 分配依据 reading parts 的绝对时间戳与各字 body ts 区间的重叠关系。
      - 任一字落空（没分到 part）→ 触发整段重均分（按字符均分 reading 文本）。

    参数:
        sentence: 句子对象
        start_pos: 起始字符位置（句内 char 索引）
        block_len: 该 @Ruby 块覆盖的字符数（kanji 字数）
        reading_parts: [(text, offset_ms), ...]，其中 offset_ms 是该 part
                       起始相对基准 ts 的偏移（首 part offset=0）。
        followers_cc_zero: Nicokara 专用语义（2026-05-30）。
            True 时只把 reading 分配给「有独立 body ts 的字（timed unit）」，
            块内**无独立 body ts 的后字（follower）一律 cc=0、ruby=None**，
            与导出器 `_collect_ruby_entries`/`_export_sentence_with_singer`
            的语义一致：follower 在 body 不写 ts、读音并入前一个 timed unit。
            例：アドベンチャー body 仅 ア/ド/ベ/チ 有 ts →
                ベ='ven'(cc1)、ン=cc0、チ='ture'(cc1)、ャ/ー=cc0，
            而非旧均分把读音散成 ベ='v'/ン='en'/… 的伪走字。
            False（默认，ASS/inline 多字 span 路径）保留旧的「逐字均分」行为。
    """
    if block_len <= 0 or not reading_parts:
        return

    end_pos = min(start_pos + block_len, len(sentence.characters))
    k = end_pos - start_pos
    if k <= 0:
        return

    first_char = sentence.characters[start_pos]
    base_ts = first_char.timestamps[0] if first_char.timestamps else 0

    if followers_cc_zero:
        _distribute_reading_timed_units(sentence, start_pos, k, reading_parts, base_ts)
        return

    # ── Step 1: 按 body ts 区间分配 parts ──
    # 计算每字的 ts 区间起点（无 body ts 的字沿用前字）
    char_starts: List[int] = []
    inherited = base_ts
    for i in range(k):
        ch = sentence.characters[start_pos + i]
        if ch.timestamps:
            inherited = ch.timestamps[0]
        char_starts.append(inherited)
    # 每字 ts 区间右端 = 下一字起点；最后一字 = 无穷大
    INF = 10**18
    char_ends: List[int] = char_starts[1:] + [INF]

    per_char_parts: List[List[Tuple[str, int]]] = [[] for _ in range(k)]
    for text, offset_ms in reading_parts:
        abs_ts = base_ts + offset_ms
        target_i = 0
        for i in range(k):
            if char_starts[i] <= abs_ts < char_ends[i]:
                target_i = i
                break
        per_char_parts[target_i].append((text, abs_ts))

    # ── Step 2: 检查落空 → 整段均分回退 ──
    any_empty = any(len(p) == 0 for p in per_char_parts)
    if any_empty:
        # 整段 reading 文本拼接后按字均分
        full_text = "".join(t for t, _ in reading_parts)
        n_chars_text = len(full_text)
        per_char_parts = [[] for _ in range(k)]
        # 文本均分（末字承担余数）
        for i in range(k):
            seg_start = (i * n_chars_text) // k
            seg_end = ((i + 1) * n_chars_text) // k if i < k - 1 else n_chars_text
            seg_text = full_text[seg_start:seg_end]
            # ts 均分：首段 abs_ts=base_ts；其余按 reading 总时长均分
            # reading 总时长 = 最后一个 part 的 offset_ms（首 part offset=0）
            total_duration = reading_parts[-1][1] if len(reading_parts) > 1 else 0
            seg_abs_ts = base_ts + (i * total_duration) // k
            per_char_parts[i].append((seg_text, seg_abs_ts))

    # ── Step 3: 写入每字 ruby + timestamps ──
    for i in range(k):
        ch = sentence.characters[start_pos + i]
        parts = per_char_parts[i]
        # parts 至少有 1 个（均分回退保证；正常分配若 any_empty=False 也保证每字非空）
        local_base = ch.timestamps[0] if ch.timestamps else parts[0][1]
        ruby_parts = [
            RubyPart(text=text, offset_ms=abs_ts - local_base)
            for text, abs_ts in parts
        ]
        ch.check_count = len(ruby_parts)
        # timestamps 同步：cp=0 保留 body ts（若有）；cp>=1 用 abs_ts 注入
        preserved = list(ch.timestamps)
        new_ts: List[int] = []
        for j, (_, abs_ts) in enumerate(parts):
            if j == 0:
                if preserved:
                    new_ts.append(preserved[0])
                else:
                    # 该字无 body ts（沿用前字）：cp=0 不强行注入 ts，
                    # 否则会在导出 body 行时输出伪 ts。
                    # 但 check_count >= 1 要求 timestamps 长度可达 check_count，
                    # 此处选择留空（render 时会沿用前字）。
                    pass
            else:
                new_ts.append(abs_ts)
        ch.timestamps = new_ts
        ch.set_ruby(Ruby(parts=ruby_parts))


def _distribute_reading_timed_units(
    sentence: Sentence,
    start_pos: int,
    k: int,
    reading_parts: List[Tuple[str, int]],
    base_ts: int,
) -> None:
    """Nicokara 专用：只把 reading 分给「有独立 body ts 的 timed unit」，
    follower（块内无独立 body ts 的字）一律 cc=0、ruby=None。

    与导出器对齐：follower 的读音并入前一个 timed unit（不产生伪走字分段），
    body 端 follower 不写 ts。timed unit 可承载多个 mora（如单字 きょ+う）。
    """
    # timed unit 局部索引：块首字（锚点，即使无 ts）+ 所有自带 body ts 的字
    timed: List[int] = []
    for i in range(k):
        ch = sentence.characters[start_pos + i]
        if i == 0 or ch.timestamps:
            timed.append(i)
    timed_starts: List[int] = [
        sentence.characters[start_pos + i].timestamps[0]
        if sentence.characters[start_pos + i].timestamps
        else base_ts
        for i in timed
    ]

    # 每个 part 分给「起点 <= abs_ts 的最后一个 timed unit」
    per_timed: List[List[Tuple[str, int]]] = [[] for _ in timed]
    for text, offset_ms in reading_parts:
        abs_ts = base_ts + offset_ms
        ti = 0
        for idx in range(len(timed)):
            if timed_starts[idx] <= abs_ts:
                ti = idx
            else:
                break
        per_timed[ti].append((text, abs_ts))

    # 某 timed unit 落空（reading 段数 < timed unit 数）→ 仅在 timed unit 间均分
    if any(len(p) == 0 for p in per_timed):
        full_text = "".join(t for t, _ in reading_parts)
        m = len(timed)
        n_text = len(full_text)
        total_duration = reading_parts[-1][1] if len(reading_parts) > 1 else 0
        per_timed = [[] for _ in timed]
        for idx in range(m):
            seg_start = (idx * n_text) // m
            seg_end = ((idx + 1) * n_text) // m if idx < m - 1 else n_text
            seg_text = full_text[seg_start:seg_end]
            seg_abs_ts = base_ts + (idx * total_duration) // m
            per_timed[idx].append((seg_text, seg_abs_ts))

    timed_set = set(timed)

    # 写入 timed unit
    for li, i in enumerate(timed):
        ch = sentence.characters[start_pos + i]
        parts = per_timed[li]
        local_base = ch.timestamps[0] if ch.timestamps else parts[0][1]
        ruby_parts = [
            RubyPart(text=text, offset_ms=abs_ts - local_base)
            for text, abs_ts in parts
        ]
        ch.check_count = len(ruby_parts)
        preserved = list(ch.timestamps)
        new_ts: List[int] = []
        for j, (_, abs_ts) in enumerate(parts):
            if j == 0:
                if preserved:
                    new_ts.append(preserved[0])
            else:
                new_ts.append(abs_ts)
        ch.timestamps = new_ts
        ch.set_ruby(Ruby(parts=ruby_parts))

    # follower：cc=0、无 ruby（读音已并入前一 timed unit）
    for i in range(k):
        if i in timed_set:
            continue
        ch = sentence.characters[start_pos + i]
        ch.check_count = 0
        ch.set_ruby(None)


def _parse_nicokara_ts_str(ts_str: str) -> Optional[int]:
    """解析 [MM:SS:CC] 时间戳字符串 → 毫秒

    Args:
        ts_str: 时间戳字符串，如 "[01:23:45]" 或 "01:23:45"

    Returns:
        毫秒数，解析失败返回 None
    """
    ts_str = ts_str.strip()
    if not ts_str:
        return None
    ts_str = ts_str.strip("[]")
    m = re.match(r"^(\d{1,2}):(\d{2})[:.]?(\d{2,3})$", ts_str)
    if not m:
        return None
    minutes = int(m.group(1))
    seconds = int(m.group(2))
    sub = m.group(3)
    millis = int(sub) * 10 if len(sub) == 2 else int(sub)
    return (minutes * 60 + seconds) * 1000 + millis


# 用于匹配 reading 中的时间戳 [MM:SS:CC] 或 [MM:SS.CC]
_READING_TS_RE = re.compile(r"\[(\d{1,2}:\d{2}[:.]\d{2,3})\]")


def _parse_reading_with_timestamps(reading: str) -> List[Tuple[str, int]]:
    """解析带时间戳的 reading 字符串

    Args:
        reading: 带时间戳的读音，如 "う[00:00:50]ん" 或 "おも,[00:00:50]い"
                  或连续时间戳 "いっ[00:00:13][00:00:25]しょ"（中间空段对应导出器
                  补的空白 part / 空 mora）

    Returns:
        [(text, offset_ms), ...] 序列；连续时间戳之间会插入占位符（停顿符）part，
        确保 reading_parts 总数与导出器写入的 mapping 长度一致。
    """
    # 快速路径：绝大多数 @Ruby entry 的 reading 不含内嵌时间戳，直接返回避免 regex
    if "[" not in reading:
        text = reading.replace(",", "")
        return [(text, 0)] if text else []

    from strange_uta_game.backend.domain.models import get_ruby_pause_char

    result: List[Tuple[str, int]] = []
    last_end = 0
    pending_offset: Optional[int] = None
    # 连续时间戳之间无读音字符的拍用停顿符占位（禁止空串——隐形且易被
    # 防御性代码误删；禁止空格——导出文件中空格读音是实义字符）。
    # 再导出时 @Ruby 剥离停顿符，round-trip 仍为 `いっ[ts1][ts2]しょ`。
    pause_char = get_ruby_pause_char()

    for m in _READING_TS_RE.finditer(reading):
        # 时间戳之前的文本
        text_before = reading[last_end:m.start()]
        if text_before:
            # 移除逗号分隔符（如果有的话）
            text_before = text_before.replace(",", "")
            if text_before:
                result.append((text_before, pending_offset or 0))
                pending_offset = None
        else:
            # 连续时间戳之间无文本：保留前一个 ts 作为占位 part
            if pending_offset is not None:
                result.append((pause_char, pending_offset))
                pending_offset = None

        # 解析时间戳
        ts_str = m.group(1)
        ts_ms = _parse_nicokara_ts_str(ts_str)
        if ts_ms is not None:
            pending_offset = ts_ms

        last_end = m.end()

    # 处理最后一段文本
    text_after = reading[last_end:]
    if text_after:
        text_after = text_after.replace(",", "")
        if text_after:
            result.append((text_after, pending_offset or 0))
    elif pending_offset is not None:
        # 末尾还残留一个 pending ts，作为占位 part
        result.append((pause_char, pending_offset))

    return result


def _parse_position_range(
    positions: List[str],
) -> Tuple[Optional[int], Optional[int]]:
    """从 @Ruby 条目的 positions 列表解析位置范围

    格式约定（由导出器生成）：
      []              → 无位置，全局条目
      ["", "end"]     → 首个子组：无开始，结束于 end
      ["start"]       → 末尾子组：开始于 start，无结束
      ["start","end"] → 中间子组：开始于 start，结束于 end

    Returns:
        (start_ms, end_ms)，None 表示无边界
    """
    if not positions:
        return None, None

    start_ms: Optional[int] = None
    end_ms: Optional[int] = None

    if len(positions) >= 1:
        start_ms = _parse_nicokara_ts_str(positions[0])
    if len(positions) >= 2:
        end_ms = _parse_nicokara_ts_str(positions[1])

    return start_ms, end_ms


class LyricParserFactory:
    """歌词解析器工厂

    根据文件扩展名自动选择合适的解析器。
    """

    @staticmethod
    def get_parser(file_path: str) -> LyricParser:
        """根据文件路径获取合适的解析器

        Args:
            file_path: 歌词文件路径

        Returns:
            对应的歌词解析器

        Raises:
            ParseError: 不支持的文件格式
        """
        path = Path(file_path)
        ext = path.suffix.lower()

        if ext == ".txt":
            return TXTParser()
        elif ext == ".lrc":
            return LRCParser()
        elif ext == ".kra":
            return KRAParser()
        elif ext == ".ass":
            from .ass_parser import ASSParser

            return ASSParser()
        elif ext == ".srt":
            from .srt_parser import SRTParser

            return SRTParser()
        elif ext == ".ttml":
            from .ttml_parser import TTMLParser

            return TTMLParser()
        else:
            raise ParseError(f"不支持的文件格式: {ext}")

    @staticmethod
    def detect_nicokara(file_path: str) -> bool:
        """检测文件是否为 Nicokara 格式

        Args:
            file_path: 文件路径

        Returns:
            是否为 Nicokara 格式
        """
        path = Path(file_path)
        if not path.exists():
            return False
        try:
            # 公共编码回退链（E1），GBK/Big5 文件同样能被检测
            content, _encoding = decode_lyric_bytes(path.read_bytes())
        except Exception:
            return False
        return NicokaraParser.is_nicokara_format(content)

    @staticmethod
    def parse_file(file_path: str) -> List[ParsedLine]:
        """自动选择解析器并解析文件

        Args:
            file_path: 歌词文件路径

        Returns:
            解析后的行列表
        """
        parser = LyricParserFactory.get_parser(file_path)
        return parser.parse_file(file_path)


def parse_to_sentences(
    parsed_lines: List[ParsedLine],
    singer_id: str,
    singer_name_to_id: Optional[Dict[str, str]] = None,
    *,
    utaten_format: bool = False,
) -> List[Sentence]:
    """将解析结果转换为 Sentence 对象。

    对齐 entities.py 重构后契约（与 `nicokara_result_to_sentences` 同源）：
    1. 多 timestamps 同一字符 → 第二个 ts 自动绑为 sentence_end_ts。
    2. ParsedLine.line_end_ts（如 ASS 末尾 \\k 的尾时长、SRT/ASS 的行级
       End 时间戳）→ 末字符的 sentence_end_ts。文件没有行尾释放事实时
       **不伪造**释放 ts（待测状态）。行尾释放点默认绑定到「连词组尾
       字符」（沿 linked_to_next 向右走到链尾），line_end_bind_last=True
       时直接绑行末字符（SRT/无 \\k 的 ASS 等行级时间轴格式）。
    2b. ParsedLine.gap_pause_map（ASS 非末尾空文本 \\k 段的段起点，
       SUG 断句轴点间隙）→ 对应字符的 sentence_end_ts（句中停顿）。
    3. ParsedLine.ruby_map（如 ASS 的 `汉字|<かな`）→ Character.set_ruby。
       多字 span（span>1）且单一 reading 段（parts_list 长度=1）的场景
       （SUG 导出的 `大冒険|<だいぼうけん` 连词语法）不做均分，整段读音
       挂在 ch0，块内字符全部 linked_to_next=True，保证 round-trip 可还原。
    4. ParsedLine.char_singer_map → Character.singer_id。需要传入
       singer_name_to_id 才会生效；调用方负责把演唱者显示名映射成 id
       （找不到则不应用，回退到 sentence.singer_id）。
    5. 最后强制 set_offset(0) 触发 global_timestamps / global_sentence_end_ts
       派生，否则导出器读不到任何全局时间。

    Args:
        parsed_lines: 解析后的行列表
        singer_id: 演唱者 ID（行级默认）
        singer_name_to_id: 可选，ASS `{\\sing_<name>}` per-char singer 标记
            的「显示名 → Singer.id」映射；为 None 时忽略 char_singer_map

    Returns:
        Sentence 对象列表
    """
    from strange_uta_game.backend.domain import Ruby, RubyPart  # 局部导入避免环依赖

    sentences = []
    name_to_id = singer_name_to_id or {}

    for parsed in parsed_lines:
        # 空行：保留为空 Sentence（用户排版意图）
        if not parsed.text:
            empty_singer_id = singer_id
            if parsed.line_singer_name and name_to_id:
                empty_singer_id = name_to_id.get(
                    parsed.line_singer_name, singer_id
                )
            sentences.append(
                Sentence(
                    singer_id=empty_singer_id,
                    characters=[],
                    song_part=parsed.song_part,
                )
            )
            continue

        sentence = Sentence.from_text(
            text=parsed.text,
            singer_id=singer_id,
        )

        # 按字符分组时间戳（同一字符多个 ts 时第二个作为 sentence_end_ts）
        char_ts_map: Dict[int, List[int]] = {}
        for char_idx, timestamp_ms in parsed.timetags:
            if 0 <= char_idx < len(sentence.characters):
                char_ts_map.setdefault(char_idx, []).append(timestamp_ms)

        last_idx_with_ts: Optional[int] = None
        for char_idx, ts_list in char_ts_map.items():
            char = sentence.characters[char_idx]
            char.add_timestamp(ts_list[0])
            if len(ts_list) >= 2:
                char.is_sentence_end = True
                char.sentence_end_ts = ts_list[1]
                if char.check_count == 0:
                    char.check_count = 1
            if last_idx_with_ts is None or char_idx > last_idx_with_ts:
                last_idx_with_ts = char_idx

        # ASS `#|` 续段给同字追加的额外 checkpoint：每个都 add_timestamp，
        # 让 check_count 自动增长，导出器能据此复原每 part 的 \k 时长。
        for char_idx, extra_ts_list in parsed.extra_checkpoints_map.items():
            if not (0 <= char_idx < len(sentence.characters)):
                continue
            char = sentence.characters[char_idx]
            for ts in extra_ts_list:
                char.add_timestamp(ts)

        # ASS 非末尾空文本 \k 段（SUG 断句轴点间隙）→ 句中停顿释放点：
        # 绑给对应字符的 sentence_end_ts（is_sentence_end=True）。
        # 必须在 line_end_ts 行尾绑定之前执行——行尾绑定只覆写末字。
        for char_idx, pause_ts in (parsed.gap_pause_map or {}).items():
            if not (0 <= char_idx < len(sentence.characters)):
                continue
            char = sentence.characters[char_idx]
            char.is_sentence_end = True
            char.sentence_end_ts = pause_ts
            if char.check_count == 0:
                char.check_count = 1

        # 注入 ruby（统一新签名：(parts_list, span_length)）
        # - span=1, parts=N → 单字多 part（ASS 的 `#|` 续段产物）
        # - span=N, parts=1 → 多字共享 reading（SUG 导出的连词单 reading）
        # - span=1, parts=1 → 标准单字单 part
        for char_idx, ruby_payload in parsed.ruby_map.items():
            if not (0 <= char_idx < len(sentence.characters)):
                continue
            # 兼容旧签名（纯字符串 or 旧 tuple）
            if isinstance(ruby_payload, tuple):
                first, second = ruby_payload
                if isinstance(first, list):
                    parts_list, span_len = first, second
                else:
                    # 旧 tuple (text, span)
                    parts_list, span_len = [first], second
            elif isinstance(ruby_payload, str):
                parts_list, span_len = [ruby_payload], 1
            else:
                continue
            parts_list = [p for p in parts_list if p]
            if not parts_list:
                continue

            span_len = min(max(1, span_len), len(sentence.characters) - char_idx)

            if span_len == 1:
                # 单字 ruby：直接挂 N 个 part
                ch = sentence.characters[char_idx]
                ch.set_ruby(Ruby(parts=[RubyPart(text=p) for p in parts_list]))
                # 若该字有多个 timestamps（来自 extra_checkpoints_map），
                # check_count 自动 = len(timestamps)；push 同步 part offset。
                if ch.check_count < len(parts_list):
                    ch.check_count = len(parts_list)
                ch.push_to_ruby()
            elif len(parts_list) == 1:
                if utaten_format:
                    # UtaTen 标记 LRC：整段读音按字拆分。
                    # - 字典命中（"一字一音"干净对应，如 世界/せかい、国道/こくどう）
                    #   → 每字独立词，linked_to_next 保持 False，编辑器按单字 ruby 显示。
                    # - 字典失配（当て字，如 新時代/はじまり）→ 拆不开，整块按字数均分
                    #   并设 linked_to_next=True，块尾 False。SUG 的 F3"拆词"也是同一思路。
                    from strange_uta_game.backend.infrastructure.parsers.kanji_reading_split import (
                        compute_per_kanji_readings,
                    )

                    block_text = "".join(
                        sentence.characters[char_idx + i].char
                        for i in range(span_len)
                    )
                    per_char_readings, is_ateji = compute_per_kanji_readings(
                        block_text, parts_list[0]
                    )
                    for i in range(span_len):
                        seg_reading = per_char_readings[i] if i < len(per_char_readings) else ""
                        ch = sentence.characters[char_idx + i]
                        if seg_reading:
                            ch.set_ruby(Ruby(parts=[RubyPart(text=seg_reading)]))
                            if ch.check_count < 1:
                                ch.check_count = 1
                            ch.push_to_ruby()
                    if is_ateji:
                        for i in range(span_len - 1):
                            sentence.characters[char_idx + i].linked_to_next = True
                        sentence.characters[char_idx + span_len - 1].linked_to_next = False
                else:
                    # SUG 多字 span 单 reading：整段读音不可拆，挂在 ch0；
                    # 块内字符全部 linked_to_next=True（块尾保持 False）。
                    # 这对应导出器 anchor + compound_tail 的契约，roundtrip 时
                    # 二次导出会还原成 `<整段汉字>|<<整段假名>`。
                    ch0 = sentence.characters[char_idx]
                    ch0.set_ruby(Ruby(parts=[RubyPart(text=parts_list[0])]))
                    if ch0.check_count < 1:
                        ch0.check_count = 1
                    ch0.push_to_ruby()
                    for i in range(span_len - 1):
                        sentence.characters[char_idx + i].linked_to_next = True
                    # 块尾不外延
                    sentence.characters[char_idx + span_len - 1].linked_to_next = False
            else:
                # 多字 span + 多段 reading：保留旧路径（Aegisub 手写罕见用法）。
                # 复用 Nicokara 同款均分实现。
                ruby_text = "".join(parts_list)
                _distribute_reading_to_chars(
                    sentence,
                    char_idx,
                    span_len,
                    [(ruby_text, 0)],
                )
                for i in range(span_len - 1):
                    next_ch = sentence.characters[char_idx + i + 1]
                    sentence.characters[char_idx + i].linked_to_next = not bool(
                        next_ch.timestamps
                    )

        # 显式节奏点数量（TTML 的空 checkpoint span 等扩展格式）。只在
        # 解析结果明确给出时覆盖；既有格式不写 check_count_map，行为不变。
        for char_idx, count in (parsed.check_count_map or {}).items():
            if not (0 <= char_idx < len(sentence.characters)):
                continue
            char = sentence.characters[char_idx]
            if count == 0:
                char.set_check_count(0, force=True)
            elif count > char.check_count:
                char.set_check_count(
                    count, force=True, ruby_split_mode="direct"
                )

        # 显式连词标记（TTML 的 x-sug-link 等扩展格式）。只在解析结果
        # 明确给出时覆盖；既有格式不写 linked_map，行为不变。
        for char_idx, linked in (parsed.linked_map or {}).items():
            if 0 <= char_idx < len(sentence.characters):
                sentence.characters[char_idx].linked_to_next = bool(linked)

        # 行级演唱者（TTML p 的 ttm:agent）：让 Sentence.singer_id 也正确，
        # 而不是只有 Character.singer_id 带歌手。
        if parsed.line_singer_name and name_to_id:
            mapped_line_singer = name_to_id.get(parsed.line_singer_name)
            if mapped_line_singer:
                sentence.singer_id = mapped_line_singer

        # per-char singer：把 char_singer_map 的显示名映射成 id 后赋给 Character.singer_id。
        # 找不到映射的名字静默忽略（调用方负责保证映射齐全；缺失时回退到行级 singer_id）。
        if parsed.char_singer_map and name_to_id:
            for ch_idx, singer_name in parsed.char_singer_map.items():
                if not (0 <= ch_idx < len(sentence.characters)):
                    continue
                mapped = name_to_id.get(singer_name)
                if mapped:
                    sentence.characters[ch_idx].singer_id = mapped

        # 行尾释放点：
        # - line_end_bind_last（SRT/无 \k 的 ASS 等行级格式）：绑到行末字符，
        #   与文件「整行末尾」语义一致，且重分析后行末规则仍标记该字符，
        #   line_end_ts 能被 apply_to_sentence 按位恢复。
        # - 默认（\k 链格式）：沿 linked_to_next 链从 last_idx_with_ts 走到
        #   连词组尾字符——避免「大冒険」末字「険」的 end_ts 丢失。
        if last_idx_with_ts is not None:
            moved_along_chain = False
            if parsed.line_end_bind_last and sentence.characters:
                tail_idx = len(sentence.characters) - 1
            else:
                tail_idx = last_idx_with_ts
                while (
                    tail_idx < len(sentence.characters) - 1
                    and sentence.characters[tail_idx].linked_to_next
                ):
                    tail_idx += 1
                moved_along_chain = tail_idx != last_idx_with_ts
            tail_char = sentence.characters[tail_idx]
            if parsed.line_end_ts is not None:
                tail_char.is_sentence_end = True
                tail_char.sentence_end_ts = parsed.line_end_ts
                if (
                    tail_char.check_count == 0
                    and (parsed.check_count_map or {}).get(tail_idx) != 0
                ):
                    tail_char.check_count = 1
                # last_idx_with_ts 字符若被沿链转移，需要清掉它的 end_ts，
                # 避免「锚字 + 链尾」同时持有 end_ts 导致二次导出多写一段
                # （bind_last 跳绑行末，锚字上的双 ts/句中停顿是文件事实，不清）
                if moved_along_chain:
                    anchor = sentence.characters[last_idx_with_ts]
                    anchor.is_sentence_end = False
                    anchor.sentence_end_ts = None
            # 文件没有行尾释放事实（如逐行 LRC 无行尾标签）时不做兜底：
            # 不凭空伪造 sentence_end_ts——停顿点标记留给 from_text/分析规则，
            # 释放时刻保持「待测」（[>T]），导出侧各自决定缺省策略。

        # 核心：派生 global_timestamps / global_sentence_end_ts，
        # 否则导出器无法读到全局时间（这是旧版 bug）。
        for ch in sentence.characters:
            ch.set_offset(0)

        if parsed.song_part:
            sentence.song_part = parsed.song_part

        sentences.append(sentence)

    return sentences


# 兼容别名
parse_to_lyric_lines = parse_to_sentences
