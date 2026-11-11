"""领域层核心数据结构定义。

数据层次（自底向上）：
  Ruby → Character → Word → Sentence → Project

- Ruby:      最小单元，存储假名文本；checkpoint 时间戳和演唱者由母对象推送
- Character: 主要单元，存储单字、Ruby、节奏点配置、时间戳、连词/停顿点标记、演唱者
- Word:      逻辑单元，由连词字符组成；不存储 Ruby，但收集字符的 Ruby 用于渲染和输出
"""

from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum, auto


# ──────────────────────────────────────────────
# 错误类
# ──────────────────────────────────────────────


class DomainError(Exception):
    """领域层错误基类"""

    pass


class ValidationError(DomainError):
    """验证错误"""

    pass


class RubyMoraDegradeError(DomainError):
    """缩减 check_count 至 0 会使 ruby 从有 mora 退化为无 mora 格式。

    语义：当 check_count > 0 时，每个 RubyPart 拥有独立 offset_ms，渲染按 mora 走字；
          当 check_count == 0 时，整段 ruby 跟随父 Character 时间戳渲染（Nicokara 无 mora 格式）。
    抛出场景：set_check_count(0) 且 ruby 非空且 force=False。
    调用方（前端层）应捕获后弹窗告知用户「将退化为无 mora 格式」，
    用户确认后用 force=True 重调；ruby 文本数据完整保留，仅渲染粒度改变。
    """

    pass


# 历史别名（保持向后兼容，可后续清理）
RubyDataLossError = RubyMoraDegradeError


# ──────────────────────────────────────────────
# 枚举
# ──────────────────────────────────────────────


# 标点符号集合（不参与注音但可加节奏点）
PUNCTUATION_SET = frozenset('''()【】[]{}「」!?、，"' ''')


# ──────────────────────────────────────────────
# Ruby 占位符（演唱停顿符）
# ──────────────────────────────────────────────

# .sug 存储用哨兵 token。占位符在文件中统一写为该 token，使存档与用户的
# 停顿符配置解耦：换了配置再打开旧文件，占位符自动映射为新配置的字符。
RUBY_PAUSE_SENTINEL = "^pause^"

_FULLWIDTH_CARET = "＾"  # ＾
_ASCII_CARET = "^"


def get_ruby_pause_char() -> str:
    """当前会话的注音占位符（= nicokara 演唱停顿符）。

    check_count 多于读音 mora 数时，多出的节奏点用本字符占位，表示
    「该拍无新文字（延音/停顿）」。禁止用空串（隐形、易被防御性代码误删，
    曾导致 checkcount/rubyparts 失配）或空格（导出文件中空格读音是实义字符）。

    领域层读取前端 AppSettings 是刻意取舍：占位符必须与导出剥离、编辑显示
    使用同一字符。无配置 / 配置为空 / 读取异常时回退 '^'。
    """
    try:
        from strange_uta_game.frontend.settings.app_settings import AppSettings

        ch = AppSettings().get("export.nicokara_pause_char", _ASCII_CARET)
        return ch if ch else _ASCII_CARET
    except Exception:
        return _ASCII_CARET


def pause_char_variants(pause_char: str) -> set:
    """返回停顿符及其全角/半角变体集合。"""
    variants = {pause_char}
    if pause_char == _ASCII_CARET:
        variants.add(_FULLWIDTH_CARET)
    elif pause_char == _FULLWIDTH_CARET:
        variants.add(_ASCII_CARET)
    return variants


def strip_ruby_pause_chars(text: str) -> str:
    """移除 ruby 文本中的停顿符占位（连同全/半角变体）。

    占位 part 表示「该拍无新文字（延音/停顿）」，是应用内部表示；
    导出给播放器的带注音格式（Nicokara @Ruby、ASS 注音、春日向/
    Kirakara）不得把它泄漏给消费方——剥离后该拍退化为纯时间戳段
    （如 ``す[ts][ts]``），导入侧再把空段还原为占位符。RL 编辑模式
    面向打轴软件（RhythmicaLyrics），占位符在该侧有意义，不剥离。
    """
    if not text:
        return text
    for pc in pause_char_variants(get_ruby_pause_char()):
        text = text.replace(pc, "")
    return text


