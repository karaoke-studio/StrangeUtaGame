# UI 层设计

StrangeUtaGame 提供了三套主要的编辑界面，分别针对打轴、行编辑和全文本编辑任务。

## 核心界面

### 1. EditorInterface (打轴界面)
这是主要的时间标签编辑界面。

- **职责**：
    - 类似节奏游戏的打轴体验，全键盘驱动；实时预览卡拉OK走字 (KaraokePreview)。
    - **工具栏**：「修改所选字符」(ModifyCharacterDialog) 修改文本/注音/节奏点数量，可注册到用户字典，保持停顿点标记与演唱者继承；「插入导唱符」(InsertGuideSymbolDialog) 在选中字符前批量插入导唱字符（如 `>`），自动计算时间戳并设置 `linked_to_next`。撤销/重做保留 Ctrl+Z/Y 快捷键。
    - **查询候补字典**（`dict_candidate_dialog`）：批量变更 / 修改所选字符 / 编辑字符(F2) 三个对话框在执行(确定)与取消之间新增「查询候补字典」按钮。点击弹出 `DictCandidateDialog`，列出词典（本地 + 启用网络源，经 `load_effective_dictionary`）中与原文**完全匹配**的条目；双击或选中点「应用」后，按所选条目的 annotated reading 完整解析（RubyPart 按 `|` 拆 mora、check_count = RubyPart 数、同 block 相邻字符连词），强制「直接应用」分段模式填充字符行，随即执行原对话框的应用逻辑并关闭两个窗口。复用后端 `_parse_dict_reading`。
    - **变速控制**：Transport Bar QLineEdit 直接输入 20-200% 值（速度滑块范围 0.2×–2.0×），Q/W 快捷键 ±5% 步进，音量 Ctrl+Q/Ctrl+W ±5%。
    - **全局 Offset**：工具栏 QLineEdit 实时调整 -2000~2000ms，经 `Character.set_offset()` 联动 `global_timestamps`。
    - **单击跳转**：单击字符跳到其第一个 checkpoint；无 checkpoint 的字符（check_count=0 且非停顿点）视觉焦点保持该字符（便于添加节奏点），内部位置仍移到最近有效 checkpoint。
    - **渲染**：逐字高亮同步 Ruby；无时间戳字符按邻近组时间连读；连词组合并渲染。
    - **Enter 换行**：在编辑模式下按 Enter 键，在当前字符处插入换行，将当前行拆分为两行。
    - **Shift+Enter 合并上一行**：在编辑模式下按 Shift+Enter 键，将当前行合并到上一行末尾。
    - **Delete 删除**：在编辑模式下按 Delete 键，删除选中字符（支持划词多选）。
    - **右键菜单**：右键点击字符弹出上下文菜单，提供删除字符、删除时间戳、插入空格、合并行、删除行、插入空行、增减节奏点、切换停顿点、设置演唱者等快捷操作。

### 2. EditInterface (行编辑界面)
提供基于表格的项目全局视图。

- **职责**：
    - 展示所有歌词行及其元数据（行号、演唱者、连词标记等）。
    - **行级操作**：添加行、删除行、复制行、插入行，支持多行选择。
    - **键盘快捷键**：Ctrl+C 复制行、Ctrl+V 粘贴行、Delete 删除行。
    - **LineDetailDialog**：双击行打开详细编辑对话框，可逐个 Character 编辑注音、节奏点、停顿点标记（is_sentence_end）和演唱者。
    - **批量变更对话框**：支持多字符划选自动填充注音；移除"将匹配词设为连词"复选框，变更为继承原字符的连词属性。
    - **字符级操作**：在行详情中支持添加、删除、复制、插入字符，Ctrl+C/V/Delete 快捷键。
    - 操作后自动维护领域不变量（is_line_end、is_sentence_end、check_count、linked_to_next）。停顿点字符允许 check_count=0（无普通节奏点时以前一个字符的 checkpoint 为开始）。

### 3. RubyInterface (全文本编辑（已废弃不建议使用）界面)
专注于注音编辑和文本调整。

- **职责**：
    - **全类型自动注音**：汉字、平假名、片假名、英文、数字、符号、空格全部生成注音；「按类型删除注音」可按字符类型批量清除（含長音ー/～、♪、小假名 ぁぃぅぇぉゃゅょゎっ 等）。
    - **连词格式**：`{text|r1,r2,...}` 按 `linked_to_next` 链条分组各字独立读音（如 `{大冒険|だい,ぼう,けん}`、`{One|ワン,,}`）。向后兼容旧版 `漢字{かんじ}`。
    - **自由文本编辑**：直接修改歌词文本，文本未变更时仅更新标注注音不清除未标注；文本变更时通过 SequenceMatcher 保留匹配字符的注音。
    - **自动应用**：切换标签页离开时自动应用修改回项目数据。
    - **更新节奏点**：根据注音与 AutoCheck 规则重新计算（更新前自动应用文本框内容）。

### 4. SettingsInterface (设置界面)
管理全局配置与校准工具。

