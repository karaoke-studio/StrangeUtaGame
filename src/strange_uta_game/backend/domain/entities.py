"""领域层实体定义。

实体(Entity)具有唯一标识和生命周期，可以被创建、修改、持久化。
"""

import colorsys
from dataclasses import dataclass, field
from typing import List, Optional
from uuid import uuid4

from .models import (
    Character,
    Ruby,
    Word,
    DomainError,
    ValidationError,
)
from .graphemes import (
    grapheme_boundaries,
    split_graphemes,
    starts_with_extender,
    is_dependent_text,
)


def _compute_complement_color(hex_color: str) -> str:
    """计算 HSV 色相旋转 180° 后的补色（保持 S/V 不变）。

    用于选中高亮渲染：和演唱者基础色在色相环上对立，保证对比度。
    纯灰色（S=0）无意义色相，直接返回原色。

    Args:
        hex_color: 形如 "#RRGGBB" 的十六进制色。

    Returns:
        形如 "#RRGGBB" 的补色；若输入无效或为灰度返回原值。
    """
    if not hex_color or not hex_color.startswith("#") or len(hex_color) != 7:
        return hex_color
    try:
        r = int(hex_color[1:3], 16) / 255.0
        g = int(hex_color[3:5], 16) / 255.0
        b = int(hex_color[5:7], 16) / 255.0
    except ValueError:
        return hex_color
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s <= 1e-6:
        return hex_color
    h2 = (h + 0.5) % 1.0
    r2, g2, b2 = colorsys.hsv_to_rgb(h2, s, v)
    return "#{:02X}{:02X}{:02X}".format(
        int(round(r2 * 255)), int(round(g2 * 255)), int(round(b2 * 255))
    )