class TimeTagType(Enum):
    """时间标签类型（用于导出兼容）

    在新层次模型中，tag_type 由上下文推导：
      - CHAR_START   : Character.timestamps[0]
      - CHAR_MIDDLE  : Character.timestamps[1:]（非停顿点字符）
      - LINE_END     : is_line_end 字符的最后一个 timestamp
      - SENTENCE_END : is_sentence_end 字符的最后一个 timestamp
      - REST         : is_rest 字符的 timestamp
    """

    CHAR_START = auto()
    CHAR_MIDDLE = auto()
    LINE_END = auto()
    SENTENCE_END = auto()
    REST = auto()


# ──────────────────────────────────────────────
# Ruby — 最小数据结构单元
# ──────────────────────────────────────────────


@dataclass
class RubyPart:
    """Ruby 的一个分段，对应父 Character 的一个 checkpoint。

    Attributes:
        text: 分段文本（如单个 mora "か"；英文 reading 整段 "Ro"）
        offset_ms: 相对父 Character 起始时间戳的偏移（毫秒）。
                   由 Character.push_to_ruby 推送时计算写入，Nicokara 内嵌时间戳导出使用。
    """

    text: str
    offset_ms: int = 0


@dataclass
class Ruby:
    """注音/振り仮名 — 最小数据结构单元

    由 List[RubyPart] 组成；每个 part 对应父 Character 的一个 checkpoint。
    时间戳 (timestamps) 和演唱者 (singer_id) 由父对象 (Character) 推送更新。

    Attributes:
        parts: Ruby 分段列表
               - 有 mora 模式：len(parts) == character.check_count，每段独立 offset_ms
               - 无 mora 模式：character.check_count == 0 时整段 ruby 跟父字符渲染
                 （Nicokara 无 mora 格式，文本完整保留）
        timestamps: checkpoint 绝对时间戳列表（毫秒，Character 推送，渲染使用）
        singer_id: 演唱者 ID（Character 推送）

    Example:
        >>> ruby = Ruby(parts=[RubyPart("あ"), RubyPart("か")])
        >>> ruby.text
        'あか'
        >>> [p.text for p in ruby.parts]
        ['あ', 'か']
    """

    parts: List[RubyPart] = field(default_factory=list)
    timestamps: List[int] = field(default_factory=list)
    singer_id: str = ""

    def __post_init__(self) -> None:
        if not self.parts:
            raise ValidationError("Ruby 分段不能为空")
        if any(not isinstance(p, RubyPart) for p in self.parts):
            raise ValidationError("Ruby.parts 元素必须是 RubyPart")

    @property
    def text(self) -> str:
        """用户可见的拼接文本（所有 part 按序拼接）。"""
        return "".join(p.text for p in self.parts)


# ──────────────────────────────────────────────
# Character — 主要数据结构单元
# ──────────────────────────────────────────────


