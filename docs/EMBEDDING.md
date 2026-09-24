# 嵌入契约（Embedding Contract）

本文件描述 StrangeUtaGame（以下简称 SUG）作为**子模块嵌入宿主程序**（当前为 karaoke-studio 工作台）时的接口契约。

> **为什么有这份文档**：SUG 既能 standalone 独立运行，也能被宿主嵌入。嵌入相关的代码（"embedded hook"）住在 SUG 自己的源文件里。当 SUG 未来分离成独立仓库 / submodule 时，**这份文档就是 SUG 与宿主之间的边界合同** —— 改动这些接口前，双方都应知道契约，避免破坏嵌入。
>
> 配套的回归测试见 [`tests/unit/test_embedded_contract.py`](../tests/unit/test_embedded_contract.py)。改 embedded 代码后跑它，确认契约没破。

---

## 两种运行模式

| | standalone（默认） | embedded |
|---|---|---|
| 触发 | `MainWindow()` / `python main.py` | `MainWindow(embedded=True)` / `MainWindow.for_embedding(...)` |
| 窗口 | 顶层 `MSFluentWindow` | 降级为子 widget（`Qt.WindowType.Widget`），由宿主放进自己的 layout |
| 配置 | 文件（`config.json` 等） | 宿主注入的 `SettingsProvider` |
| 缓存 | `程序目录/.cache` | `SUG_CACHE_DIR` 环境变量指向的目录 |
| 日志 | `程序目录/logs`（不可写时 `~/.strange_uta_game/logs`） | `SUG_LOGS_DIR` 环境变量指向的目录 |
| 顶层行为 | 全部自管 | 跳过（见下），由宿主管 |

**核心不变量：embedded 的一切惰性化 —— 当 `embedded=False` 且 provider 为 None 且 `SUG_CACHE_DIR` 未设时，SUG 行为必须跟没有嵌入支持时逐字节一致。** 这是 SUG 能独立分发的前提。

---

## 1. 构造接口

`frontend/main_window.py`：

```python
class MainWindow(MSFluentWindow):
    _embedded: bool = False  # 类级 fallback

    def __init__(self, embedded: bool = False, settings_provider=None, ai_timing_host=None):
        # ⚠ _embedded / _settings_provider 必须在 super().__init__() 之前赋值。
        #   MSFluentWindow init 会触发 resizeEvent/changeEvent，那些 handler
        #   读 self._embedded；未赋值会 AttributeError，Qt C++ 事件分发无法
        #   捕获 Python 异常，进程直接 0xC0000409 崩溃。
        self._embedded = embedded
        self._settings_provider = settings_provider
        super().__init__()

    @staticmethod
    def for_embedding(parent=None, settings_provider=None, ai_timing_host=None) -> "MainWindow":
        # 构造 embedded 实例，剥离顶层窗口装饰，挂到 parent。宿主拿去 addWidget。
```

**契约**：宿主用 `for_embedding(parent, settings_provider, ai_timing_host=...)` 创建，得到一个可直接 `addWidget` 到任意 layout 的 widget。

## 2. 宿主调用的公开方法

| 方法 | 用途 |
|---|---|
| `trigger_save() -> bool` | 宿主把自己顶层的 Ctrl+S 转发到这里；返回是否成功发起异步保存 |
| `has_unsaved_changes() -> bool` | 宿主 closeEvent 用，判断是否有脏数据 |
| `flush_unsaved()` | 宿主销毁 widget 前调用，把脏数据兜底写到崩溃恢复临时文件 |
| `export_to_next_payload() -> dict \| None` | 获取项目、角色、Nicokara 标签和媒体路径的隔离快照，供宿主送往下一模块；含 `axis_plan` 分色分轴计划（§2.1） |
| `on_host_visibility_changed(visible: bool)` | 宿主切入/切出整个 SUG 控件时调用；切出时只要音频仍在播放就立即暂停（不受“离开打轴界面时暂停”设置约束），并从音频实际状态同步快捷键模式；同时转发给前后台节流器——宿主隐藏 SUG 区域而宿主窗口仍可见时，只有这条显式通知能让非音频服务（UI 轮询/主题轮询等）降频 |