@dataclass
class Singer:
    """演唱者/角色

    表示一个演唱者或角色（用于多声部和声场景）。

    Attributes:
        id: 唯一标识符（UUID）
        name: 演唱者名称（如 "初音ミク"、"合唱"、"和声"）
        color: 主显示颜色（#RRGGBB，始终有效）
        complement_color: 自动计算的补色（选中高亮用）
        color_mode: 颜色模式，"solid"（单色）或 "split"（分色）
        split_colors: 分色模式下的额外颜色列表（colors[1..4]，最多4项使总数≤5）
        is_default: 是否为默认演唱者
        is_placeholder: 是否为软件初始占位演唱者（用户修改后自动清除）
        display_priority: 显示优先级（数字越小越优先显示）
        enabled: 是否启用（禁用的演唱者不参与全局序列）
        backend_number: 后台编号（从1开始递增，不随显示名改变）

    Example:
        >>> singer = Singer(name="初音ミク", color="#FF6B6B")
        >>> singer.name
        '初音ミク'
    """

    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = "未命名"
    color: str = "#FF6B6B"
    complement_color: str = ""
    color_mode: str = "solid"
    split_colors: List[str] = field(default_factory=list)
    backend_number: int = 0
    is_default: bool = False
    is_placeholder: bool = False
    display_priority: int = 0
    enabled: bool = True
    group: str = ""
    # ── TTML 来源 agent（仅从 TTML 导入时填写；其他格式保持空） ──
    # 保存源文件的 xml:id / type / 是否有 ttm:name，供导出时原样回写。
    # ``ttml_agent_named`` 仅在源 agent 存在且原本有名字时为 True；无名
    # agent 被用户重命名后由 rename() 置为 True，导出才会真正写入名字。
    ttml_agent_id: str = ""
    ttml_agent_type: str = ""
    ttml_agent_named: bool = True
    # 导入时使用的展示名（同名 agent 会加 id 后缀）与源 ttm:name 原文。
    # 导出时只有用户改过展示名才写 Singer.name，否则回写 source_name。
    ttml_agent_display_name: str = ""
    ttml_agent_source_name: str = ""
    # 辅助角色演唱者（翻译/音译/和声等）：非空表示该 Singer 是导入 TTML
    # 辅助轨时自动建立的角色，不参与普通 agent 表；导出时由所属 Sentence
    # 的 aux_role 决定写 x-* span。
    ttml_role: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raise ValidationError("演唱者ID不能为空")
        if not self.name:
            raise ValidationError("演唱者名称不能为空")
        if not self.color.startswith("#") or len(self.color) != 7:
            raise ValidationError(f"颜色格式无效: {self.color} (应为 #RRGGBB)")
        # 兼容旧数据：color_mode 缺失或非法时默认 solid
        if self.color_mode not in ("solid", "split"):
            self.color_mode = "solid"
        # 过滤非法的 split_colors 条目，最多保留 4 个（总颜色数 ≤ 5）
        self.split_colors = [
            c for c in self.split_colors
            if c and c.startswith("#") and len(c) == 7
        ][:4]
        # 自动补算补色：持久化但不强制用户可见。
        # 仅在字段为空或与当前 color 不兼容时重算，保证 .sug 旧文件向后兼容。
        if not self.complement_color or not self.complement_color.startswith("#") or len(self.complement_color) != 7:
            self.complement_color = _compute_complement_color(self.color)

    def get_all_colors(self) -> List[str]:
        """返回所有颜色列表。

        solid 模式返回 [color]，split 模式返回 [color] + split_colors。
        """
        if self.color_mode == "split" and self.split_colors:
            return [self.color] + list(self.split_colors)
        return [self.color]

    def rename(self, new_name: str) -> None:
        """重命名演唱者"""
        if not new_name:
            raise ValidationError("演唱者名称不能为空")
        self.name = new_name
        self.is_placeholder = False
        # 来自 TTML 的无名 agent：一旦用户重命名，导出时就应写入这个新名字；
        # 否则仍保留源文件的“无 ttm:name”状态，避免 round-trip 凭空多出名字。
        if self.ttml_agent_id and not self.ttml_agent_named:
            self.ttml_agent_named = True

    def adopt_ttml_agent(self, other: "Singer") -> None:
        """复制另一个演唱者的 TTML agent 来源信息（导入合并时用）。"""
        self.ttml_agent_id = other.ttml_agent_id
        self.ttml_agent_type = other.ttml_agent_type
        self.ttml_agent_named = other.ttml_agent_named
        self.ttml_agent_display_name = other.ttml_agent_display_name
        self.ttml_agent_source_name = other.ttml_agent_source_name
        self.ttml_role = other.ttml_role

    def change_color(
        self,
        new_color: str,
        color_mode: str = None,
        split_colors: List[str] = None,
    ) -> None:
        """修改颜色设置（同步更新补色）"""
        if not new_color.startswith("#") or len(new_color) != 7:
            raise ValidationError(f"颜色格式无效: {new_color}")
        self.color = new_color
        self.complement_color = _compute_complement_color(new_color)
        if color_mode is not None:
            if color_mode not in ("solid", "split"):
                raise ValidationError(f"颜色模式无效: {color_mode}")
            self.color_mode = color_mode
        if split_colors is not None:
            self.split_colors = [
                c for c in split_colors
                if c and c.startswith("#") and len(c) == 7
            ][:4]
        self.is_placeholder = False

    def set_enabled(self, enabled: bool) -> None:
        """设置启用状态"""
        self.enabled = enabled
        self.is_placeholder = False


