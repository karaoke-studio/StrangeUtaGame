"""快捷键子页面。

_on_shortcut_changed 与原始 SettingsInterface 逻辑完全一致：
- 冲突检测后，通过 _notify_changed（即 _change_callback）通知外层保存。
- _SHORTCUT_ACTIONS / _SHORTCUT_MODES 保留为类属性，供测试直接引用。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QCoreApplication, QTimer
from qfluentwidgets import FluentIcon as FIF, InfoBar, InfoBarPosition, SettingCardGroup

from ..cards import ComboSettingCard, ShortcutSettingCard
from .base import SubSettingInterface


# 翻译统一走显式 QCoreApplication.translate 直呼（pylupdate6 只认直呼
# 形态，别名转发会把条目误标 vanished；不依赖 self，单测的
# SimpleNamespace 替身同样可用）。

class ShortcutSubInterface(SubSettingInterface):

    _SHORTCUT_ACTIONS: list = [
        # (key, icon, title, content, default_timing, default_edit, scope,
        #  timing_content, edit_content, readonly)
        #
        # 顺序按功能分组（同类相邻）；不可修改的固定功能集中在列表最底部。
        # 工具栏（EditorToolBar）里的每个功能都在此登记，可在快捷键设置中自定义；
        # 工具栏功能默认不绑定按键（留空），由用户按需设置。

        # ── 播放控制 ──
        ("play_pause", FIF.PLAY, "播放/暂停", "切换播放和暂停", "A:short", "A:short", "both", None, None, False),
        ("stop", FIF.PAUSE, "停止", "停止播放", "S:short", "S:short", "both", None, None, False),
        ("restart_playback", FIF.SYNC, "从头播放", "停止并从头播放", "SHIFT+S:short", "SHIFT+S:short", "both", None, None, False),
        ("speed_down", FIF.SPEED_OFF, "减速", "降低播放速度", "Q:short", "Q:short", "both", None, None, False),
        ("speed_up", FIF.SPEED_HIGH, "加速", "提高播放速度", "W:short", "W:short", "both", None, None, False),
        ("speed_reset", FIF.SPEED_MEDIUM, "恢复默认速度", "将播放速度恢复为设置中的默认速度；双击速度条也可恢复", "E:short", "E:short", "both", None, None, False),
        ("volume_up", FIF.VOLUME, "音量增大", "增大播放音量", "", "", "both", None, None, False),
        ("volume_down", FIF.MUTE, "音量减小", "减小播放音量", "", "", "both", None, None, False),
        ("volume_reset", FIF.VOLUME, "恢复默认音量", "将播放音量恢复为设置中的默认音量；双击音量条也可恢复", "", "", "both", None, None, False),

        # ── 导航与跳转 ──
        ("nav_prev_line", FIF.UP, "上一行", "移动到上一歌词行", "UP:short", "UP:short", "both", None, None, False),
        ("nav_next_line", FIF.DOWN, "下一行", "移动到下一歌词行", "DOWN:short", "DOWN:short", "both", None, None, False),
        ("nav_prev_char", FIF.LEFT_ARROW, "上一字符", "在当前行内移动到上一个字符；若已在首字符则跳到上一行末字符", "LEFT:short", "LEFT:short", "both", None, None, False),
        ("nav_next_char", FIF.RIGHT_ARROW, "下一字符", "在当前行内移动到下一个字符；若已在末字符则跳到下一行首字符", "RIGHT:short", "RIGHT:short", "both", None, None, False),
        ("seek_back", FIF.LEFT_ARROW, "后退", "后退跳转【仅打轴模式】", "Z:short", "", "timing_only", None, None, False),
        ("seek_forward", FIF.CHEVRON_RIGHT, "前进", "前进跳转【仅打轴模式】", "X:short", "", "timing_only", None, None, False),
        ("lock_playback_start", FIF.PIN, "锁定播放起点", "在当前播放位置锁定区间起点；再次触发则取消，可撤销", "[:short", "[:short", "both", None, None, False),
        ("lock_playback_end", FIF.PIN, "锁定播放终点", "在当前播放位置锁定区间终点；再次触发则取消，可撤销", "]:short", "]:short", "both", None, None, False),

        # ── 时间轴显示 ──
        ("toggle_waveform_spectrum", FIF.VIEW, "切换波形/声谱/双谱", "在波形图、声谱图与双谱模式之间循环切换", "", "", "both", None, None, False),

        # ── 打轴与时间戳微调 ──
        ("tag_now", FIF.PLAY, "打轴键", "打轴操作的按键【仅打轴模式】", "D:short,F:short", "", "timing_only", None, None, False),
        ("tag_now_extra", FIF.PLAY, "打轴键 Extra", "打轴操作的备用按键【仅打轴模式】", "SPACE:short", "", "timing_only", None, None, False),
        ("tag_and_delete_next", FIF.PLAY, "打轴并删除下一节奏点", "记录当前节奏点时间戳，同时删除下一个节奏点本身（减少 check_count），光标跳至原第三个节奏点【仅打轴模式】", "", "", "timing_only", None, None, False),
        ("tag_now_editor", FIF.PLAY, "打轴键（编辑模式）", "编辑模式下打轴：记录当前进度条时间戳至当前节奏点【仅编辑模式】", "", "", "edit_only", None, None, False),
        ("tag_now_extra_editor", FIF.PLAY, "打轴键 Extra（编辑模式）", "编辑模式下打轴（备用键）：记录当前进度条时间戳至当前节奏点【仅编辑模式】", "", "", "edit_only", None, None, False),
        ("delete_timestamp", FIF.DELETE, "删除当前时间戳并回滚", "删除跳转【仅打轴模式】", "Backspace:short", "", "timing_only", None, None, False),
        ("timestamp_up", FIF.UP, "时间戳+步长", "增加选中节奏点时间戳", "ALT+UP:short", "ALT+UP:short", "both", None, None, False),
        ("timestamp_down", FIF.DOWN, "时间戳-步长", "减少选中节奏点时间戳", "ALT+DOWN:short", "ALT+DOWN:short", "both", None, None, False),

        # ── 节奏点与停顿点 ──
        ("add_checkpoint", FIF.PIN, "增加节奏点", "增加当前字符的节奏点数量", "1:short", "Space:short", "split", "增加当前字符的节奏点数量（默认 1）", "增加当前字符的节奏点数量（默认 Space）", False),
        ("remove_checkpoint", FIF.REMOVE, "删除节奏点", "减少当前字符的节奏点数量", "3:short", "BACKSPACE:short", "split", "减少当前字符的节奏点数量（默认 3）", "减少当前字符的节奏点数量（默认 Backspace）", False),
        ("cycle_checkpoint", FIF.SYNC, "切换字内节奏点", "在当前字符的多个节奏点之间循环切换（Alt+→）", "ALT+RIGHT:short", "ALT+RIGHT:short", "both", None, None, False),
        ("cycle_checkpoint_prev", FIF.SYNC, "切换字内节奏点（反向）", "在当前字符的多个节奏点之间反向循环切换（Alt+←）", "ALT+LEFT:short", "ALT+LEFT:short", "both", None, None, False),
        ("clear_all_checkpoints", FIF.DELETE, "清除所有节奏点", "删除当前字符全部节奏点并取消停顿点标记（cc=0，is_sentence_end=False）", "CTRL+\\:short", "CTRL+\\:short", "both", None, None, False),
        ("toggle_line_end", FIF.TAG, "切换停顿点", "切换当前字符的停顿点标记", "2:short", ".:short", "split", "切换当前字符的停顿点标记（默认 2）", "切换当前字符的停顿点标记（默认句号）", False),
        ("timestamps_to_sentence_end", FIF.TAG, "时间戳转停顿点", "取消所有节奏点、清除时间戳并标记为停顿点", "P:short", "P:short", "both", None, None, False),

        # ── 字符与行编辑 ──
        ("edit_ruby", FIF.EDIT, "注音编辑", "编辑当前字符注音", "F2:short", "F2:short", "both", None, None, False),
        ("toggle_word_join", FIF.LINK, "连词", "连词/取消连词；划选多个字符时：全未连词则整段连为一个词，否则整段取消连词", "F3:short", "F3:short", "both", None, None, False),
        ("insert_space", FIF.ADD, "插入空格", "在当前字符后插入空格", "M:short", "M:short", "both", None, None, False),
        ("modify_char", FIF.EDIT, "修改所选字符", "打开修改所选字符对话框", "J:short", "J:short", "both", None, None, False),
        ("modify_line", FIF.EDIT, "修改选中行", "打开修改选中行对话框", "CTRL+2:short", "CTRL+2:short", "both", None, None, False),
        ("bulk_change", FIF.EDIT, "批量变更", "打开批量变更对话框", "CTRL+H:short", "CTRL+H:short", "both", None, None, False),
        ("insert_guide", FIF.ADD, "插入导唱符", "打开插入导唱符对话框", "CTRL+1:short", "CTRL+1:short", "both", None, None, False),
        ("auto_insert_guide", FIF.SYNC, "自动插入导唱符", "根据时间戳自动插入导唱符", "", "", "both", None, None, False),
        ("toggle_needs_guide", FIF.PIN, "切换导唱待办", "切换当前字符的导唱待办标记（在字符左上角显示半透明 ✚，提示稍后需要插入导唱符）", ";:short", ";:short", "both", None, None, False),

        # ── 自动注音 ──
        ("analyze_rubies", FIF.SYNC, "注音分析", "自动分析全部注音", "CTRL+3:short", "CTRL+3:short", "both", None, None, False),
        ("analyze_rubies_by_line", FIF.SYNC, "按行注音分析", "仅分析当前行的注音", "CTRL+SHIFT+A:short", "CTRL+SHIFT+A:short", "both", None, None, False),
        ("analyze_rubies_selected", FIF.SYNC, "注音分析所选字符", "仅分析当前行选中字符的注音", "CTRL+A:short", "CTRL+A:short", "both", None, None, False),
        ("analyze_rubies_no_cp", FIF.SYNC, "注音分析（仅注音）", "分析全部注音，但不更新节奏点", "", "", "both", None, None, False),
        ("analyze_rubies_by_line_no_cp", FIF.SYNC, "按行注音分析（仅注音）", "仅分析当前行注音，但不更新节奏点", "", "", "both", None, None, False),
        ("analyze_rubies_selected_no_cp", FIF.SYNC, "注音分析所选字符（仅注音）", "仅分析选中字符注音，但不更新节奏点", "", "", "both", None, None, False),
        ("romanize_all", FIF.FONT, "全部转为罗马字", "将现有注音整体转为罗马字（不更新节奏点、不删除注音）", "", "", "both", None, None, False),
        ("analyze_pinyin", FIF.FONT, "中文拼音注音", "为中文歌词自动添加拼音注音", "", "", "both", None, None, False),
        ("analyze_pinyin_katakana", FIF.FONT, "中文片假名注音", "为中文歌词自动添加片假名近似音注音", "", "", "both", None, None, False),
        ("analyze_korean", FIF.FONT, "韩文注音", "为韩文歌词按设置风格标注片假名/平假名/罗马音注音", "", "", "both", None, None, False),
        ("delete_rubies_by_type", FIF.DELETE, "按类型删除注音", "按类型删除注音对话框", "CTRL+4:short", "CTRL+4:short", "both", None, None, False),

        # ── 演唱者 ──
        ("singer_manager", FIF.PEOPLE, "演唱者管理", "打开演唱者管理窗口", "", "", "both", None, None, False),
        ("apply_singer", FIF.PEOPLE, "应用演唱者", "为选中字符设置演唱者", "K:short", "K:short", "both", None, None, False),
        ("set_singer_by_line", FIF.PEOPLE, "按行设置演唱者", "按行批量设置演唱者", "CTRL+5:short", "CTRL+5:short", "both", None, None, False),

        # ── 时间戳工具 ──
        ("complete_timestamp", FIF.DATE_TIME, "补全时间戳", "为缺失时间戳的字符补全时间戳", "", "", "both", None, None, False),
        ("separate_symbol_timestamp", FIF.DATE_TIME, "分离符号时间戳", "将符号与相邻字符的时间戳分离", "", "", "both", None, None, False),
        ("adjust_raw_timestamp", FIF.DATE_TIME, "调整原始时间戳", "整体调整全部原始时间戳", "", "", "both", None, None, False),
        ("adjust_raw_timestamp_line", FIF.DATE_TIME, "按行调整原始时间戳", "调整当前行的原始时间戳", "", "", "both", None, None, False),
        ("adjust_raw_timestamp_selected", FIF.DATE_TIME, "调整所选字符原始时间戳", "调整所选字符的原始时间戳", "", "", "both", None, None, False),
        ("delete_all_timestamps", FIF.DELETE, "删除所有时间戳", "删除项目中全部时间戳", "", "", "both", None, None, False),
        ("delete_all_timestamps_keep_head", FIF.DELETE, "删除所有时间戳（保留行首）", "删除全部时间戳，但保留每行行首时间戳", "", "", "both", None, None, False),
        ("delete_timestamps_selected", FIF.DELETE, "删除所选范围时间戳", "删除所选范围内的时间戳", "", "", "both", None, None, False),
        ("auto_generate_interlude_guide", FIF.MUSIC, "自动生成间奏指引", "根据时间戳自动生成间奏指引", "", "", "both", None, None, False),

        # ── 文件与导出 ──
        ("new_project", FIF.ADD, "新建项目", "新建空白项目", "CTRL+0:short", "CTRL+0:short", "both", None, None, False),
        ("load_project", FIF.FOLDER, "加载项目", "打开已有项目文件", "", "", "both", None, None, False),
        ("save_as", FIF.SAVE_AS, "项目另存为", "将当前项目另存为新文件", "", "", "both", None, None, False),
        ("load_audio", FIF.MUSIC, "加载音频", "加载音频文件", "", "", "both", None, None, False),
        ("load_lyrics", FIF.DOCUMENT, "加载歌词", "加载歌词文本文件", "", "", "both", None, None, False),
        ("concat_sug", FIF.LINK, "多项目拼接", "拼接多个 SUG 项目", "", "", "both", None, None, False),
        ("open_fulltext", FIF.EDIT, "全文本编辑", "打开全文本编辑界面", "CTRL+T:short", "CTRL+T:short", "both", None, None, False),
        ("quick_export", FIF.SHARE, "快捷导出", "使用默认导出格式快速导出到文件", "CTRL+E:short", "CTRL+E:short", "both", None, None, False),

        # ══════════ 以下为固定功能（不可修改快捷键），始终置于列表最底部 ══════════
        ("undo", FIF.CANCEL, "撤销", "撤销操作", "CTRL+Z:short", "CTRL+Z:short", "both", None, None, True),
        ("redo", FIF.SYNC, "重做", "重做操作", "CTRL+Y:short", "CTRL+Y:short", "both", None, None, True),
        ("save", FIF.SAVE, "保存", "保存项目", "CTRL+S:short", "CTRL+S:short", "both", None, None, True),
        ("copy_chars", FIF.COPY, "复制字符", "复制选中字符的完整信息", "CTRL+C:short", "CTRL+C:short", "both", None, None, True),
        ("copy_plain_text", FIF.DOCUMENT, "复制纯文本", "复制选中字符的正文文本（不含注音、节奏点、时间码），可粘贴到任何位置", "CTRL+SHIFT+C:short", "CTRL+SHIFT+C:short", "both", None, None, True),
        ("paste_lyrics", FIF.PASTE, "粘贴", "无歌词时粘贴整批歌词文本；已有歌词时在光标处插入（复制的字符或纯文本）", "CTRL+V:short", "CTRL+V:short", "both", None, None, True),
        ("insert_line_break", FIF.RETURN, "插入换行", "在光标处插入换行", "Enter:short", "Enter:short", "both", None, None, True),
        ("merge_line_up", FIF.RETURN, "合并上一行", "将当前行合并到上一行末尾", "Shift+Enter:short", "Shift+Enter:short", "both", None, None, True),
        ("delete_char", FIF.DELETE, "删除字符", "删除选中内容或当前字符", "Delete:short", "Delete:short", "both", None, None, True),
    ]

    _SHORTCUT_MODES: list = [
        ("timing_mode", "打轴模式（音乐播放时）"),
        ("edit_mode", "编辑模式（音乐暂停时）"),
    ]

    # 设置页分组边界：(组标题源串, 该组第一个 action key)。
    # 顺序必须与 _SHORTCUT_ACTIONS 中各功能分段的排列一致——_init_ui 遇到
    # 边界 key 就另起一个 SettingCardGroup（对齐关系由 _validate_group_boundaries
    # 在 _init_ui 开头校验）。纯 UI 展示层划分，不影响 _shortcut_cards 映射
    # 与 shortcuts.<mode>.<action> 存取路径。
    _SHORTCUT_GROUPS: list[tuple[str, str]] = [
        ("播放控制", "play_pause"),
        ("导航与跳转", "nav_prev_line"),
        ("时间轴显示", "toggle_waveform_spectrum"),
        ("打轴与时间戳微调", "tag_now"),
        ("节奏点与停顿点", "add_checkpoint"),
        ("字符与行编辑", "edit_ruby"),
        ("自动注音", "analyze_rubies"),
        ("演唱者", "singer_manager"),
        ("时间戳工具", "complete_timestamp"),
        ("文件与导出", "new_project"),
        ("固定功能（不可修改）", "undo"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._shortcut_cards: dict[str, dict[str, ShortcutSettingCard]] = {
            mode_key: {} for mode_key, _ in self._SHORTCUT_MODES
        }
        self._loading = False
        # 注音编辑窗口下拉的延迟应用标记：避免在 RoundMenu 关闭栈内
        # 同步触发设置保存，并合并连续快速切换。
        self._f2_editor_apply_pending = False
        self._init_ui()

        # 主题变化时刷新快捷键标签前缀颜色（深/浅模式颜色不同）
        from strange_uta_game.frontend.theme import theme
        theme.changed.connect(self._refresh_tag_colors)

    @staticmethod
    def _tag_colors() -> dict:
        """返回当前主题下快捷键标签前缀颜色。

        浅色模式：深蓝/深绿/深紫（对浅背景对比度高）
        深色模式：亮蓝/亮绿/亮紫（对深背景对比度高）
        """
        from strange_uta_game.frontend.theme import theme
        if theme.is_dark:
            return {"timing": "#60CDFF", "edit": "#6BCB77", "both": "#CE93D8"}
        return {"timing": "#0078D4", "edit": "#107C10", "both": "#5C2D91"}

    @staticmethod
    def _make_tag_html(scope: str, title: str, colors: dict) -> str:
        """生成带颜色标签前缀的 HTML 标题字符串。

        scope 标签 ``[通用]/[打轴]/[编辑]`` 通过 QCoreApplication.translate 走
        翻译表——本方法是 ``@staticmethod``，无 ``self.tr``。"""
        _map = {
            "both":         (colors["both"],   QCoreApplication.translate("ShortcutSubInterface", "[通用]")),
            "timing_only":  (colors["timing"], QCoreApplication.translate("ShortcutSubInterface", "[打轴]")),
            "edit_only":    (colors["edit"],   QCoreApplication.translate("ShortcutSubInterface", "[编辑]")),
            "split_timing": (colors["timing"], QCoreApplication.translate("ShortcutSubInterface", "[打轴]")),
            "split_edit":   (colors["edit"],   QCoreApplication.translate("ShortcutSubInterface", "[编辑]")),
        }
        if scope in _map:
            color, tag = _map[scope]
            return f'<span style="color:{color};font-weight:bold;">{tag}</span> {title}'
        return title

    def _refresh_tag_colors(self):
        """主题变化时重新为所有快捷键卡片标题应用正确的颜色标签。"""
        colors = self._tag_colors()
        tr = self.tr
        for row in self._SHORTCUT_ACTIONS:
            key, _, title, _, _, _, scope, _, _, _ = row
            translated = tr(title)
            if scope == "both":
                card = self._shortcut_cards["timing_mode"].get(key)
                if card:
                    card.setTitle(self._make_tag_html("both", translated, colors))
            elif scope == "timing_only":
                card = self._shortcut_cards["timing_mode"].get(key)
                if card:
                    card.setTitle(self._make_tag_html("timing_only", translated, colors))
            elif scope == "edit_only":
                card = self._shortcut_cards["edit_mode"].get(key)
                if card:
                    card.setTitle(self._make_tag_html("edit_only", translated, colors))
            elif scope == "split":
                card_t = self._shortcut_cards["timing_mode"].get(key)
                if card_t:
                    card_t.setTitle(self._make_tag_html("split_timing", translated, colors))
                card_e = self._shortcut_cards["edit_mode"].get(key)
                if card_e:
                    card_e.setTitle(self._make_tag_html("split_edit", translated, colors))

    def _register_action_strings_for_extractor(self):
        """显式枚举 _SHORTCUT_ACTIONS 里每条 title / content / split content。
        _SHORTCUT_ACTIONS 是类级常量，``tr(变量)`` 形式抓不到——但运行时
        Qt 仍会按 source 字符串到 .ts 里查；只要源串以 self.tr 字面量在本
        类（ShortcutSubInterface）上下文里出现过一次，extractor 就能登记。
        本方法返回值无意义、调用一次即可。"""
        tr = self.tr
        # title（按 _SHORTCUT_ACTIONS 顺序，缺译时按 source 显示）
        tr("播放/暂停"); tr("停止"); tr("从头播放"); tr("减速"); tr("加速"); tr("恢复默认速度")
        tr("音量增大"); tr("音量减小"); tr("恢复默认音量"); tr("上一行"); tr("下一行")
        tr("上一字符"); tr("下一字符")
        tr("切换字内节奏点（反向）"); tr("时间戳+步长"); tr("时间戳-步长")
        tr("切换字内节奏点")
        tr("注音编辑"); tr("连词")
        tr("打轴键"); tr("打轴键 Extra"); tr("打轴并删除下一节奏点")
        tr("打轴键（编辑模式）"); tr("打轴键 Extra（编辑模式）")
        tr("后退"); tr("前进"); tr("删除当前时间戳并回滚")
        tr("切换波形/声谱/双谱")
        tr("增加节奏点"); tr("删除节奏点"); tr("切换停顿点")
        tr("批量变更"); tr("修改所选字符"); tr("插入导唱符"); tr("自动插入导唱符")
        tr("切换导唱待办"); tr("修改选中行")
        tr("注音分析"); tr("按行注音分析"); tr("注音分析所选字符")
        tr("全文本编辑"); tr("按类型删除注音")
        tr("按行设置演唱者"); tr("应用演唱者")
        tr("时间戳转停顿点"); tr("清除所有节奏点")
        tr("快捷导出"); tr("插入空格")
        # 新增：工具栏功能对应的可设置快捷键 title（默认留空）
        tr("注音分析（仅注音）"); tr("按行注音分析（仅注音）")
        tr("注音分析所选字符（仅注音）"); tr("全部转为罗马字")
        tr("中文拼音注音"); tr("韩文注音")
        tr("演唱者管理")
        tr("补全时间戳"); tr("分离符号时间戳")
        tr("调整原始时间戳"); tr("按行调整原始时间戳"); tr("调整所选字符原始时间戳")
        tr("删除所有时间戳"); tr("删除所有时间戳（保留行首）"); tr("删除所选范围时间戳")
        tr("自动生成间奏指引")
        tr("新建项目"); tr("加载项目"); tr("项目另存为"); tr("加载音频"); tr("加载歌词"); tr("多项目拼接")
        tr("撤销"); tr("重做"); tr("保存"); tr("复制字符"); tr("复制纯文本")
        tr("粘贴"); tr("插入换行"); tr("合并上一行"); tr("删除字符")
        # content
        tr("切换播放和暂停"); tr("停止播放"); tr("停止并从头播放")
        tr("降低播放速度"); tr("提高播放速度")
        tr("将播放速度恢复为设置中的默认速度；双击速度条也可恢复")
        tr("增大播放音量"); tr("减小播放音量")
        tr("将播放音量恢复为设置中的默认音量；双击音量条也可恢复")
        tr("移动到上一歌词行"); tr("移动到下一歌词行")
        tr("在波形图、声谱图与双谱模式之间循环切换")
        tr("在当前行内移动到上一个字符；若已在首字符则跳到上一行末字符")
        tr("在当前行内移动到下一个字符；若已在末字符则跳到下一行首字符")
        tr("在当前字符的多个节奏点之间反向循环切换（Alt+←）")
        tr("增加选中节奏点时间戳"); tr("减少选中节奏点时间戳")
        tr("在当前字符的多个节奏点之间循环切换（Alt+→）")
        tr("编辑当前字符注音")
        tr("连词/取消连词；划选多个字符时：全未连词则整段连为一个词，否则整段取消连词")
        tr("打轴操作的按键【仅打轴模式】")
        tr("打轴操作的备用按键【仅打轴模式】")
        tr("记录当前节奏点时间戳，同时删除下一个节奏点本身（减少 check_count），光标跳至原第三个节奏点【仅打轴模式】")
        tr("编辑模式下打轴：记录当前进度条时间戳至当前节奏点【仅编辑模式】")
        tr("编辑模式下打轴（备用键）：记录当前进度条时间戳至当前节奏点【仅编辑模式】")
        tr("后退跳转【仅打轴模式】"); tr("前进跳转【仅打轴模式】")
        tr("删除跳转【仅打轴模式】")
        tr("增加当前字符的节奏点数量")
        tr("增加当前字符的节奏点数量（默认 [）")
        tr("增加当前字符的节奏点数量（默认 Space）")
        tr("减少当前字符的节奏点数量")
        tr("减少当前字符的节奏点数量（默认 ]")
        tr("减少当前字符的节奏点数量（默认 Backspace）")
        tr("切换当前字符的停顿点标记")
        tr("切换当前字符的停顿点标记（默认 句号）")
        tr("打开批量变更对话框"); tr("打开修改所选字符对话框")
        tr("打开插入导唱符对话框"); tr("根据时间戳自动插入导唱符")
        tr("切换当前字符的导唱待办标记（在字符左上角显示半透明 ✚，提示稍后需要插入导唱符）")
        tr("打开修改选中行对话框")
        tr("自动分析全部注音"); tr("仅分析当前行的注音")
        tr("仅分析当前行选中字符的注音")
        tr("打开全文本编辑界面"); tr("按类型删除注音对话框")
        tr("按行批量设置演唱者"); tr("为选中字符设置演唱者")
        tr("取消所有节奏点、清除时间戳并标记为停顿点")
        tr("删除当前字符全部节奏点并取消停顿点标记（cc=0，is_sentence_end=False）")
        tr("使用默认导出格式快速导出到文件")
        tr("在当前字符后插入空格")
        # 新增：工具栏功能对应的 content
        tr("分析全部注音，但不更新节奏点")
        tr("仅分析当前行注音，但不更新节奏点")
        tr("仅分析选中字符注音，但不更新节奏点")
        tr("将现有注音整体转为罗马字（不更新节奏点、不删除注音）")
        tr("打开演唱者管理窗口")
        tr("为缺失时间戳的字符补全时间戳")
        tr("将符号与相邻字符的时间戳分离")
        tr("整体调整全部原始时间戳")
        tr("调整当前行的原始时间戳")
        tr("调整所选字符的原始时间戳")
        tr("删除项目中全部时间戳")
        tr("删除全部时间戳，但保留每行行首时间戳")
        tr("删除所选范围内的时间戳")
        tr("根据时间戳自动生成间奏指引")
        tr("新建空白项目"); tr("打开已有项目文件"); tr("将当前项目另存为新文件")
        tr("加载音频文件"); tr("加载歌词文本文件"); tr("拼接多个 SUG 项目")
        tr("为中文歌词自动添加拼音注音"); tr("为韩文歌词按设置风格标注片假名/平假名/罗马音注音")
        tr("撤销操作"); tr("重做操作"); tr("保存项目")
        tr("复制选中字符的完整信息")
        tr("复制选中字符的正文文本（不含注音、节奏点、时间码），可粘贴到任何位置")
        tr("无歌词时粘贴整批歌词文本；已有歌词时在光标处插入（复制的字符或纯文本）")
        tr("在光标处插入换行"); tr("将当前行合并到上一行末尾")
        tr("删除选中内容或当前字符")
        # _SHORTCUT_MODES 标签
        tr("打轴模式（音乐播放时）"); tr("编辑模式（音乐暂停时）")
        # _SHORTCUT_GROUPS 分组标题
        tr("播放控制"); tr("导航与跳转"); tr("时间轴显示"); tr("打轴与时间戳微调")
        tr("节奏点与停顿点"); tr("字符与行编辑"); tr("自动注音")
        tr("演唱者"); tr("时间戳工具"); tr("文件与导出")
        tr("固定功能（不可修改）")

    @classmethod
    def _validate_group_boundaries(cls) -> None:
        """校验 _SHORTCUT_GROUPS 与 _SHORTCUT_ACTIONS 对齐——两份清单漂移时
        启动即报错，而不是静默把卡片归入错误的组。"""
        action_keys = {row[0] for row in cls._SHORTCUT_ACTIONS}
        assert cls._SHORTCUT_GROUPS, "_SHORTCUT_GROUPS 不能为空"
        assert cls._SHORTCUT_GROUPS[0][1] == cls._SHORTCUT_ACTIONS[0][0], (
            "_SHORTCUT_GROUPS 首组边界必须对齐 _SHORTCUT_ACTIONS 第一项"
        )
        for _title, first_key in cls._SHORTCUT_GROUPS:
            assert first_key in action_keys, (
                f"_SHORTCUT_GROUPS 边界 key {first_key!r} 不在 _SHORTCUT_ACTIONS 中，"
                "该组永远不会被创建"
            )

    def _init_ui(self):
        # 哑调用，仅为让 extractor 把所有 action title/content 字符串
        # 收录到本类上下文（参见方法 docstring）
        self._register_action_strings_for_extractor()
        colors = self._tag_colors()
        tr = self.tr

        self._validate_group_boundaries()
        # action key → 组标题源串；遇到边界 key 就另起一个 SettingCardGroup。
        # 首个 action 必是边界（上方断言保证），无需再设无组回退分支。
        boundaries = {first_key: title for title, first_key in self._SHORTCUT_GROUPS}

        def _wrap(t, s):
            # 类级常量里的 title 是源字符串；显示前过一遍 tr() 走翻译表
            return self._make_tag_html(s, tr(t), colors)

        group = None
        for row in self._SHORTCUT_ACTIONS:
            key, icon, title, content, dt, de, scope, tc, ec, ro = row
            if key in boundaries:
                group_title = boundaries[key]
                group = SettingCardGroup(tr(group_title), self.scrollWidget)
                self._tr_register(group, title_source=group_title)
                self.expandLayout.addWidget(group)
            if scope == "both":
                card = ShortcutSettingCard(icon, "", tr(content), dt, parent=group)
                card.setTitle(_wrap(title, "both"))
                if ro: card.setReadOnly(True)
                self._shortcut_cards["timing_mode"][key] = card
                self._shortcut_cards["edit_mode"][key] = card
                group.addSettingCard(card)
                if not ro:
                    card.value_changed.connect(lambda v, c=card: self._on_shortcut_changed(c, v))
            elif scope == "timing_only":
                card = ShortcutSettingCard(icon, "", tr(content), dt, parent=group)
                card.setTitle(_wrap(title, "timing_only"))
                if ro: card.setReadOnly(True)
                self._shortcut_cards["timing_mode"][key] = card
                group.addSettingCard(card)
                if not ro:
                    card.value_changed.connect(lambda v, c=card: self._on_shortcut_changed(c, v))
            elif scope == "edit_only":
                card = ShortcutSettingCard(icon, "", tr(content), de, parent=group)
                card.setTitle(_wrap(title, "edit_only"))
                if ro: card.setReadOnly(True)
                self._shortcut_cards["edit_mode"][key] = card
                group.addSettingCard(card)
                if not ro:
                    card.value_changed.connect(lambda v, c=card: self._on_shortcut_changed(c, v))
            elif scope == "split":
                ct = tr(tc) if tc else tr(content)
                ce = tr(ec) if ec else tr(content)
                card_t = ShortcutSettingCard(icon, "", ct, dt, parent=group)
                card_t.setTitle(_wrap(title, "split_timing"))
                if ro: card_t.setReadOnly(True)
                self._shortcut_cards["timing_mode"][key] = card_t
                group.addSettingCard(card_t)
                if not ro:
                    card_t.value_changed.connect(lambda v, c=card_t: self._on_shortcut_changed(c, v))
                card_e = ShortcutSettingCard(icon, "", ce, de, parent=group)
                card_e.setTitle(_wrap(title, "split_edit"))
                if ro: card_e.setReadOnly(True)
                self._shortcut_cards["edit_mode"][key] = card_e
                group.addSettingCard(card_e)
                if not ro:
                    card_e.value_changed.connect(lambda v, c=card_e: self._on_shortcut_changed(c, v))

            if key == "edit_ruby":
                self.card_f2_ruby_editor = self._tr_register(
                    ComboSettingCard(
                        FIF.EDIT,
                        tr("注音编辑窗口"),
                        tr("选择使用迷你浮窗或完整编辑窗口"),
                        items=[tr("迷你浮窗"), tr("经典大窗口")],
                        parent=group,
                    ),
                    title_source="注音编辑窗口",
                    content_source="选择使用迷你浮窗或完整编辑窗口",
                )
                self.card_f2_ruby_editor.set_item_sources(
                    ["迷你浮窗", "经典大窗口"]
                )
                self.card_f2_ruby_editor.index_changed.connect(
                    self._schedule_f2_editor_apply
                )
                group.addSettingCard(self.card_f2_ruby_editor)

    # connect_signals 不需要额外操作，信号已在 _init_ui 中连接
    def connect_signals(self):
        pass

    def _schedule_f2_editor_apply(self, *_args) -> None:
        """Defer settings notification until the combo popup has closed."""
        if self._loading or self._f2_editor_apply_pending:
            return
        self._f2_editor_apply_pending = True
        QTimer.singleShot(0, self._apply_f2_editor_change)

    def _apply_f2_editor_change(self) -> None:
        self._f2_editor_apply_pending = False
        if not self._loading:
            self._notify_changed()

    def _rebuild_for_language_change(self) -> None:
        """快捷键页：基类登记的 group title 由 super 处理；这里再把每张卡的
        title (HTML)/content 按当前语言重刷一遍——title 走 _refresh_tag_colors
        （它已经 tr 标题 + 重新拼 HTML），content 走 contentLabel.setText。"""
        super()._rebuild_for_language_change()
        try:
            self._refresh_tag_colors()
        except Exception:
            pass
        tr = self.tr
        for row in self._SHORTCUT_ACTIONS:
            key, _, _, content, _, _, scope, tc, ec, _ = row
            if scope in ("both", "timing_only"):
                card = self._shortcut_cards["timing_mode"].get(key)
                if card is not None and hasattr(card, "contentLabel"):
                    card.contentLabel.setText(tr(content))
            if scope in ("both", "edit_only"):
                card = self._shortcut_cards["edit_mode"].get(key)
                if card is not None and hasattr(card, "contentLabel"):
                    card.contentLabel.setText(tr(content))
            if scope == "split":
                card_t = self._shortcut_cards["timing_mode"].get(key)
                if card_t is not None and hasattr(card_t, "contentLabel"):
                    card_t.contentLabel.setText(tr(tc) if tc else tr(content))
                card_e = self._shortcut_cards["edit_mode"].get(key)
                if card_e is not None and hasattr(card_e, "contentLabel"):
                    card_e.contentLabel.setText(tr(ec) if ec else tr(content))

    def _on_shortcut_changed(self, changed_card: ShortcutSettingCard, new_value: str):
        """冲突检测，无冲突则通知外层保存。"""
        if self._loading:
            return

        new_pairs: list[tuple[str, str]] = []
        for k in new_value.split(","):
            k = k.strip()
            if not k:
                continue
            if ":" in k:
                kp, tp = k.rsplit(":", 1)
                new_pairs.append((kp.strip().upper(), tp.strip().lower()))
            else:
                new_pairs.append((k.upper(), "short"))

        # action title 走翻译表，冲突提示里也用翻译版本。用模块级 _tr 而非
        # self.tr：_on_shortcut_changed 单测用 SimpleNamespace 替身调用本函数，
        # SimpleNamespace 没有 .tr 方法。
        action_titles = {a[0]: QCoreApplication.translate("ShortcutSubInterface", a[2]) for a in self._SHORTCUT_ACTIONS}

        for mode_key, mode_label_raw in self._SHORTCUT_MODES:
            mode_label = QCoreApplication.translate("ShortcutSubInterface", mode_label_raw)
            mode_actions = self._shortcut_cards[mode_key]
            if not any(card is changed_card for card in mode_actions.values()):
                continue
            for action_key, card in mode_actions.items():
                if card is changed_card:
                    continue
                other_pairs = card.all_keys_with_trigger()
                if action_key == "tag_now":
                    expanded = []
                    for k, _ in other_pairs:
                        expanded.extend([(k, "short"), (k, "long")])
                    other_pairs = expanded
                for nk, nt in new_pairs:
                    if not nk:
                        continue
                    for ok, ot in other_pairs:
                        if nk == ok and nt == ot:
                            for btn in [changed_card.btn_key1, changed_card.btn_key2]:
                                if btn.get_key().strip().upper() == nk and btn.get_trigger_type() == nt:
                                    btn.restore_original_key()
                            trigger_label = QCoreApplication.translate("ShortcutSubInterface", "长按") if nt == "long" else QCoreApplication.translate("ShortcutSubInterface", "短按")
                            InfoBar.warning(
                                title=QCoreApplication.translate("ShortcutSubInterface", "快捷键冲突"),
                                content=QCoreApplication.translate("ShortcutSubInterface", "[{mode}]「{action}」已占用{trigger}按键 {key}").format(
                                    mode=mode_label,
                                    action=action_titles[action_key],
                                    trigger=trigger_label,
                                    key=nk,
                                ),
                                orient=Qt.Orientation.Horizontal,
                                isClosable=True,
                                position=InfoBarPosition.TOP,
                                duration=4000,
                                parent=self,
                            )
                            return

        self._notify_changed()

    def load_settings(self, s):
        self._loading = True
        try:
            for mode_key, _ in self._SHORTCUT_MODES:
                for row in self._SHORTCUT_ACTIONS:
                    action_key = row[0]
                    default_timing = row[4]
                    default_edit = row[5]
                    default_key = default_timing if mode_key == "timing_mode" else default_edit
                    card = self._shortcut_cards[mode_key].get(action_key)
                    if card is None:
                        continue
                    value = s.get(f"shortcuts.{mode_key}.{action_key}", default_key)
                    card.setValue(value)
            f2_editor_idx = {
                "compact": 0,
                "classic": 1,
            }.get(s.get("ui.f2_ruby_editor_mode", "compact"), 0)
            self.card_f2_ruby_editor.setCurrentIndex(f2_editor_idx)
        finally:
            self._loading = False

    def collect_settings(self, s):
        s.set(
            "ui.f2_ruby_editor_mode",
            {0: "compact", 1: "classic"}.get(
                self.card_f2_ruby_editor.currentIndex(), "compact"
            ),
        )
        for mode_key, _ in self._SHORTCUT_MODES:
            for row in self._SHORTCUT_ACTIONS:
                action_key = row[0]
                readonly = row[9] if len(row) > 9 else False
                if readonly:
                    continue
                card = self._shortcut_cards[mode_key].get(action_key)
                if card is None:
                    continue
                s.set(f"shortcuts.{mode_key}.{action_key}", card.value())