embedded 实例还公开 `export_to_next_requested` 信号。SUG 的导出页仅在
embedded 模式显示“进入下一步”按钮；点击后直接发出该信号（分组编辑在
「导出字幕分组」小窗完成，见 §2.1），宿主收到后调用
`export_to_next_payload()`。standalone 模式不创建这个按钮，原导出流程
不变。

### 2.1 分色分轴计划（axis_plan）

同一 SUG 项目可按演唱者（分色）拆成宿主侧的多个轴文件。编辑入口是导出页
的**「导出字幕分组」小窗**（Nicokara / Kirakara 格式显示，由原「演唱者过
滤」升级而来）：默认只显示分组摘要（每组一行胶囊卡片：主分组星标 + 组名
+ 成员色点；未分组 / 未入组单独提示），点「修改分组...」弹出大对话框编
辑。对话框约束：竖排卡片横向排列，向右追加/删除分组（至少保留 1 组），
每组独立勾选演唱者、组名必填且不得重复；必须且只能有一个「主分组」。
**组内不勾选任何演唱者 = 该轴包含全部演唱者**（沿用过滤器「不勾选则导
出全部」的口径；payload 中物化为当前全部歌手 id）。确认后写回
`project.axis_groups` 并标脏。

小窗与对话框的自绘部分（胶囊行底色/主分组徽标/成员色点/幽灵「添加分组」
按钮）全部纳入 SUG 主题单例管理：色值取自 `theme`，切主题时整行/整卡重
建或重涂；演唱者原色做 `theme.ensure_contrast` 对比度校正（浅色主题下亮
色自动加深），保证深浅两套下可读。

embedded「进入下一步」**不再自动弹窗**：分组编辑统一在小窗完成，按钮点
击直接发 `export_to_next_requested` 信号，宿主随后的
`export_to_next_payload()` 读取当前 `project.axis_groups`（空 = 单轴）。

standalone「导出」按组拆分（embedded 宿主不经过此路径，仅供理解语义）：

- 存在 **1 个以上**分组且格式支持演唱者过滤（Nicokara / Kirakara）时，
  按组导出多个文件，文件名追加 `_分组名`；单个分组 = 按该组过滤的普通
  导出（不加后缀）。其他格式忽略轴分组。
- 每组文件的 @Emoji 标签按**本组实际使用的演唱者**解析触发词
  （【演唱者名】或裸名），只保留能对应上的行。
- **主分组**的文件携带完整标签信息（@Title/@Artist/@Album/@TaggingBy +
  非 @Emoji 的 custom 行）；非主分组只带本组 @Emoji；计时字段
  （@Offset/@HeadOffset/@SilencemSec）所有文件保留。

`export_to_next_payload()` 返回值中的 `axis_plan` 为纯 dict 快照：

```python
{
    "mode": "single" | "split",   # split = axis_groups 非空
    "groups": [
        {
            "name": "轴1",
            "singer_ids": ["uuid-a", "uuid-b"],
            "is_primary": True,    # 有且仅有一个主分组
            "singers": [  # 冗余快照（宿主免反查 project）
                {"id": "...", "name": "初音ミク", "color": "#FF6B6B",
                 "color_mode": "solid", "split_colors": [...]},
            ],
        },
    ],
    "unassigned": ["uuid-c"],  # 未进入任何组的启用演唱者 ID
}
```

**每组的内容口径**（与 Nicokara 导出的演唱者过滤一致，见
`nicokara_exporter.py`）：行内任一字符属于组内演唱者则保留整行（空行无
条件保留），行内组外字符剔除。同一演唱者可同时属于多组；未入组演唱者
的文本不进入任何轴。

分组随 `.sug` 持久化（`axis_groups` 键，仅非空时写入；旧文件缺省 = 单
轴）。宿主从磁盘加载 `.sug` 时可读到同一份数据，与 payload 快照同源。

异步保存完成后发出 `project_save_finished(str)`，失败时发出
`project_save_failed(str)`。宿主需要“保存后继续”的流程时，应等待对应信号，
不能把 `trigger_save()` 返回 `True` 当作已经写入磁盘。

宿主的外层页面切换不会进入 SUG 的 `switchTo()`。因此宿主应在隐藏 SUG 前调用
`on_host_visibility_changed(False)`，并在重新显示后调用
`on_host_visibility_changed(True)`。SUG 的 embedded `hideEvent/showEvent` 也会执行相同
同步作为兜底；接口是幂等的，显式通知与 Qt 事件重复到达不会重复暂停。