@dataclass
class Character:
    """字符 — 主要数据结构单元

    存储单个字符及其注音、节奏点配置、时间戳、连词/停顿点标记、演唱者。
    当时间戳或演唱者更新时，主动将变更推送给 Ruby 对象。

    Attributes:
        char: 单个字符（如 "赤"、"い"）
        ruby: 注音对象，可以为空
        check_count: 节奏点数量（需要击打几次，默认 1，可以为 0）
        timestamps: checkpoint 时间戳列表（毫秒），索引 = checkpoint_idx
        linked_to_next: 是否与下一字符连词
        is_line_end: 是否是行尾字符（行级换行标记，一行只有一个）
        is_sentence_end: 是否是停顿点字符（停顿点标记，一行内可有多个，额外 +1 checkpoint）
        is_rest: 是否是休止符
        singer_id: 演唱者 UUID

    Example:
        >>> ch = Character(char="赤", check_count=2, singer_id="singer_1")
        >>> ch.set_ruby(Ruby(parts=[RubyPart(text="あか")]))
        >>> ch.add_timestamp(1000)
        >>> ch.ruby.timestamps
        [1000]
    """

    char: str
    ruby: Optional[Ruby] = None
    check_count: int = 1
    timestamps: List[int] = field(default_factory=list)
    sentence_end_ts: Optional[int] = None
    # linked_to_next: 是否与下一字组成"连词"（语义上的复合词），
    # 是连词信息的**唯一**权威载体。@RubyN tag 内多字合并、
    # editor 的 {...||...} 序列化、N3 解析回灌均以此字段为准。
    linked_to_next: bool = False
    is_line_end: bool = False
    # is_sentence_end: ⚠ 命名遗留 —— 真实语义是「**演唱时的呼吸/语句停顿**」，
    # **不是语义层面的句末**。用于：
    #   - LRC/Nicokara 行末或句中停顿点的释放时间戳输出
    #   - ASS/txt2ass 行结束时刻计算
    # **不应**用来判断：
    #   - 是否构成连词（用 linked_to_next）
    #   - 语义句子边界（本字段做不到这件事）
    # 因重构成本过高未改名，所有调用点请按"演唱停顿"语义解读。
    is_sentence_end: bool = False
    is_rest: bool = False
    singer_id: str = ""

    # 导唱待办标记：用户在此字符前缺一段导唱符的提醒。
    # 不参与时间戳/ruby 不变式，不推送给 Ruby，不影响导出内容；
    # 仅用于 KaraokePreview 视觉提示 + 导出前弹窗汇总。
    needs_guide: bool = field(default=False, compare=False)

    # 实际导唱字符标记。与 needs_guide（待办）不同，本字段表示该字符本身
    # 就是导唱块的一部分；不参与时间戳/ruby 不变式，也不影响正式导出。
    is_guide: bool = field(default=False, compare=False)

    # 强制输出演唱者标签：即使与前一字演唱者相同，也在此字符前插入
    # 【演唱者名】标签。用于全文本编辑器手动强制插入/冗余标签的场景。
    force_singer_tag: bool = field(default=False, compare=False)

    # 选中的 checkpoint 索引（全局单选不变量由 Project 层管理；None = 未选中）
    # 不参与 .sug 序列化——选中态是 UI 状态，不跨会话持久化。
    selected_checkpoint_idx: Optional[int] = field(default=None, compare=False)

    # 全局偏移量（内部管理，不参与构造和序列化）
    _global_offset_ms: int = field(default=0, init=False, repr=False)

    # 派生偏移时间戳（自动维护，不参与构造和序列化）
    global_timestamps: List[int] = field(default_factory=list, init=False, repr=False)
    global_sentence_end_ts: Optional[int] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.char:
            raise ValidationError("字符不能为空")
        if self.check_count < 0:
            raise ValidationError(f"节奏点数量不能为负数: {self.check_count}")
        # 初始化渲染/导出偏移时间戳（确保从文件加载的 Character 也有正确的派生数据）
        self._update_offset_timestamps()

    # ── 时间戳管理 ──

    def push_to_ruby(self) -> None:
        """将自己的时间戳、演唱者推送给 Ruby；同时计算每个 RubyPart 的 offset_ms。

        offset_ms 相对父 Character 的第一个时间戳（timestamps[0]）。
        - 若 part 索引 i < len(timestamps)：写入 timestamps[i] - base_ts
        - 否则（未打轴的尾部 part）：清零，避免删 timestamp 后旧 offset 残留
        """
        if not self.ruby:
            return
        self.ruby.timestamps = self.all_timestamps
        self.ruby.singer_id = self.singer_id
        base_ts = self.timestamps[0] if self.timestamps else 0
        for i, part in enumerate(self.ruby.parts):
            if i < len(self.timestamps):
                part.offset_ms = self.timestamps[i] - base_ts
            else:
                part.offset_ms = 0

    def add_timestamp(self, timestamp_ms: int, checkpoint_idx: int = -1) -> None:
        """添加时间戳

        Args:
            timestamp_ms: 时间戳（毫秒）
            checkpoint_idx: 指定写入的 checkpoint 索引（-1 = 追加到末尾）

        Raises:
            ValidationError: 时间戳为负、索引越界，或稀疏写入目标位之前
                无任何时间戳可回填空位
        """
        if timestamp_ms < 0:
            raise ValidationError(f"时间戳不能为负数: {timestamp_ms}")
        if checkpoint_idx >= self.check_count:
            raise ValidationError("普通节奏点索引超出范围")
        if checkpoint_idx >= 0:
            # 稀疏打轴（跳过前面 cp 直接写后位）需要回填空位：
            # 用前一 cp 的时间戳填充，避免产生破坏单调性的 0ms 伪时间戳；
            # 无前一 cp 时间戳可回填（timestamps 为空且目标位 > 0）时拒绝写入。
            if len(self.timestamps) < checkpoint_idx:
                if not self.timestamps:
                    raise ValidationError(
                        f"稀疏打轴被拒绝：cp {checkpoint_idx} 之前的节奏点尚未打轴，"
                        "无时间戳可回填空位"
                    )
                self.timestamps.extend(
                    [self.timestamps[-1]] * (checkpoint_idx - len(self.timestamps))
                )
            if len(self.timestamps) == checkpoint_idx:
                # 目标位正好是下一个空槽（含首写 len == checkpoint_idx == 0）
                self.timestamps.append(timestamp_ms)
            else:
                self.timestamps[checkpoint_idx] = timestamp_ms
        else:
            self.timestamps.append(timestamp_ms)
        self._update_offset_timestamps()
        self.push_to_ruby()

    def remove_timestamp_at(self, checkpoint_idx: int) -> Optional[int]:
        """移除指定 checkpoint_idx 的时间戳

        Args:
            checkpoint_idx: checkpoint 索引

        Returns:
            被移除的时间戳，如果索引无效返回 None
        """
        if checkpoint_idx >= self.check_count:
            return None
        if 0 <= checkpoint_idx < len(self.timestamps):
            removed = self.timestamps.pop(checkpoint_idx)
            self._update_offset_timestamps()
            self.push_to_ruby()
            return removed
        return None

    def clear_timestamps(self) -> None:
        """清空所有时间戳"""
        self.timestamps.clear()
        self.sentence_end_ts = None
        self._update_offset_timestamps()
        self.push_to_ruby()

    def set_sentence_end_ts(self, ts: int) -> None:
        """设置停顿点时间戳"""
        if ts < 0:
            raise ValidationError(f"时间戳不能为负数: {ts}")
        if not self.is_sentence_end:
            raise ValidationError("当前字符未标记为停顿点")
        self.sentence_end_ts = ts
        self._update_offset_timestamps()
        self.push_to_ruby()

    def clear_sentence_end_ts(self) -> None:
        """清除停顿点时间戳"""
        self.sentence_end_ts = None
        self._update_offset_timestamps()
        self.push_to_ruby()

    def set_check_count(
        self, new_count: int, *, force: bool = False, ruby_split_mode: str = "mora"
    ) -> None:
        """权威 setter：变更 check_count 并维护 timestamps / ruby.parts 不变式。

        不变式：
          - len(timestamps) <= check_count
          - check_count >= 1 时 len(ruby.parts) == check_count（有 mora 格式）
          - check_count == 0 且 ruby 非空：退化为 Nicokara 无 mora 格式
            （ruby 文本完整保留，整段跟随父字符 timestamp 渲染）

        缩小行为：
          - timestamps 截断到 new_count
          - new_count >= 1 且 ruby.parts 超出：合并尾段
            （示例：parts=[A,B,C,D], new_count=2 → parts=[A, B+C+D]）
          - new_count == 0 且 ruby 非空：
              * force=False → 抛 RubyMoraDegradeError（调用方应弹窗告知用户退化）
              * force=True  → 保留 ruby 整段（Nicokara 无 mora 格式，文本不丢失）

        放大行为：
          - ruby_split_mode="direct"：保留现有 parts 不变，新增部分追加停顿符
          - ruby_split_mode="char"：按字符重新拆分现有 ruby 文本
          - ruby_split_mode="mora"：按 mora 重新拆分现有 ruby 文本（默认）

        Args:
            new_count: 新的节奏点数量（>= 0）
            force: 允许 new_count==0 时退化为无 mora 格式
            ruby_split_mode: 放大时 ruby 分段方式 ("direct"/"char"/"mora")

        Raises:
            ValidationError: new_count < 0
            RubyMoraDegradeError: new_count==0 且 ruby 非空且 !force
        """
        if new_count < 0:
            raise ValidationError(f"节奏点数量不能为负数: {new_count}")

        # mora 退化告知保护
        if (
            new_count == 0
            and self.ruby is not None
            and len(self.ruby.parts) > 0
            and not force
        ):
            raise RubyMoraDegradeError(
                f"将 check_count 减至 0 会使字符 '{self.char}' 的注音从有 mora 退化为"
                f"Nicokara 无 mora 格式（ruby 文本保留），需用户确认后传入 force=True"
            )

        old_count = self.check_count
        self.check_count = new_count

        # 缩小：trim timestamps + 合并 ruby.parts 尾段
        if new_count < old_count:
            if len(self.timestamps) > new_count:
                self.timestamps = self.timestamps[:new_count]
            if (
                self.ruby is not None
                and new_count >= 1
                and len(self.ruby.parts) > new_count
            ):
                # 合并 parts[new_count-1:] 到 parts[new_count-1]
                keep = self.ruby.parts[: new_count - 1]
                merged_text = "".join(
                    p.text for p in self.ruby.parts[new_count - 1 :]
                )
                merged_part = RubyPart(
                    text=merged_text,
                    offset_ms=self.ruby.parts[new_count - 1].offset_ms,
                )
                self.ruby.parts = keep + [merged_part]
            # new_count == 0 且 force=True：ruby 整段保留（Nicokara 无 mora 格式）

        # 放大：按 ruby_split_mode 重新拆分 ruby.parts 以维持不变式
        if new_count > old_count and self.ruby is not None and self.ruby.parts:
            if ruby_split_mode == "direct":
                pause = get_ruby_pause_char()
                while len(self.ruby.parts) < new_count:
                    self.ruby.parts.append(RubyPart(text=pause))
            else:
                existing_text = "".join(p.text for p in self.ruby.parts)
                if existing_text:
                    new_parts = Character._resplit_ruby(
                        existing_text, new_count, ruby_split_mode
                    )
                    self.ruby.parts = [RubyPart(text=t) for t in new_parts]

        # 不变式收口：无论以何参数调用（含 new_count == old_count 的修复式调用），
        # 离开本方法时必须满足 new_count >= 1 → len(ruby.parts) == new_count。
        # 旧版存档丢失占位 part、或绕过本 setter 的历史写点造成的失配在此修复：
        # 不足补占位符（停顿符），多余合并尾段。
        if (
            self.ruby is not None
            and new_count >= 1
            and len(self.ruby.parts) != new_count
        ):
            if len(self.ruby.parts) < new_count:
                pause = get_ruby_pause_char()
                self.ruby.parts = self.ruby.parts + [
                    RubyPart(text=pause)
                    for _ in range(new_count - len(self.ruby.parts))
                ]
            else:
                keep = self.ruby.parts[: new_count - 1]
                merged_text = "".join(
                    p.text for p in self.ruby.parts[new_count - 1 :]
                )
                self.ruby.parts = keep + [
                    RubyPart(
                        text=merged_text,
                        offset_ms=self.ruby.parts[new_count - 1].offset_ms,
                    )
                ]

        self._update_offset_timestamps()
        self.push_to_ruby()

    # ── 内部工具 ──

    _SMALL_KANA = set("ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮー")

    @staticmethod
    def _resplit_ruby(text: str, target_count: int, mode: str) -> List[str]:
        """按指定模式将 ruby 文本重新拆分为 target_count 段。

        mode:
          - "direct": 保留原文不分割，不足部分用占位符（停顿符）补齐
          - "char": 按读音单位（字素簇）均分——附加符不与基字拆开
          - "mora": 按 mora/音节均分（小假名、IPA 修饰符附属前一拍，
            含 IPA 特有字符时按音节拆分）

        Returns:
            长度为 target_count 的字符串列表
        """
        if target_count <= 0:
            return []
        if target_count == 1:
            return [text]

        pause = get_ruby_pause_char()
        if mode == "direct":
            return [text] + [pause] * (target_count - 1)

        # char / mora 模式都先去除逗号
        clean = text.replace(",", "")
        if not clean:
            return [pause] * target_count

        from strange_uta_game.backend.infrastructure.parsers.inline_format import (
            distribute_ruby_chars_evenly,
            split_into_moras,
            split_ruby_units,
        )

        if mode == "char":
            return distribute_ruby_chars_evenly(split_ruby_units(clean), target_count)

        # mode == "mora"
        moras = split_into_moras(clean)
        if len(moras) >= target_count:
            head = moras[: target_count - 1]
            tail = "".join(moras[target_count - 1 :])
            return head + [tail]
        return moras + [pause] * (target_count - len(moras))

    def get_timestamp(self, checkpoint_idx: int) -> Optional[int]:
        """获取指定 checkpoint_idx 的时间戳"""
        if 0 <= checkpoint_idx < len(self.timestamps):
            return self.timestamps[checkpoint_idx]
        return None

    # ── Ruby 管理 ──

    def set_ruby(self, ruby: Optional[Ruby]) -> None:
        """设置 Ruby 并推送当前时间戳和演唱者"""
        self.ruby = ruby
        self.push_to_ruby()

    # ── 查询 ──

    @property
    def is_fully_timed(self) -> bool:
        """检查是否所有节奏点都已打轴"""
        normal_done = len(self.timestamps) >= self.check_count
        if not normal_done:
            return False
        if self.is_sentence_end:
            return self.sentence_end_ts is not None
        return True

    @property
    def total_timing_points(self) -> int:
        """总打轴点数（普通 checkpoint + 停顿点）"""
        return self.check_count + (1 if self.is_sentence_end else 0)

    @property
    def all_timestamps(self) -> List[int]:
        """按打轴顺序返回所有时间戳（只读视图）"""
        sentence_end = (
            [self.sentence_end_ts]
            if self.is_sentence_end and self.sentence_end_ts is not None
            else []
        )
        return list(self.timestamps) + sentence_end

    @property
    def has_ruby(self) -> bool:
        """是否有注音"""
        return self.ruby is not None

    @property
    def is_punctuation(self) -> bool:
        """是否是标点符号（不参与注音但可加节奏点）"""
        return self.char in PUNCTUATION_SET

    def is_sentence_end_tail_cp(self, cp_idx: int) -> bool:
        """判定 cp_idx 是否为本字符的"停顿点 cp"。

        语义：停顿点字符在 check_count 个普通 cp 之后追加 1 个虚拟 cp（索引 = check_count），
        该虚拟 cp 仅响应打轴键的 released 事件；普通 cp 仅响应 pressed 事件。

        Args:
            cp_idx: 待判定的 cp 索引（all_timestamps 域）

        Returns:
            True 表示该 cp 是停顿点 cp（仅 released 写入）；False 表示普通 cp（仅 pressed 写入）。
        """
        if not self.is_sentence_end:
            return False
        return cp_idx == self.check_count

    def get_tag_type(self, checkpoint_idx: int) -> TimeTagType:
        """根据上下文推导时间标签类型

        Args:
            checkpoint_idx: checkpoint 索引

        Returns:
            推导出的 TimeTagType
        """
        if self.is_rest:
            return TimeTagType.REST
        if self.is_sentence_end and checkpoint_idx == self.total_timing_points - 1:
            return TimeTagType.SENTENCE_END
        if self.is_line_end and checkpoint_idx == self.check_count - 1:
            return TimeTagType.LINE_END
        if checkpoint_idx == 0:
            return TimeTagType.CHAR_START
        return TimeTagType.CHAR_MIDDLE

    # ── 偏移时间戳管理 ──

    def set_offset(self, offset_ms: int) -> None:
        """设置全局偏移量并重新计算派生时间戳

        Args:
            offset_ms: 偏移量（毫秒），负值=提前
        """
        self._global_offset_ms = offset_ms
        self._update_offset_timestamps()

    def _update_offset_timestamps(self) -> None:
        """根据基础时间戳和偏移量重新计算全局时间戳"""
        self.global_timestamps = [
            max(0, ts + self._global_offset_ms) for ts in self.timestamps
        ]
        self.global_sentence_end_ts = (
            max(0, self.sentence_end_ts + self._global_offset_ms)
            if self.sentence_end_ts is not None
            else None
        )

    @property
    def all_global_timestamps(self) -> List[int]:
        """按打轴顺序返回所有全局时间戳（带偏移）"""
        sentence_end = (
            [self.global_sentence_end_ts]
            if self.is_sentence_end and self.global_sentence_end_ts is not None
            else []
        )
        return list(self.global_timestamps) + sentence_end


