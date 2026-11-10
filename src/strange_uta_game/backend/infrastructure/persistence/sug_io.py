"""SUG 项目文件解析器

StrangeUtaGame 项目文件格式 (.sug)
基于 JSON，包含完整的项目数据。

版本历史:
  - v1.0: 初始版本（lines + checkpoints + timetags + rubies 分离存储）
  - v2.0: 层次化模型（sentences + characters 一体化存储）
  - v0.3.0: Ruby 分组模型（Ruby.text 支持 # 分组）
"""

import json
import os
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path
from datetime import datetime
from uuid import uuid4

from strange_uta_game.backend.domain import (
    Project,
    ProjectMetadata,
    Singer,
    AxisGroup,
    Sentence,
    Character,
    Ruby,
    RubyPart,
)
from strange_uta_game.backend.domain.models import (
    RUBY_PAUSE_SENTINEL,
    get_ruby_pause_char,
    pause_char_variants,
)


class SugParseError(Exception):
    """SUG 文件解析错误"""

    pass


def _split_ruby_text(ruby_text: str, char_count: int) -> List[str]:
    """将 ruby 文本拆分到多个字符（用于 v1.0 迁移）

    按字符数均匀分配。字符数 <= 目标数时一对一分配，
    多余位置为空字符串；字符数 > 目标数时均匀合并。

    Args:
        ruby_text: 注音文本
        char_count: 需要分配到的字符数量

    Returns:
        拆分后的文本列表，每个元素对应一个字符
    """
    if char_count <= 0:
        return [ruby_text] if ruby_text else []
    if char_count == 1:
        return [ruby_text]

    chars = list(ruby_text)
    if len(chars) <= char_count:
        return chars + [""] * (char_count - len(chars))

    # 字符数 > 目标数：均匀合并
    result = []
    base = len(chars) // char_count
    extra = len(chars) % char_count
    pos = 0
    for i in range(char_count):
        size = base + (1 if i < extra else 0)
        result.append("".join(chars[pos : pos + size]))
        pos += size
    return result