上述通知同时驱动前后台性能节流（`frontend/background_throttle.py`）：不可见时
非音频服务降频（编辑页播放头轮询降到 200ms、Win10 主题轮询暂停等），音频
播放不受影响。不走 `MainWindow` 嵌入路径的宿主也可以直接调用模块级
`strange_uta_game.frontend.background_throttle.set_visibility_override(visible)`：
`False` 强制按隐藏处理，`True`/`None` 恢复自动判定（窗口最小化等自动检测仍生效）。

## 3. 设置后端：SettingsProvider

`frontend/settings/app_settings.py`：

```python
@runtime_checkable
class SettingsProvider(Protocol):
    def load(self) -> dict: ...                       # 主 config（config.json 等价）
    def save(self, data: dict) -> None: ...
    def load_extra(self, key: str, default): ...      # key ∈ {"dictionary","singers","network"}
    def save_extra(self, key: str, data) -> None: ...
```

- `AppSettings(provider=<obj>)`：provider 模式 —— 不碰文件系统，主 config 走 `provider.load/save`，词典/演唱者/网络走 `provider.load_extra/save_extra`。
- `AppSettings.set_default_provider(provider)`：**进程级全局默认**。SUG 代码里散落大量裸 `AppSettings()` 调用，靠这个让它们自动走宿主存储。`for_embedding` 内部会调它。优先级：显式 `provider=` 参数 > `_default_provider` > 文件模式。
- **边界 deepcopy**：进出 provider 的数据都 deepcopy，防止宿主与 SUG 共享嵌套引用导致互相污染。

## 4. 运行时路径注入

| 注入项 | 形式 | SUG 侧读取点 |
|---|---|---|
| 缓存目录 | `SUG_CACHE_DIR` **环境变量** | 三处 `_get_cache_dir()`（`frontend/project_store.py`、`backend/infrastructure/audio/tsm_cache.py`、`.../video_converter.py`）优先读它。`project_store` 的缓存路径已**惰性化**（`_cache_dir()`/`_untitled_temp_path()` 函数，非 import 期常量），避免 import 时机固化错路径。 |
| 日志目录 | `SUG_LOGS_DIR` **环境变量** | `app_dirs.logs_dir()` 优先读它。`logs/` 下两类文件都跟随：`ai_timing.log`（AI 打轴模块统一日志，`backend/application/ai_timing/ailog.py`，弹窗「日志」按钮可一键定位）与 `crash.log`（`frontend/crash_guard.py` 全局异常兜底）。宿主不设时两者落在宿主 exe 旁的 `logs/`（不可写时回退 `~/.strange_uta_game/logs`）。 |
| ffmpeg 路径 | 配置键 `tools.ffmpeg_path`（主 config namespace） | `video_converter.get_ffmpeg_path()` |

**注意**：`SUG_CACHE_DIR` 必须在 import SUG 任何模块**之前**设置（虽已惰性化降低风险，但宿主仍应尽早设）。`SUG_LOGS_DIR` 同理尽早设置——`logs_dir()` 惰性解析，但 AI 打轴的 worker 子进程由宿主侧 SUG 进程经 `SUG_AI_TIMING_LOG`（内部机制，宿主无需关心）传递最终路径，两进程写同一份文件。

另有一份日志**不**跟随 `SUG_LOGS_DIR`：AI 打轴的安装流水 `install.log` 始终写在运行环境目录（`<ai_runtime>/install.log`，嵌入模式即宿主托管 runtime 所在处），因为它与运行环境共存亡、随环境重建。

## 5. embedded 模式下 SUG 内部的行为契约

`embedded=True` 时，SUG **跳过 / 隐藏 / noop** 以下（全部由宿主接管）：