@dataclass
class AxisGroup:
    """轴分组 — 分色分轴计划中的一组。

    嵌入式宿主「进入下一步」流程与 standalone 按组拆分导出使用：同一
    SUG 项目可按演唱者（分色）拆成多个轴文件，一个 AxisGroup 描述进入
    一个轴的演唱者集合。

    约束：
        - 组内 singer_ids 引用 Project.singers 中的演唱者；
        - **singer_ids 为空 = 全部演唱者**（沿用过滤器「不勾选则导出全部」
          口径；由导出页/对话框产生，消费侧把空集解释为不筛选）；
        - 一个演唱者可同时属于多个组（其文本同时进入多个轴）；
        - 不强求覆盖全部演唱者 — 未入组的演唱者文本不进入任何轴；
        - 多组时必须且只能有一个主分组（``is_primary``，由 Project 归一
          化保证）：主分组的导出文件携带完整标签信息（@Title 等 +
          非歌手 custom 标签），其余组只带本组实际演唱者的 @Emoji 标签；
        - 组内内容口径与 Nicokara 导出的演唱者过滤一致：行内任一字符
          属于组内演唱者则保留整行（空行无条件保留），行内组外字符剔除。

    Example:
        >>> group = AxisGroup(name="轴1", singer_ids=["uuid-a", "uuid-b"])
        >>> group.name
        '轴1'
    """

    name: str = ""
    singer_ids: List[str] = field(default_factory=list)
    is_primary: bool = False

    def __post_init__(self) -> None:
        self.name = str(self.name or "").strip()
        # 去重但保持顺序（重复 ID 无意义且会让宿主侧分组统计翻倍）
        seen = set()
        self.singer_ids = [
            sid for sid in self.singer_ids
            if sid and not (sid in seen or seen.add(sid))
        ]

    def contains_singer(self, singer_id: str) -> bool:
        """该演唱者是否属于本组。"""
        return singer_id in self.singer_ids

    def to_dict(self) -> dict:
        """转换为字典（用于 .sug 序列化）。"""
        return {
            "name": self.name,
            "singer_ids": list(self.singer_ids),
            "is_primary": self.is_primary,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AxisGroup":
        """从字典创建（容忍缺字段/非法类型，坏条目回退默认值）。"""
        if not isinstance(data, dict):
            return cls()
        raw_ids = data.get("singer_ids") or []
        if not isinstance(raw_ids, list):
            raw_ids = []
        return cls(
            name=str(data.get("name", "") or ""),
            singer_ids=[str(sid) for sid in raw_ids if sid],
            is_primary=bool(data.get("is_primary", False)),
        )


@dataclass
class AuxTrack:
    """TTML 解析期的一棵辅助轨树（导入后转成独立 Sentence）。

    SUG 的最终数据模型里，翻译、音译、和声是独立的 ``Sentence`` 行并分配
    角色 Singer；:class:`AuxTrack` 只作为解析中间结构，保留源 span 的
    文本、role、语言、时间与嵌套子 span，供 ``build_ttml_sentences`` 转换。

    * 翻译 / 音译通常是叶子节点：``role="x-translation"`` / ``"x-roman"``；
    * 背景人声可以是叶子或带逐词子 span 的容器：``role="x-bg"``；
    * 未知 ``x-*`` role 同样保留，不静默丢弃。

    :attr:`text` 只保存该元素自身的直接文本；子节点在 :attr:`children`。
    用 :attr:`plain_text` 取包含所有子节点的完整文本。

    Attributes:
        role: 源 ``ttm:role`` 属性（如 ``"x-translation"``；空 = 结构子 span）
        text: 元素直接文本
        lang: ``xml:lang``（翻译常见 ``zh-CN``）
        begin: 元素开始时间（毫秒，可空）
        end: 元素结束时间（毫秒，可空）
        agent: 源 ``ttm:agent`` 的 xml:id（可空）
        children: 子 span 的辅助轨节点（背景人声逐词时间轴）
    """

    role: str = ""
    text: str = ""
    lang: str = ""
    begin: Optional[int] = None
    end: Optional[int] = None
    agent: str = ""
    children: List["AuxTrack"] = field(default_factory=list)

    @property
    def roles(self) -> tuple:
        """拆开 ``role`` 属性中的多个 role token。"""
        return tuple(part for part in str(self.role or "").split() if part)

    def has_role(self, role: str) -> bool:
        """是否包含指定 role token。"""
        return role in self.roles

    @property
    def plain_text(self) -> str:
        """包含全部子节点的完整文本（叶子节点即 :attr:`text`）。"""
        if not self.children:
            return self.text
        return self.text + "".join(child.plain_text for child in self.children)

    @property
    def is_translation(self) -> bool:
        return self.has_role("x-translation")

    @property
    def is_romanization(self) -> bool:
        return self.has_role("x-roman")

    @property
    def is_background(self) -> bool:
        return self.has_role("x-bg")


@dataclass
class Sentence:
    """句子 — 一行歌词

    由字符列表构成，按 linked_to_next 标记自动分组为词语。
    句子是歌词的基本行单位，对应一行文本。

    Attributes:
        id: 唯一标识符（UUID）
        singer_id: 所属演唱者ID（行级默认演唱者）
        characters: 字符列表

    Example:
        >>> s = Sentence(
        ...     singer_id="singer_1",
        ...     characters=[
        ...         Character(char="赤", singer_id="singer_1"),
        ...         Character(char="い", singer_id="singer_1"),
        ...     ]
        ... )
        >>> s.text
        '赤い'
    """

    singer_id: str
    id: str = field(default_factory=lambda: str(uuid4()))
    characters: List[Character] = field(default_factory=list)
    # ── TTML 辅助角色行（翻译 / 音译 / 背景人声） ──
    # 这些内容不再挂在主行上，而是变成独立的 Sentence，由 Singer/轴分组
    # 管理；下列字段只在该 Sentence 是辅助角色行时非空。
    aux_role: str = ""
    aux_parent_id: str = ""
    aux_lang: str = ""
    aux_agent: str = ""
    aux_order: int = 0
    aux_begin: Optional[int] = None
    aux_end: Optional[int] = None
    # AMLL/Apple Music 段落（Verse / Chorus / ...）；空 = 未标注。
    # 块级元数据，不是逐字演唱者分配。
    song_part: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raise ValidationError("句子ID不能为空")
        if not self.singer_id:
            raise ValidationError("singer_id 不能为空")

    # ── 文本属性 ──

    @property
    def text(self) -> str:
        """歌词文本"""
        return "".join(c.char for c in self.characters)

    # ── 辅助角色行便捷访问 ──

    @property
    def is_auxiliary(self) -> bool:
        """该行是否来自 TTML 辅助角色（翻译/音译/和声等）。"""
        return bool(self.aux_role)

    @property
    def translation(self) -> str:
        """辅助角色为翻译时返回文本，否则空串。"""
        return self.text if "x-translation" in self.aux_role.split() else ""

    @property
    def romanization(self) -> str:
        """辅助角色为音译时返回文本，否则空串。"""
        return self.text if "x-roman" in self.aux_role.split() else ""

    @property
    def background(self) -> str:
        """辅助角色为背景人声时返回文本，否则空串。"""
        return self.text if "x-bg" in self.aux_role.split() else ""

    @property
    def chars(self) -> List[str]:
        """字符列表（兼容旧接口）"""
        return [c.char for c in self.characters]

    # ── 词语计算 ──

    @property
    def words(self) -> List[Word]:
        """从字符的 linked_to_next 标志计算词语

        拥有 linked_to_next=True 的字符与下一字符组成同一词语。
        没有连词的单个字符独立为一个词语。

        Returns:
            词语列表
        """
        words: List[Word] = []
        current: List[Character] = []
        for char in self.characters:
            current.append(char)
            if not char.linked_to_next:
                words.append(Word(characters=current))
                current = []
        if current:
            words.append(Word(characters=current))
        return words

    # ── 字符管理 ──

    def get_character(self, char_idx: int) -> Optional[Character]:
        """获取指定索引的字符"""
        if 0 <= char_idx < len(self.characters):
            return self.characters[char_idx]
        return None

    def get_ruby_for_char(self, char_idx: int) -> Optional[Ruby]:
        """获取指定字符的注音"""
        char = self.get_character(char_idx)
        return char.ruby if char else None

    def get_word_for_char(self, char_idx: int) -> Optional[Word]:
        """获取包含指定字符的词语"""
        idx = 0
        for word in self.words:
            end_idx = idx + len(word.characters)
            if idx <= char_idx < end_idx:
                return word
            idx = end_idx
        return None

    def get_word_char_range(self, char_idx: int) -> tuple[int, int]:
        """获取包含指定字符的词语的字符范围

        Returns:
            (start_idx, end_idx) — 左闭右开
        """
        idx = 0
        for word in self.words:
            end_idx = idx + len(word.characters)
            if idx <= char_idx < end_idx:
                return idx, end_idx
            idx = end_idx
        return char_idx, char_idx + 1

    def insert_character(self, idx: int, ch: Character) -> None:
        """在指定位置前插入字符。"""
        if idx < 0 or idx > len(self.characters):
            raise ValidationError(
                f"字符索引 {idx} 超出范围 [0, {len(self.characters)}]"
            )

        if not self.characters:
            ch.is_line_end = True
        else:
            # 行尾标记迁移（一行只有一个 is_line_end，且位于行末）：
            # 插入点越过原行尾字（idx 大于其位置）时，原行尾字不再位于行末，
            # 标记迁移到新字符；插入字自带行尾标记但落在行尾字之前时，
            # 清掉其标记，避免出现双行尾字。
            end_idx = next(
                (i for i, c in enumerate(self.characters) if c.is_line_end), -1
            )
            if end_idx >= 0:
                if idx > end_idx:
                    self.characters[end_idx].is_line_end = False
                    ch.is_line_end = True
                elif ch.is_line_end:
                    ch.is_line_end = False

        self.characters.insert(idx, ch)

    def delete_character(self, idx: int) -> bool:
        """删除指定字符，返回删除后该行是否为空。"""
        if idx < 0 or idx >= len(self.characters):
            raise ValidationError(
                f"字符索引 {idx} 超出范围 [0, {len(self.characters)})"
            )

        char = self.characters[idx]
        was_line_end = char.is_line_end
        was_sentence_end = char.is_sentence_end
        self.characters.pop(idx)

        if not self.characters:
            return True

        if was_line_end:
            promote_idx = idx - 1 if idx > 0 else len(self.characters) - 1
            self.characters[promote_idx].is_line_end = True

        if was_sentence_end:
            prev_idx = idx - 1
            if 0 <= prev_idx < len(self.characters):
                prev = self.characters[prev_idx]
                if not prev.is_sentence_end:
                    prev.is_sentence_end = True

        if idx > 0 and idx - 1 < len(self.characters):
            prev = self.characters[idx - 1]
            if prev.linked_to_next and idx - 1 >= len(self.characters) - 1:
                prev.linked_to_next = False

        return False

    def toggle_sentence_end(self, idx: int) -> None:
        """切换指定字符的停顿点标记。"""
        char = self.get_character(idx)
        if not char:
            raise ValidationError(
                f"字符索引 {idx} 超出范围 [0, {len(self.characters)})"
            )

        if char.is_sentence_end:
            char.clear_sentence_end_ts()
        char.is_sentence_end = not char.is_sentence_end

    def add_checkpoint(self, idx: int, *, ruby_split_mode: str = "mora") -> None:
        """增加指定字符的普通节奏点。"""
        char = self.get_character(idx)
        if not char:
            raise ValidationError(
                f"字符索引 {idx} 超出范围 [0, {len(self.characters)})"
            )
        char.set_check_count(char.check_count + 1, ruby_split_mode=ruby_split_mode)

    def remove_checkpoint(self, idx: int, *, force: bool = False) -> None:
        """减少指定字符的普通节奏点。

        Args:
            idx: 字符索引
            force: 当减至 0 会丢失 ruby 时是否强制（透传给 set_check_count）

        Raises:
            RubyDataLossError: 减至 0 且 ruby 非空且 !force（调用方应弹窗确认）
        """
        char = self.get_character(idx)
        if not char:
            raise ValidationError(
                f"字符索引 {idx} 超出范围 [0, {len(self.characters)})"
            )
        new_count = max(0, char.check_count - 1)
        char.set_check_count(new_count, force=force)

    def split_at(self, idx: int) -> "Sentence":
        """在指定字符后断行，返回后半句。"""
        if idx < 0 or idx >= len(self.characters):
            raise ValidationError(
                f"字符索引 {idx} 超出范围 [0, {len(self.characters)})"
            )

        current_chars = self.characters[: idx + 1]
        moved_chars = self.characters[idx + 1 :]
        self.characters = current_chars

        split_char = self.characters[idx]
        split_char.is_line_end = True
        split_char.linked_to_next = False

        if moved_chars:
            moved_singer_id = moved_chars[0].singer_id or split_char.singer_id or self.singer_id
            if not any(ch.is_line_end for ch in moved_chars):
                moved_chars[-1].is_line_end = True
            return Sentence(
                singer_id=moved_singer_id,
                characters=moved_chars,
                song_part=self.song_part,
                aux_role=self.aux_role,
                aux_parent_id=self.aux_parent_id,
                aux_lang=self.aux_lang,
                aux_agent=self.aux_agent,
                aux_order=self.aux_order,
                aux_begin=self.aux_begin,
                aux_end=self.aux_end,
            )

        moved_singer_id = split_char.singer_id or self.singer_id
        return Sentence(
            singer_id=moved_singer_id,
            characters=[],
            song_part=self.song_part,
            aux_role=self.aux_role,
            aux_parent_id=self.aux_parent_id,
            aux_lang=self.aux_lang,
            aux_agent=self.aux_agent,
            aux_order=self.aux_order,
            aux_begin=self.aux_begin,
            aux_end=self.aux_end,
        )

    # ── 时间戳管理 ──

    def shift_times(self, delta_ms: int) -> None:
        """整体平移本行时间（含副轴容器），并刷新 global/ruby。"""
        delta_ms = int(delta_ms)
        for char in self.characters:
            if char.timestamps:
                char.timestamps = [
                    max(0, int(timestamp) + delta_ms)
                    for timestamp in char.timestamps
                ]
            if char.sentence_end_ts is not None:
                char.set_sentence_end_ts(
                    max(0, int(char.sentence_end_ts) + delta_ms)
                )
            char._update_offset_timestamps()
            char.push_to_ruby()
        if self.aux_begin is not None:
            self.aux_begin = max(0, int(self.aux_begin) + delta_ms)
        if self.aux_end is not None:
            self.aux_end = max(0, int(self.aux_end) + delta_ms)

    def clear_times(self) -> None:
        """清空本行时间（含副轴容器）。"""
        for char in self.characters:
            char.clear_timestamps()
        self.aux_begin = None
        self.aux_end = None

    def push_all_timestamps(self) -> None:
        """将所有字符的时间戳推送给各自的 Ruby"""
        for char in self.characters:
            char.push_to_ruby()

    def get_timetags_for_char(self, char_idx: int) -> List[int]:
        """获取指定字符的所有时间戳

        Returns:
            时间戳列表（按 checkpoint_idx 顺序）
        """
        char = self.get_character(char_idx)
        return char.all_timestamps if char else []

    def get_global_timetags_for_char(self, char_idx: int) -> List[int]:
        """获取指定字符的所有全局时间戳（含偏移）

        Returns:
            带全局偏移的时间戳列表（按 checkpoint_idx 顺序）
        """
        char = self.get_character(char_idx)
        return char.all_global_timestamps if char else []

    def clear_all_timestamps(self) -> None:
        """清空所有字符的时间戳"""
        for char in self.characters:
            char.clear_timestamps()

    def clear_one_timestamps(self, char_idx: int) -> None:
        """清空一个字符的时间戳"""
        self.characters[char_idx].clear_timestamps()

    # ── 查询 ──

    def is_fully_timed(self) -> bool:
        """检查是否所有字符的节奏点都已打轴"""
        if not self.characters:
            return False
        total_pts = sum(c.total_timing_points for c in self.characters)
        if total_pts == 0:
            return True  # 无打轴任务的行默认已完成
        return all(c.is_fully_timed for c in self.characters)

    def get_timing_progress(self) -> tuple[int, int]:
        """获取打轴进度

        Returns:
            (已完成数量, 总共需要数量)
        """
        done = sum(
            len(c.timestamps) + (1 if c.sentence_end_ts is not None else 0)
            for c in self.characters
        )
        total = sum(c.total_timing_points for c in self.characters)
        return done, total

    @property
    def timing_start_ms(self) -> Optional[int]:
        """句子最早时间戳（毫秒），如果无时间标签返回 None"""
        all_ts = [ts for c in self.characters for ts in c.all_timestamps]
        return min(all_ts) if all_ts else None

    @property
    def timing_end_ms(self) -> Optional[int]:
        """句子最晚时间戳（毫秒），如果无时间标签返回 None"""
        all_ts = [ts for c in self.characters for ts in c.all_timestamps]
        return max(all_ts) if all_ts else None

    @property
    def global_timing_start_ms(self) -> Optional[int]:
        """句子最早全局时间戳（含偏移），如果无时间标签返回 None"""
        all_ts = [ts for c in self.characters for ts in c.all_global_timestamps]
        return min(all_ts) if all_ts else None

    @property
    def global_timing_end_ms(self) -> Optional[int]:
        """句子最晚全局时间戳（含偏移），如果无时间标签返回 None"""
        all_ts = [ts for c in self.characters for ts in c.all_global_timestamps]
        return max(all_ts) if all_ts else None

    @property
    def has_timetags(self) -> bool:
        """是否有任何时间标签"""
        return any(c.all_timestamps for c in self.characters)

    # ── Ruby 管理 ──

    @property
    def rubies(self) -> List[Ruby]:
        """收集所有字符的 Ruby 对象"""
        return [c.ruby for c in self.characters if c.ruby]

    def add_ruby_to_char(self, char_idx: int, ruby: Ruby) -> None:
        """为指定字符添加注音

        Args:
            char_idx: 字符索引
            ruby: 注音对象

        Raises:
            ValidationError: 如果字符索引无效或字符已有注音
        """
        char = self.get_character(char_idx)
        if not char:
            raise ValidationError(
                f"字符索引 {char_idx} 超出范围 [0, {len(self.characters)})"
            )
        if char.ruby:
            raise ValidationError(f"字符 {char_idx} 已有注音")
        char.set_ruby(ruby)

    def remove_ruby_from_char(self, char_idx: int) -> Optional[Ruby]:
        """移除指定字符的注音

        Returns:
            被移除的 Ruby 对象，如果没有返回 None
        """
        char = self.get_character(char_idx)
        if char and char.ruby:
            removed = char.ruby
            char.set_ruby(None)
            return removed
        return None

    def clear_all_rubies(self) -> None:
        """清空所有字符的注音"""
        for char in self.characters:
            char.set_ruby(None)

    # ── 字素簇归一化 ──

    def needs_grapheme_normalization(self) -> bool:
        """是否存在「不在字素簇边界起始」的字符（需要归一化合并）。

        覆盖两种来源：按码点切分产生的孤立附加符残段（``['ɛ', '̃']``），
        以及多码点但跨簇的字符。全为单码点且无延伸码点开头时直接为 False
        （快路径，常规日/中/英/韩歌词永远走不到慢路径）。
        """
        for c in self.characters:
            if len(c.char) == 1:
                if starts_with_extender(c.char):
                    return True
            else:
                return True  # 多码点：需要完整核对簇边界
        return False

    def normalize_graphemes(self) -> bool:
        """把不在字素簇边界起始的字符并入前一字符，返回是否发生改动。

        合并规则（**leader 优先**）：

        - ``char``：拼接；
        - ``check_count`` / ``timestamps`` / ``ruby``：取 leader 的；leader 完全
          无节奏点数据且被并者是**连结线并入的基字**（非纯附加符）时，继承
          其数据（保住旧存档里挂在后一基字上的时间戳）；
        - 行尾/停顿点/连词标记（``linked_to_next`` ``is_line_end``
          ``is_sentence_end`` ``sentence_end_ts``）：取簇内**最后一个**成员的值
          （这些标记描述的是簇与后续内容的关系）；
        - 演唱者 / 导唱标记：leader 的，leader 缺省时取簇内首个非空值。

        行首的孤立附加符（无前驱可并）保持原样——数据保留，渲染层兜底。
        """
        chars = self.characters
        if not self.needs_grapheme_normalization():
            return False
        boundaries = set(grapheme_boundaries(self.text))
        had_line_end = any(c.is_line_end for c in chars)
        merged: List[Character] = []
        offset = 0
        changed = False
        for c in chars:
            dependent = offset not in boundaries
            if dependent and merged:
                leader = merged[-1]
                leader.char += c.char
                pure_mark = is_dependent_text(c.char)
                if not pure_mark and not leader.timestamps:
                    # 连结线并入的基字：leader 无时间戳时继承（节奏点数取
                    # max，ruby 在 leader 无注音时一并继承），避免丢轴
                    if leader.check_count == 0:
                        leader.check_count = c.check_count
                    if leader.ruby is None:
                        leader.ruby = c.ruby
                    leader.timestamps = list(c.timestamps)
                if leader.sentence_end_ts is None:
                    leader.sentence_end_ts = c.sentence_end_ts
                if not leader.singer_id:
                    leader.singer_id = c.singer_id
                leader.needs_guide = leader.needs_guide or c.needs_guide
                leader.is_guide = leader.is_guide or c.is_guide
                leader.force_singer_tag = leader.force_singer_tag or c.force_singer_tag
                # 簇尾标记：取最后一个成员
                leader.linked_to_next = c.linked_to_next
                leader.is_line_end = leader.is_line_end or c.is_line_end
                leader.is_sentence_end = leader.is_sentence_end or c.is_sentence_end
                leader.push_to_ruby()
                changed = True
            else:
                merged.append(c)
            offset += len(c.char)
        if changed:
            self.characters = merged
            if merged and had_line_end:
                for c in merged:
                    c.is_line_end = False
                merged[-1].is_line_end = True
        return changed

    def explode_graphemes(self) -> Optional[List[List[int]]]:
        """把多码点字符临时拆成单码点「码点视图」，返回映射。

        Returns:
            ``spans``：每个原字符对应的码点件下标列表；无可拆（全单码点）
            时返回 ``None``（视图与原句完全一致，零开销）。

        说明：
            供自动注音等**按码点索引**的既有管线使用（配合
            :meth:`merge_grapheme_pieces` 还原）。首码点件继承全部数据；
            后续码点件为空壳（cc=0、簇内连词）；簇尾标记（行尾/停顿点/
            连词）迁到最后一件；导唱/强插标签留在首件。
        """
        if all(len(c.char) == 1 for c in self.characters):
            return None
        pieces: List[Character] = []
        spans: List[List[int]] = []
        for c in self.characters:
            cps = list(c.char)
            span: List[int] = []
            if len(cps) == 1:
                span.append(len(pieces))
                pieces.append(c)
                spans.append(span)
                continue
            # 首件：原对象（保数据），摘下簇尾标记（先快照再改——first 就是 c）
            tail = (
                c.is_line_end,
                c.is_sentence_end,
                c.sentence_end_ts,
                c.linked_to_next,
            )
            first = c
            first.char = cps[0]
            first.is_line_end = False
            first.is_sentence_end = False
            first.sentence_end_ts = None
            first.linked_to_next = True  # 簇内连成组
            span.append(len(pieces))
            pieces.append(first)
            # 中间件 / 末件
            for j in range(1, len(cps)):
                is_last = j == len(cps) - 1
                piece = Character(char=cps[j], check_count=0, singer_id=c.singer_id)
                if is_last:
                    piece.is_line_end = tail[0]
                    piece.is_sentence_end = tail[1]
                    piece.sentence_end_ts = tail[2]
                    piece.linked_to_next = tail[3]
                else:
                    piece.linked_to_next = True
                span.append(len(pieces))
                pieces.append(piece)
            spans.append(span)
        self.characters = pieces
        return spans

    def merge_grapheme_pieces(self, spans: Optional[List[List[int]]]) -> None:
        """把 :meth:`explode_graphemes` 的码点视图合并回字素簇字符。

        与 ``explode_graphemes`` 互逆；码点件在管线中可能已被原地修改
        （check_count / timestamps / ruby 等），合并时**首件优先**、簇尾标记
        取末件。若管线改变了字符数量（当前流程不会），退化为
        :meth:`normalize_graphemes` 的簇边界合并。
        """
        if not spans:
            return
        n_pieces = sum(len(s) for s in spans)
        if len(self.characters) != n_pieces:
            # 防御：管线改写了结构，按簇边界归一化兜底
            self.normalize_graphemes()
            return
        merged: List[Character] = []
        idx = 0
        for span in spans:
            group = self.characters[idx : idx + len(span)]
            idx += len(span)
            if len(group) == 1:
                merged.append(group[0])
                continue
            leader = group[0]
            last = group[-1]
            leader.char = "".join(p.char for p in group)
            # 连结线并入的基字（非纯附加符）：leader 无节奏点数据时继承末件，
            # 避免丢旧存档挂在后一基字上的轴
            dependents_all_marks = is_dependent_text(
                "".join(p.char for p in group[1:])
            )
            if (
                not dependents_all_marks
                and leader.check_count == 0
                and not leader.timestamps
            ):
                leader.check_count = last.check_count
                leader.timestamps = list(last.timestamps)
                if leader.ruby is None:
                    leader.ruby = last.ruby
            if leader.sentence_end_ts is None:
                leader.sentence_end_ts = last.sentence_end_ts
            if not leader.singer_id:
                leader.singer_id = last.singer_id
            leader.linked_to_next = last.linked_to_next
            leader.is_line_end = leader.is_line_end or last.is_line_end
            leader.is_sentence_end = leader.is_sentence_end or last.is_sentence_end
            for p in group[1:]:
                leader.needs_guide = leader.needs_guide or p.needs_guide
                leader.is_guide = leader.is_guide or p.is_guide
                leader.force_singer_tag = leader.force_singer_tag or p.force_singer_tag
            leader.push_to_ruby()
            merged.append(leader)
        self.characters = merged
        # 行尾标记唯一性重建（合并后行尾必在最后一个字符上）
        if merged and any(p.is_line_end for p in self.characters):
            for p in self.characters:
                p.is_line_end = False
            merged[-1].is_line_end = True

    # ── 初始化辅助 ──

    @classmethod
    def from_text(
        cls,
        text: str,
        singer_id: str,
        id: Optional[str] = None,
        *,
        by_codepoint: bool = False,
    ) -> "Sentence":
        """从纯文本创建句子。

        自动拆分为单字符（**字素簇**），设置默认 checkpoint 配置。
        最后一个字符标记为停顿点（check_count=1）。

        Args:
            text: 歌词文本
            singer_id: 演唱者 ID
            id: 可选的句子 ID
            by_codepoint: True 时按码点拆分（供既有按码点索引的解析管线
                使用——它们随后自行调用 :meth:`normalize_graphemes` 收口）。
                默认按字素簇拆分。

        Returns:
            Sentence 实例
        """
        if not text:
            raise ValidationError("歌词文本不能为空")

        chars_list = list(text) if by_codepoint else split_graphemes(text)
        characters = []
        for i, ch in enumerate(chars_list):
            is_last = i == len(chars_list) - 1
            characters.append(
                Character(
                    char=ch,
                    check_count=1,
                    is_line_end=is_last,
                    is_sentence_end=is_last,
                    singer_id=singer_id,
                )
            )

        kwargs = {"singer_id": singer_id, "characters": characters}
        if id is not None:
            kwargs["id"] = id
        return cls(**kwargs)