- **职责**：
    - **Offset 校准（可视化弹窗）**：深色画布上 2 个白色滑块按 BPM 匀速滑过中央红色判定线（接近时放大），`sd.OutputStream` 实时生成节拍器点击音，spin-wait 亚毫秒级定时；空格敲击记录偏移，左上角实时显示最近/平均偏移；BPM 60-240 可调；窗口关闭调 `_stop_metronome()` 清理。
    - **打轴设定**：默认打轴偏移 `tag_offset_ms`（默认 0ms）；全局偏移按项目存于 `Project.global_offset_ms`（渲染/导出共用同一套 `global_timestamps`）；微调时间戳步长 `timing_adjust_step_ms`（默认 10ms）；后退/前进默认 2000/3000ms。快捷键可在「设置 → 快捷键」配置（默认 `add_checkpoint`=`[`、`remove_checkpoint`=`]`、`speed_down`/`speed_up`=Q/W、`toggle_line_end`=`.`、`toggle_word_join`=F3 等），支持双绑定与长按绑定（按键持续 300ms 以上触发不同功能）。
    - **Auto Check 设置**：小写假名 check 开关、停顿点判定开关。
    - **定时自动保存**：启用开关 + 间隔 1~60 分钟。
    - **用户字典管理**：条目按优先级排列（新增置顶），上移/下移调整；支持导入 RL 字典（`原文\t注音1,注音2...`，`＋` 代表连词）；独立存储于 `dictionary.json`，首次启动自动从内嵌 1757 条初始化，重置配置不影响字典。
    - **配置管理（About 界面）**：「打开目录」「更改位置」(通过 `.config_redirect`)；`config/` 作为 package data 嵌入，缺失时回退内嵌版本。

### 5. KaraokePreview (卡拉OK预览)
打轴界面内嵌的实时走字预览组件。

- **职责**：
    - 逐字高亮渲染 + Ruby 同步走字；无时间戳字符按邻近组时间均分连读；连词组合并渲染 Ruby 与连词框。
    - 使用 `ch.global_timestamps` 和 `ch.global_sentence_end_ts`（按全局偏移预计算，渲染与导出共用同一套），消除双重偏移逻辑。
    - **渲染缓存**：每帧复用逐句渲染数据（字符宽度、分组、wipe 时间、连词组），仅在数据变更时重算，降低 60fps CPU 开销。
    - **跨字符多选**：普通左键拖拽 = 单段整字选区（含跨行，原行为）；`Ctrl`+单击 = 切换单字，`Ctrl`+拖拽 = 追加整字范围，支持不相邻/跨行。扩展选区以字符集合保存，`get_selected_char_ranges()` 返回逐行分段；`get_normalized_selection()` 只在单段选区时返回，避免调用方误用不完整范围。直接支持应用演唱者、注音分析/删除、时间戳调整/删除、节奏点与停顿点批量操作、删除字符、按连续段设置/取消连词；需要单字/单行语义的操作由 `EditorInterface._collapse_extended_selection_for_action()` 先退出扩展选区、只保留最后选中字符再执行（复制/粘贴为单 InfoBar 不额外提示）。全文本/行详情/自动导唱等独立工具打开前会先清除扩展选区。

## 前端目录结构

| 路径 | 职责 |
|------|------|
| `frontend/main_window.py` | `MainWindow` 主窗口框架 + 侧边栏导航 |
| `frontend/editor/timing_interface.py` | `EditorInterface` 主类（打轴主控） |
| `frontend/editor/timing/` | 从 `timing_interface` 拆出的子组件：`TransportBar` / `EditorToolBar` / `KaraokePreview` / `TimelineWidget` / `ModifyCharacterDialog` / `InsertGuideSymbolDialog` / `CharEditDialog` / `BulkChangeDialog` / `_SentenceSnapshotCommand` |
| `frontend/editor/line_interface.py` | `EditInterface` 行表格编辑 |
| `frontend/editor/fulltext_interface.py` | `RubyInterface` 全文本/注音编辑 |
| `frontend/editor/timing/bulk_change_dialog.py` | 批量字符替换对话框（通过 `_SentenceSnapshotCommand` 支持撤销） |
| `frontend/settings/settings_interface.py` | `SettingsInterface` 主界面 |
| `frontend/settings/{app_settings,cards,dictionary_dialog,nicokara_dialog,calibration_dialog}.py` | 设置界面的配置模型、卡片控件、字典/导出/校准对话框 |
| `frontend/singer/singer_interface.py` | `SingerInterface` 演唱者管理 |
| `frontend/home/home_interface.py` | `HomeInterface` 项目创建入口 |
| `frontend/export/export_interface.py` | `ExportInterface` 导出配置界面 |
| `frontend/online/online_interface.py` | `OnlineQueryInterface` 在线查询界面（占位） |
| `frontend/theme.py` | 主题管理器（深色/浅色/跟随系统） |
| `frontend/project_store.py` | 项目状态存储 |
| `frontend/workers.py` | 后台工作线程 |

为保留历史 `from ...editor.timing_interface import X` 和 `from ...settings.settings_interface import X` 路径，主模块对拆出的符号进行了 re-export。

## 辅助界面
- **MainWindow**：FluentWindow 主框架 + 侧边栏导航。图标 `resource/icon.ico`（开发/PyInstaller 双兼容）；全局 Ctrl+S 保存（无路径弹另存为）；切换标签页时自动应用全文本编辑修改；启动时询问闪退恢复；未保存关闭弹中文按钮（保存/放弃/取消）。
- **HomeInterface**：项目创建入口（侧边栏导航中的「主页」）。启动后默认进入打轴界面，用户可直接拖入歌词/音频/项目文件开始工作，也可导航到主页手动创建项目。
    - **导入流程**：拖入/选择文件时仅读原始内容到文本框；格式解析延迟到「创建项目」时执行。
    - **格式检测**：自动识别 ASS / SRT / LRC（逐行/逐字/增强型）/ Nicokara / 内联 / 纯文本；支持 .lrc/.txt/.kra/.ass/.srt 歌词与 .mp3/.wav/.flac/.ogg 音频拖拽。
    - **演唱者传播**：创建项目时默认演唱者 ID 传播到 sentence / character / ruby 三层，避免 singer_id 留空；Nicokara 通过 singer_key 映射达到同效果。
- **ExportInterface**：导出配置界面。演唱者过滤自动仅列当前实际使用者（QScrollArea 可滚动）；「插入【演唱者名】标签」作为独立设置项，与过滤无关。