**跳过**（`if not self._embedded`）：
- 窗口几何持久化（`_win_settings`、几何定时器、resize/change 存盘）
- 全局 Ctrl+S 快捷键注册（改由宿主转发 `trigger_save`）
- 启动期定时器：崩溃恢复弹窗、应用 updater 自检
- `_init_window` 的全局主题 / 标题 / 尺寸 / 居中（只保留 widget 本地背景兜底）
- `closeEvent` 的 `QApplication.quit()`（embedded 下会杀掉宿主进程）
- **全局主题写入**：`SettingsInterface._apply_theme_setting` 在 embedded 下直接
  return —— 不能 set `theme.mode`，否则会通过 `_sync_app_palette()` 掀翻
  宿主 `QApplication.palette()` 并调 `qfluentwidgets.setTheme`，导致工作台
  出现"半亮半暗"崩坏画面。主题归宿主独占（host 通过 `theme_workbench`
  adapter 驱动同一个 SUG `theme` 单例）。

**隐藏 UI**：
- `tools_group`：ffmpeg 路径选择入口（宿主统一管理 ffmpeg；由 provider 判定）
- `_path_card`：配置文件位置卡片（embedded 下配置走宿主，无文件目录概念；由 provider 判定）
- `card_theme`：主题选择卡（embedded 下主题归宿主"界面"设置独占；由 provider 判定）
- `card_high_dpi_scaling`：由显式 `embedded=True` 判定（旧调用兼容 provider
  判定）。进程 DPI awareness 必须由宿主在创建 `QApplication` 前决定，
  嵌入后的 SUG 不能独立切换

**改走 provider**（不访问 standalone 文件）：
- `load/save_dictionary`、`load/save_singer_presets`、`load/save_network_dictionary`（走 `load_extra/save_extra`）
- 网络词典启动自动更新仍会调度；`maybe_auto_update_network_dictionary` 通过
  provider 读取源配置并写回 cache namespace 与更新时间戳。

## 6. AI 打轴宿主能力：ai_timing_host（可选注入）

`for_embedding(..., ai_timing_host=...)` 接受一个满足
`backend/application/ai_timing/host.py::AiTimingHost` 协议的对象（鸭子类型，
宿主不导入 SUG 代码）。传入后 SUG 的「AI 打轴」使用宿主能力；传 `None`
（或省略）时回落 standalone 默认配置，两种模式共用同一弹窗与核心逻辑。

| 方法 | 语义 |
|---|---|
| `separation_status() -> dict` | 工作台分离环境状态 `{available, model, message}`；`available` 涵盖「已配置但服务未运行」（INSTALLED_STOPPED/STARTING）——`separate_vocal` 执行前会自动拉起/等待服务 |
| `effective_identity() -> dict` | 当前生效分离身份 `{model, stem, params}`（人声缓存键组成） |
| `find_session_vocal(source_path, media_sha256) -> Path \| None` | 本次会话已分离、与原音频匹配的人声（零分离复用） |
| `separate_vocal(source_path, on_progress, is_cancelled) -> Path` | 阻塞执行一次工作台人声分离，返回产物路径；取消/失败抛中文异常 |
| `ai_cache_dir() -> Path` | SUG AI 缓存根目录（宿主 `.cache` 范围，§7.2） |
| `model_root() -> Path`（可选） | 统一 AI 模型根目录（对齐模型与分离模型同源管理，嵌入模式复用工作台目录）；缺省 = SUG 自身默认目录 |
| `http_proxy() -> str`（可选） | 当前生效的下载代理 URL；空串/缺省 = 不显式代理（SUG 模型下载默认跟随宿主网络设置，standalone 则用 SUG 自身的「网络与代理」设置） |
| `runtime_python() -> str \| None`（可选，方案 B） | 宿主托管 PyMSS Runtime 的 `python.exe`。路径存在时：SUG 的「安装/修复」向该解释器**增量**安装 AI 依赖（`install_shared`：不建 venv、不重装 torch，torchaudio 按其 torch 版本/变体自动配对），embedded 的对齐 worker 与分离共享同一份 torch（含 CUDA）；返回 None / 路径不存在 = 嵌入模式引导去宿主分离页安装，或经用户确认后独立安装兜底（用户自选/自装解释器优先于托管值）。能力发现用 `getattr`，不实现不影响其余协议 |
| `note_runtime_changed() -> bool`（可选，方案 B 配套） | SUG 增量安装完成后的通知：pip 会升级/降级宿主清单登记在案的共用包（如 audio-separator 需要的 librosa 降级），宿主据此按磁盘现状重登记 installed manifest，否则其下次启动的完整性校验报「文件缺失或损坏」。返回 False / 未实现时 SUG 静默跳过；standalone 无宿主不触发 |
| `stop_separation_service(timeout_s=30) -> dict`（可选，方案 B 配套） | SUG 增量安装（`install_shared`）**开 pip 前**调用：宿主分离服务进程已加载的 torch 等 `.pyd` 锁住 runtime 的 site-packages，pip 覆盖被锁文件可能半失败、留下新旧混杂的安装。宿主应**阻塞等到服务真正停止**（穿过 `SERVICE_STOPPING` 等异步中间态）才返回 `{"stopped": bool, "message": str}`，保证 pip 启动时文件锁已释放；有分离任务在执行时拒绝（`stopped=False` + 中文原因，不打断任务）；服务本就没跑时幂等返回 `stopped=True`。装完由 `note_runtime_changed` 重登记并重启服务；装前没跑则无需显式恢复——`separate_vocal` 执行时自动拉起。未实现时 SUG 静默跳过 |
| `open_separation_page() -> bool`（可选） | 跳转到宿主分离环境页（工作台第 2 步「分离人声」）；返回 False / 未实现时 SUG 回落为文字提示 |