class SugMigrator:
    """SUG 文件版本迁移器

    处理不同版本之间的数据迁移。
    """

    CURRENT_VERSION = "0.3.0" # 实际已更新至0.3.2，但是没有更改数据因此不更改

    @classmethod
    def migrate(cls, data: Dict[str, Any], from_version: str) -> Dict[str, Any]:
        """将旧版本数据迁移到最新版本

        Args:
            data: 旧版本数据
            from_version: 原版本号

        Returns:
            迁移后的数据（当前版本格式字典）
        """
        if from_version == cls.CURRENT_VERSION:
            return data

        if from_version == "1.0":
            data = cls._migrate_v1_to_v2(data)
            from_version = "2.0"

        if from_version == "2.0":
            return cls._migrate_v2_to_v0_2_0(data)

        # 未知版本，原样返回（由解析器尝试处理）
        return data

    @classmethod
    def _migrate_v1_to_v2(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """v1.0 → v2.0 迁移

        将旧的 lines/checkpoints/timetags/rubies 分离存储格式
        转换为新的 sentences/characters 层次化存储格式。
        """
        result = {
            "version": "2.0",
            "id": data.get("id", ""),
            "metadata": data.get("metadata", {}),
            "audio_duration_ms": data.get("audio_duration_ms", 0),
            "singers": data.get("singers", []),
            "sentences": [],
        }

        for line_data in data.get("lines", []):
            sentence_data = cls._migrate_line_to_sentence(line_data)
            result["sentences"].append(sentence_data)

        return result

    @classmethod
    def _migrate_v2_to_v0_2_0(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """v2.0 → v0.3.0 迁移。

        丢弃旧的 char-level Ruby，基于当前 sentence 文本和 checkpoint
        重新分析生成带 # 分组的 Ruby 文本。
        """
        from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
            analyze_sentence_ruby,
        )

        migrated = dict(data)
        migrated["version"] = cls.CURRENT_VERSION

        migrated_sentences: List[Dict[str, Any]] = []
        pause_char = get_ruby_pause_char()
        placeholder_texts = pause_char_variants(pause_char) | {""}
        for sentence_data in data.get("sentences", []):
            sentence_dict = dict(sentence_data)
            sentence = SugProjectParser._dict_to_sentence(sentence_dict, pause_char)
            analyze_sentence_ruby(sentence)
            migrated_sentences.append(
                SugProjectParser._sentence_to_dict(sentence, placeholder_texts)
            )

        migrated["sentences"] = migrated_sentences
        return migrated

    @classmethod
    def _migrate_line_to_sentence(cls, line_data: Dict[str, Any]) -> Dict[str, Any]:
        """将 v1.0 的 line dict 转换为 v2.0 的 sentence dict

        转换规则:
          1. chars + checkpoints → characters（合并属性）
          2. timetags 按 char_idx 分组 → character.timestamps[]
          3. rubies 按 start_idx/end_idx 拆分到各 character
          4. check_count==0 → 前一字符 linked_to_next=True
        """
        line_singer_id = line_data.get("singer_id", "")
        text = line_data.get("text", "")
        chars = line_data.get("chars", list(text) if text else [])

        # 修复 chars/text 不一致
        if text and "".join(chars) != text:
            chars = list(text)

        # ── 1. 构建 checkpoint 映射: char_idx → checkpoint dict ──
        cp_map: Dict[int, Dict[str, Any]] = {}
        for cp in line_data.get("checkpoints", []):
            idx = int(cp.get("char_idx", 0))
            cp_map[idx] = dict(cp)  # 浅拷贝，避免修改原数据

        # ── 2. linked_to_next 迁移: check_count==0 → 前一字符 linked_to_next ──
        # 空格字符不应触发连词（空格 check_count==0 是过滤规则的结果，不代表连读）
        sorted_indices = sorted(cp_map.keys())
        for i, idx in enumerate(sorted_indices):
            cp = cp_map[idx]
            ch_char = chars[idx] if idx < len(chars) else ""
            if ch_char and ch_char.isspace():
                continue
            if int(cp.get("check_count", 1)) == 0 and i > 0:
                prev_idx = sorted_indices[i - 1]
                cp_map[prev_idx]["linked_to_next"] = True

        # ── 3. 构建 timetag 映射: char_idx → {cp_idx: timestamp_ms} ──
        tag_map: Dict[int, Dict[int, int]] = {}
        for tag in line_data.get("timetags", []):
            char_idx = int(tag.get("char_idx", 0))
            cp_idx = int(tag.get("checkpoint_idx", 0))
            ts = int(tag.get("timestamp_ms", 0))
            if char_idx not in tag_map:
                tag_map[char_idx] = {}
            tag_map[char_idx][cp_idx] = ts

        # ── 4. 构建 ruby 映射: char_idx → ruby_text ──
        ruby_map: Dict[int, str] = {}
        for ruby_data in line_data.get("rubies", []):
            start = int(ruby_data.get("start_idx", 0))
            end = int(ruby_data.get("end_idx", 1))
            ruby_text = ruby_data.get("text", "")
            if not ruby_text:
                continue
            span = end - start
            if span <= 0:
                continue
            if span == 1:
                ruby_map[start] = ruby_text
            else:
                parts = _split_ruby_text(ruby_text, span)
                for offset, part in enumerate(parts):
                    if part:
                        ruby_map[start + offset] = part

        # ── 5. 组装 characters ──
        characters: List[Dict[str, Any]] = []
        for i, ch in enumerate(chars):
            cp = cp_map.get(i, {})
            check_count = max(int(cp.get("check_count", 1)), 0)

            # 从 timetag map 构建 timestamps 列表（按 cp_idx 顺序）
            timestamps: List[int] = []
            if i in tag_map:
                tag_indices = sorted(tag_map[i].keys())
                if tag_indices:
                    # 缺失的 cp_idx 不再填 0——那会把「未打轴的节奏点」
                    # 伪造成「0ms 打轴」（歌曲开头凭空多一个轴点）。
                    # 按 v2 语义取连续前缀，在首个缺口处截断
                    for cp_idx in range(tag_indices[-1] + 1):
                        if cp_idx not in tag_map[i]:
                            break
                        timestamps.append(tag_map[i][cp_idx])

            char_dict: Dict[str, Any] = {
                "char": ch,
                "check_count": check_count,
                "timestamps": timestamps,
                "linked_to_next": bool(cp.get("linked_to_next", False)),
                "is_line_end": bool(cp.get("is_line_end", False)),
                "is_rest": bool(cp.get("is_rest", False)),
                "singer_id": cp.get("singer_id", "") or line_singer_id,
            }

            if i in ruby_map:
                char_dict["ruby"] = {"text": ruby_map[i]}

            characters.append(char_dict)

        return {
            "id": line_data.get("id") or str(uuid4()),
            "singer_id": line_singer_id,
            "characters": characters,
        }


class SugProjectParser:
    """SUG 项目文件解析器

    负责 Project 对象的序列化和反序列化。
        支持 v0.3.0 格式读写，以及 v1.0/v2.0 格式向上兼容读取。
    """

    @staticmethod
    def save(
        project: Project,
        file_path: str,
        *,
        nicokara_tags: Optional[Dict[str, Any]] = None,
        media_path: Optional[str] = None,
        progress_cb=None,
    ) -> None:
        """保存项目到 SUG 文件

        Args:
            project: 项目对象
            file_path: 保存路径
            nicokara_tags: Nicokara 标签数据（可选）
            media_path: 实际媒体文件路径（可选，不含 .cache 临时路径）
            progress_cb: 可选进度回调 ``(stage: str) -> None``，在序列化和写盘两个阶段调用

        Raises:
            SugParseError: 保存失败
        """
        try:
            if progress_cb:
                progress_cb("正在序列化项目数据...")
            data = SugProjectParser.serialize(
                project, nicokara_tags=nicokara_tags, media_path=media_path
            )
            SugProjectParser.write(data, file_path, progress_cb=progress_cb)
        except SugParseError:
            raise
        except Exception as e:
            raise SugParseError(f"保存项目失败: {e}")

    @staticmethod
    def serialize(
        project: Project,
        *,
        nicokara_tags: Optional[Dict[str, Any]] = None,
        media_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """将 Project 序列化为当前版本的纯 dict 快照。

        快照只含 str/int/list 等值类型、不引用任何 domain 对象——构建完成
        那一刻即与后续编辑隔离，可安全交给后台线程写盘（见 write）。
        主线程成本为几 ms 级，远低于 deepcopy 整个 Project。
        """
        return SugProjectParser._project_to_dict(
            project, nicokara_tags=nicokara_tags, media_path=media_path
        )

    @staticmethod
    def write(data: Dict[str, Any], file_path: str, *, progress_cb=None) -> None:
        """将 serialize 产出的 dict 快照写入 SUG 文件（可放后台线程）。

        原子写：先写同目录临时文件再 ``os.replace`` 替换正式文件，
        写盘中途崩溃 / 断电不会留下截断的正式项目文件
        （os.replace 在同盘同目录下是原子操作）。
        """
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if progress_cb:
            progress_cb("正在写入文件...")
        temp_path = path.with_name(path.name + ".tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                # fsync 后再 replace：rename 只防进程崩溃（内存页缓存仍在），
                # 断电时数据未落盘会得到空/半截正式文件
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, path)
        except Exception:
            # 兜底清理临时文件；正式文件保持上一次写盘的完整内容
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @staticmethod
    def load(file_path: str) -> Project:
        """从 SUG 文件加载项目

        支持 v1.0、v2.0 和 v0.3.0 格式。旧文件会自动迁移到当前模型。

        Args:
            file_path: 文件路径

        Returns:
            项目对象

        Raises:
            SugParseError: 加载失败或文件损坏
        """
        project, _ = SugProjectParser.load_with_extras(file_path)
        return project

    @staticmethod
    def load_with_extras(file_path: str) -> Tuple[Project, Dict[str, Any]]:
        """从 SUG 文件加载项目，并同时返回附加字段（nicokara_tags、media_path）。

        单次解析同时产出 Project 与 extras，调用方不应为读取 extras
        重新解析整个文件。

        Args:
            file_path: 文件路径

        Returns:
            (项目对象, extras dict) — extras 仅包含文件中存在的字段

        Raises:
            SugParseError: 加载失败或文件损坏
        """
        path = Path(file_path)

        if not path.exists():
            raise SugParseError(f"文件不存在: {file_path}")

        try:
            # 先按字节整读（读盘系统调用期间释放 GIL，UI 线程可继续派发事件），
            # 再交 json.loads 直接解析 bytes：scanner 自动识别并跳过 BOM
            # （utf-8-sig 兼容），且跳过文本模式 TextIOWrapper 的增量解码与
            # 换行翻译层——大存档（数十 MB）实测解析耗时约降 15%、峰值内存
            # 略降。注意 json.loads 内部仍会 decode 成 str（bytes/str 短暂
            # 共存）、C scanner 本身仍持有 GIL，此改动的收益是边际而非量级。
            data = json.loads(path.read_bytes())

        except json.JSONDecodeError as e:
            raise SugParseError(f"JSON 解析错误: {e}")
        except Exception as e:
            raise SugParseError(f"读取文件失败: {e}")

        # extras 必须在版本迁移前提取：旧版本迁移（如 v1.0）会重建
        # 字典丢弃附加字段
        extras: Dict[str, Any] = {}
        if "nicokara_tags" in data:
            extras["nicokara_tags"] = data["nicokara_tags"]
        if "media_path" in data:
            extras["media_path"] = data["media_path"]

        try:
            # 版本检查和迁移（与 _dict_to_project 同一捕获面：损坏的旧版
            # 数据在迁移中途抛出的裸 ValueError/AttributeError 等统一转
            # SugParseError，不再直接炸到调用方）
            version = data.get("version", "1.0")
            if version != SugMigrator.CURRENT_VERSION:
                data = SugMigrator.migrate(data, version)

            project = SugProjectParser._dict_to_project(data)
        except SugParseError:
            raise
        except Exception as e:
            raise SugParseError(f"项目数据解析失败: {e}") from e

        return project, extras

    # ==================== 序列化 (Project → Dict) ====================

    @staticmethod
    def _project_to_dict(
        project: Project,
        *,
        nicokara_tags: Optional[Dict[str, Any]] = None,
        media_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """将 Project 对象转换为当前版本字典"""
        result = {
            "version": SugMigrator.CURRENT_VERSION,
            "id": project.id,
            "metadata": {
                "title": project.metadata.title,
                "artist": project.metadata.artist,
                "album": project.metadata.album,
                "language": project.metadata.language,
                "created_at": project.metadata.created_at.isoformat(),
                "updated_at": project.metadata.updated_at.isoformat(),
                "extra": dict(project.metadata.extra),
            },
            "audio_duration_ms": project.audio_duration_ms,
            "singers": [
                SugProjectParser._singer_to_dict(s) for s in project.singers
            ],
            "sentences": [],
        }
        placeholder_texts = pause_char_variants(get_ruby_pause_char()) | {""}
        result["sentences"] = [
            SugProjectParser._sentence_to_dict(s, placeholder_texts)
            for s in project.sentences
        ]
        # 仅当global_offset_ms有值时才写入（兼容性考虑）
        if project.global_offset_ms is not None:
            result["global_offset_ms"] = project.global_offset_ms
        # 轴分组：仅在已配置时写入（旧版 SUG 无此键，读取侧按缺省空处理）
        if project.axis_groups:
            result["axis_groups"] = [g.to_dict() for g in project.axis_groups]
        if nicokara_tags:
            result["nicokara_tags"] = nicokara_tags
        if media_path:
            result["media_path"] = media_path
        return result

    @staticmethod
    def _singer_to_dict(singer: Singer) -> Dict[str, Any]:
        """把 Singer 序列化为存档字典。

        TTML agent 来源信息只在非空时写入，旧版 .sug 不因此改变结构。
        """
        data: Dict[str, Any] = {
            "id": singer.id,
            "name": singer.name,
            "color": singer.color,
            "complement_color": singer.complement_color,
            "color_mode": singer.color_mode,
            "split_colors": singer.split_colors,
            "is_default": singer.is_default,
            "is_placeholder": singer.is_placeholder,
            "display_priority": singer.display_priority,
            "enabled": singer.enabled,
            "backend_number": singer.backend_number,
            "group": singer.group,
        }
        if singer.ttml_agent_id:
            data["ttml_agent_id"] = singer.ttml_agent_id
            data["ttml_agent_type"] = singer.ttml_agent_type
            data["ttml_agent_named"] = bool(singer.ttml_agent_named)
            if singer.ttml_agent_display_name:
                data["ttml_agent_display_name"] = singer.ttml_agent_display_name
            if singer.ttml_agent_source_name:
                data["ttml_agent_source_name"] = singer.ttml_agent_source_name
        if singer.ttml_role:
            data["ttml_role"] = singer.ttml_role
        return data

    @staticmethod
    def _sentence_to_dict(
        sentence: Sentence,
        placeholder_texts: Optional[set] = None,
    ) -> Dict[str, Any]:
        """将 Sentence 对象转换为字典"""
        if placeholder_texts is None:
            placeholder_texts = pause_char_variants(get_ruby_pause_char()) | {""}
        characters = []
        for char in sentence.characters:
            char_dict: Dict[str, Any] = {
                "char": char.char,
                "check_count": char.check_count,
                "timestamps": list(char.timestamps),
                "sentence_end_ts": char.sentence_end_ts,
                "linked_to_next": char.linked_to_next,
                "is_line_end": char.is_line_end,
                "is_sentence_end": char.is_sentence_end,
                "is_rest": char.is_rest,
                "singer_id": char.singer_id,
                "needs_guide": char.needs_guide,
                "is_guide": char.is_guide,
                "force_singer_tag": char.force_singer_tag,
            }
            if char.ruby:
                # 占位 part（停顿符及其全/半角变体）统一写为哨兵 token，使存档
                # 与用户的停顿符配置解耦；空串 part（历史遗留）一并规范化为哨兵，
                # 加载时映射回当前配置的停顿符。
                char_dict["ruby"] = {
                    "parts": [
                        {
                            "text": (
                                RUBY_PAUSE_SENTINEL
                                if p.text in placeholder_texts
                                else p.text
                            ),
                            "offset_ms": p.offset_ms,
                        }
                        for p in char.ruby.parts
                    ],
                }
            characters.append(char_dict)

        data: Dict[str, Any] = {
            "id": sentence.id,
            "singer_id": sentence.singer_id,
            "characters": characters,
        }
        if sentence.aux_role:
            data["aux_role"] = sentence.aux_role
            if sentence.aux_parent_id:
                data["aux_parent_id"] = sentence.aux_parent_id
            if sentence.aux_lang:
                data["aux_lang"] = sentence.aux_lang
            if sentence.aux_agent:
                data["aux_agent"] = sentence.aux_agent
            data["aux_order"] = int(sentence.aux_order)
            if sentence.aux_begin is not None:
                data["aux_begin"] = int(sentence.aux_begin)
            if sentence.aux_end is not None:
                data["aux_end"] = int(sentence.aux_end)
        if sentence.song_part:
            data["song_part"] = sentence.song_part
        return data

    # ==================== 反序列化 (Dict → Project) ====================

    @staticmethod
    def _dict_to_project(data: Dict[str, Any]) -> Project:
        """将当前/兼容版本字典转换为 Project 对象"""
        # 解析元数据（安全 datetime 解析）
        metadata_data = data.get("metadata", {})

        def _safe_datetime(value: Optional[str]) -> datetime:
            if value:
                try:
                    return datetime.fromisoformat(value)
                except (ValueError, TypeError):
                    pass
            return datetime.now()

        extra = metadata_data.get("extra")
        metadata = ProjectMetadata(
            title=metadata_data.get("title", ""),
            artist=metadata_data.get("artist", ""),
            album=metadata_data.get("album", ""),
            language=metadata_data.get("language", "ja"),
            created_at=_safe_datetime(metadata_data.get("created_at")),
            updated_at=_safe_datetime(metadata_data.get("updated_at")),
            extra=dict(extra) if isinstance(extra, dict) else {},
        )

        # 解析演唱者
        singers = []
        for singer_data in data.get("singers", []):
            singer = Singer(
                id=singer_data.get("id") or str(uuid4()),
                name=singer_data.get("name", "未命名"),
                color=singer_data.get("color", "#FF6B6B"),
                complement_color=singer_data.get("complement_color", ""),
                color_mode=singer_data.get("color_mode", "solid"),
                split_colors=singer_data.get("split_colors", []),
                is_default=singer_data.get("is_default", False),
                is_placeholder=singer_data.get("is_placeholder", False),
                display_priority=int(singer_data.get("display_priority", 0)),
                enabled=singer_data.get("enabled", True),
                backend_number=int(singer_data.get("backend_number", 0)),
                group=singer_data.get("group", ""),
                ttml_agent_id=str(singer_data.get("ttml_agent_id", "") or ""),
                ttml_agent_type=str(singer_data.get("ttml_agent_type", "") or ""),
                ttml_agent_named=bool(
                    singer_data.get("ttml_agent_named", True)
                ),
                ttml_agent_display_name=str(
                    singer_data.get("ttml_agent_display_name", "") or ""
                ),
                ttml_agent_source_name=str(
                    singer_data.get("ttml_agent_source_name", "") or ""
                ),
                ttml_role=str(singer_data.get("ttml_role", "") or ""),
            )
            singers.append(singer)

        # 解析句子。停顿符在整次解析内恒定，循环外一次取齐——
        # get_ruby_pause_char() 每次调用都会触发 AppSettings 配置目录的
        # 可写性探测（临时文件试写等文件系统 IO），逐句调用在大项目
        # （数千行）上会放大为秒级延迟并加剧与 UI 线程的 GIL 争抢。
        pause_char = get_ruby_pause_char()
        sentences = []
        for sentence_data in data.get("sentences", []):
            sentence = SugProjectParser._dict_to_sentence(sentence_data, pause_char)
            sentences.append(sentence)

        # 创建项目
        # 检查global_offset_ms字段是否存在（兼容旧版.sug）
        global_offset_raw = data.get("global_offset_ms")
        global_offset_ms = int(global_offset_raw) if global_offset_raw is not None else None

        # 轴分组（可选字段；旧版 .sug 缺省为空 = 未分轴）
        axis_groups = [
            AxisGroup.from_dict(g) for g in data.get("axis_groups", []) or []
        ]

        project = Project(
            id=data.get("id") or str(uuid4()),
            singers=singers,
            sentences=sentences,
            metadata=metadata,
            audio_duration_ms=int(data.get("audio_duration_ms", 0)),
            global_offset_ms=global_offset_ms,
            axis_groups=axis_groups,
        )

        return project

    @staticmethod
    def _dict_to_sentence(
        data: Dict[str, Any],
        pause_char: Optional[str] = None,
    ) -> Sentence:
        """将字典转换为 Sentence 对象。

        Args:
            data: sentence 层 dict。
            pause_char: 当前会话的注音停顿符。批量解析时由
                ``_dict_to_project`` 循环外一次取好传入——get_ruby_pause_char()
                每次调用都触发设置配置目录的可写性探测（文件系统 IO），
                逐句调用在大项目上会放大为秒级延迟；None 时本函数自行
                读取（单句直调兼容路径）。
        """
        singer_id = data.get("singer_id", "")

        characters = []
        if pause_char is None:
            pause_char = get_ruby_pause_char()
        legacy_pause_texts = pause_char_variants(pause_char) | {RUBY_PAUSE_SENTINEL, ""}
        for char_data in data.get("characters", []):
            # 解析 Ruby。任何 part 都不丢弃（空 part 曾被静默过滤，导致
            # check_count 与 parts 数失配）：哨兵 token、历史遗留的空串、
            # 以及旧版直接写入的停顿符字面值，统一映射为当前配置的停顿符。
            ruby = None
            ruby_data = char_data.get("ruby")
            if ruby_data:
                parts_data = ruby_data.get("parts")
                if parts_data:
                    parts = []
                    for p in parts_data:
                        raw_text = str(p.get("text", "") or "")
                        if raw_text in legacy_pause_texts:
                            raw_text = pause_char
                        parts.append(
                            RubyPart(
                                text=raw_text,
                                offset_ms=int(p.get("offset_ms", 0)),
                            )
                        )
                    if parts:
                        ruby = Ruby(parts=parts)

            raw_check_count = int(char_data.get("check_count", 1))
            timestamps = [int(ts) for ts in char_data.get("timestamps", [])]
            is_sentence_end = bool(char_data.get("is_sentence_end", False))
            has_sentence_end_ts_field = "sentence_end_ts" in char_data
            sentence_end_ts = char_data.get("sentence_end_ts")

            if has_sentence_end_ts_field:
                if sentence_end_ts is not None:
                    sentence_end_ts = int(sentence_end_ts)
                check_count = raw_check_count
            else:
                check_count = raw_check_count
                if is_sentence_end:
                    old_check_count = raw_check_count
                    check_count = max(0, old_check_count - 1)
                    if len(timestamps) == old_check_count and timestamps:
                        sentence_end_ts = timestamps.pop()
                    else:
                        timestamps = timestamps[:check_count]

            char = Character(
                char=char_data.get("char", "?"),
                ruby=ruby,
                check_count=check_count,
                timestamps=timestamps,
                sentence_end_ts=sentence_end_ts,
                linked_to_next=bool(char_data.get("linked_to_next", False)),
                is_line_end=bool(char_data.get("is_line_end", False)),
                is_sentence_end=is_sentence_end,
                is_rest=bool(char_data.get("is_rest", False)),
                singer_id=char_data.get("singer_id", "") or singer_id,
                needs_guide=bool(char_data.get("needs_guide", False)),
                is_guide=bool(char_data.get("is_guide", False)),
                force_singer_tag=bool(char_data.get("force_singer_tag", False)),
            )
            # 不变式自愈：旧版存档丢过空 part、或旧迁移路径减 cc 未并段的
            # 存量损伤，在此通过 set_check_count 的收口逻辑修复
            # （不足补占位符、多余并尾段；一致时为 no-op）。
            if (
                char.ruby is not None
                and char.check_count >= 1
                and len(char.ruby.parts) != char.check_count
            ):
                char.set_check_count(char.check_count, force=True)
            # 推送时间戳和演唱者到 Ruby
            char.push_to_ruby()
            characters.append(char)

        # 确保 singer_id 有效（回退到第一个字符的 singer_id）
        effective_singer_id = singer_id
        if not effective_singer_id and characters:
            effective_singer_id = characters[0].singer_id

        sentence = Sentence(
            id=data.get("id") or str(uuid4()),
            singer_id=effective_singer_id,
            characters=characters,
            aux_role=str(data.get("aux_role", "") or ""),
            aux_parent_id=str(data.get("aux_parent_id", "") or ""),
            aux_lang=str(data.get("aux_lang", "") or ""),
            aux_agent=str(data.get("aux_agent", "") or ""),
            aux_order=int(data.get("aux_order", 0) or 0),
            aux_begin=(
                int(data["aux_begin"])
                if data.get("aux_begin") is not None
                else None
            ),
            aux_end=(
                int(data["aux_end"]) if data.get("aux_end") is not None else None
            ),
            song_part=str(data.get("song_part", "") or ""),
        )

        return sentence