# ──────────────────────────────────────────────
# Word — 逻辑单元（由连词字符组成）
# ──────────────────────────────────────────────


@dataclass
class Word:
    """词语 — 由连词字符组成的逻辑单元

    通过日语词典、英语词典自动语义分割，或用户手动 F3 toggle。
    如果字符的 linked_to_next=True，则与下一字符组成同一词语；
    如果没有连词，单个字符即为一个词语。

    Word 不存储 Ruby，但会把字符的 Ruby 收集起来，
    用于绘制连词框、解析和最终输出（逗号分隔）。

    Attributes:
        characters: 组成该词语的字符列表
    """

    characters: List[Character] = field(default_factory=list)

    @property
    def text(self) -> str:
        """词语文本"""
        return "".join(c.char for c in self.characters)

    @property
    def ruby_parts(self) -> List[str]:
        """各字符 Ruby 的分段文本列表（按 checkpoint 顺序展开）。"""
        return [p.text for c in self.characters if c.ruby for p in c.ruby.parts]

    @property
    def ruby_text(self) -> str:
        """合并的 Ruby 文本（用于渲染连词框）"""
        return "".join(self.ruby_parts)

    @property
    def ruby_csv(self) -> str:
        """逗号分隔的 Ruby 文本（用于输出）"""
        return ",".join(self.ruby_parts)

    @property
    def has_ruby(self) -> bool:
        """词语中是否包含 Ruby"""
        return any(c.ruby for c in self.characters)

    @property
    def char_count(self) -> int:
        """字符数量"""
        return len(self.characters)