embedded 语义（对应主仓库 AI 打轴计划 §6.2）：跟随工作台当前「分离人声」
设置，不安装第二份 PyMSS Runtime、不复制模型、不在 SUG 设置中保存另一套
分离参数。宿主实现见工作台
`krok_helper/audio_processing/separation/ai_timing_host.py`。

## 7. 宿主侧职责（工作台，分离后**不**跟 SUG 走）

以下在宿主仓库实现，仅列出供理解契约全貌：
- `KrokHelperSettingsBridge`（实现 `SettingsProvider`，桥到工作台 settings.json 的 `lyrics_timing*` 字段）
- `_sync_lyrics_timing_host_paths`（注入 `SUG_CACHE_DIR` + `tools.ffmpeg_path`）
- 启动时一次性迁移老 SUG 配置（`migrate_strange_uta_game_settings`）
- `KaraokeAiTimingHost`（实现 §6 的 `AiTimingHost` 协议，注入给 SUG AI 打轴）

## 8. 字体缓存与预热（宿主可选复用）

`frontend/font_cache.py` —— SUG 所有字体枚举的进程级缓存。宿主进程同样可能携带庞大字体库（数百上千族），可直接复用：

| 接口 | 语义 |
|---|---|
| `prewarm(include_picker_entries=True)` | 同步预热：字体族快照 + 字体选择器条目。适合放在宿主启动的空闲阶段（如闪屏/加载页）。**Qt 侧调用须在主线程。** |
| `prewarm_async(qt_delay_ms=1500, include_picker_entries=True)` | 后台预热：本地化字体名磁盘扫描（纯 stdlib）进后台线程；Qt 侧枚举经 `QTimer.singleShot` 排回主线程。幂等，重复调用不会重建缓存。SUG standalone 启动即自行调用，嵌入时宿主可再调一次（成本为零）。 |
| `invalidate(clear_alias_map=True)` | 清空缓存。宿主运行期安装/卸载系统字体、或调用 `QFontDatabase.addApplicationFont` 之后必须调用，否则 SUG 界面看不到新字体。`clear_alias_map=False` 可保留昂贵的字体文件名扫描结果（确认字体文件未变时）。 |
| `installed_families()` / `installed_family_map()` / `has_installed_family(family)` | 缓存的字体族枚举（供宿主自己的字体 UI 复用，避免各自重复枚举）。 |
| `font_picker_entries()` | 缓存的 `[(族名, 显示名, 搜索文本)]` 条目（含本地化名，已过滤位图字体）。 |

不调用任何接口也安全：缓存全部懒加载，首次访问自动构建——预热只是把成本从"用户首次打开字体选择器"挪到启动空闲期。

## 9. 契约稳定性约定

改动 §1–§6 及 §8 的任何签名 / 行为，视为**破坏性变更**，需：
1. 先更新本文档
2. 跑 `tests/unit/test_embedded_contract.py` 确认（或同步更新测试）
3. 通知宿主维护方

standalone 行为（`embedded=False` 路径）**绝不能**因 embedded 改动而回退 —— 这是 SUG 独立分发的红线。
