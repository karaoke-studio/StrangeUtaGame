"""SUG 项目文件解析器测试。"""

import pytest
from pathlib import Path
from datetime import datetime

from strange_uta_game.backend.infrastructure.persistence.sug_io import (
    SugProjectParser,
    SugParseError,
)
from strange_uta_game.backend.domain import (
    Project,
    Singer,
    Sentence,
    Character,
    Ruby,
    RubyPart,
)


class TestSugProjectParser:
    """测试 SUG 项目文件解析器"""

    def test_save_and_load_simple_project(self, tmp_path):
        """测试保存和加载简单项目"""
        # 创建项目
        project = Project()
        singer = project.get_default_singer()

        sentence = Sentence.from_text("测试歌词", singer.id)
        project.add_sentence(sentence)

        # 保存
        file_path = tmp_path / "test.sug"
        SugProjectParser.save(project, str(file_path))

        # 验证文件存在
        assert file_path.exists()

        # 加载
        loaded = SugProjectParser.load(str(file_path))

        # 验证
        assert loaded.id == project.id
        assert len(loaded.sentences) == 1
        assert loaded.sentences[0].text == "测试歌词"

    def test_save_and_load_with_timetags(self, tmp_path):
        """测试保存和加载带时间标签的项目"""
        project = Project()
        singer = project.get_default_singer()

        sentence = Sentence.from_text("赤い花", singer.id)
        sentence.characters[0].add_timestamp(1000)
        sentence.characters[1].add_timestamp(1500)
        sentence.characters[2].add_timestamp(2000)
        sentence.characters[2].set_sentence_end_ts(2500)

        project.add_sentence(sentence)

        # 保存并加载
        file_path = tmp_path / "test.sug"
        SugProjectParser.save(project, str(file_path))
        loaded = SugProjectParser.load(str(file_path))

        # 验证
        assert sum(len(c.all_timestamps) for c in loaded.sentences[0].characters) == 4
        assert loaded.sentences[0].characters[0].timestamps[0] == 1000
        assert loaded.sentences[0].characters[2].sentence_end_ts == 2500

    def test_load_legacy_v2_sentence_end_without_sentence_end_ts(self, tmp_path):
        file_path = tmp_path / "legacy_v2.sug"
        file_path.write_text(
            """
{
  "version": "2.0",
  "id": "p1",
  "metadata": {},
  "audio_duration_ms": 0,
  "singers": [
    {
      "id": "s1",
      "name": "默认",
      "color": "#FF6B6B",
      "is_default": true,
      "display_priority": 0,
      "enabled": true,
      "backend_number": 1
    }
  ],
  "sentences": [
    {
      "id": "line1",
      "singer_id": "s1",
      "characters": [
        {
          "char": "花",
          "check_count": 2,
          "timestamps": [1000, 1500],
          "linked_to_next": false,
          "is_line_end": true,
          "is_sentence_end": true,
          "is_rest": false,
          "singer_id": "s1"
        }
      ]
    }
  ]
}
            """.strip(),
            encoding="utf-8",
        )

        loaded = SugProjectParser.load(str(file_path))

        char = loaded.sentences[0].characters[0]
        assert char.check_count == 1
        assert char.timestamps == [1000]
        assert char.sentence_end_ts == 1500

    def test_load_legacy_v2_sentence_end_without_release_timestamp(self, tmp_path):
        file_path = tmp_path / "legacy_v2_partial.sug"
        file_path.write_text(
            """
{
  "version": "2.0",
  "id": "p1",
  "metadata": {},
  "audio_duration_ms": 0,
  "singers": [
    {
      "id": "s1",
      "name": "默认",
      "color": "#FF6B6B",
      "is_default": true,
      "display_priority": 0,
      "enabled": true,
      "backend_number": 1
    }
  ],
  "sentences": [
    {
      "id": "line1",
      "singer_id": "s1",
      "characters": [
        {
          "char": "花",
          "check_count": 2,
          "timestamps": [1000],
          "linked_to_next": false,
          "is_line_end": true,
          "is_sentence_end": true,
          "is_rest": false,
          "singer_id": "s1"
        }
      ]
    }
  ]
}
            """.strip(),
            encoding="utf-8",
        )

        loaded = SugProjectParser.load(str(file_path))

        char = loaded.sentences[0].characters[0]
        assert char.check_count == 1
        assert char.timestamps == [1000]
        assert char.sentence_end_ts is None

    def test_save_and_load_with_rubies(self, tmp_path):
        """测试保存和加载带注音的项目"""
        project = Project()
        singer = project.get_default_singer()

        sentence = Sentence.from_text("赤い花", singer.id)
        sentence.characters[0].set_ruby(Ruby(parts=[RubyPart(text="あか")]))
        sentence.characters[2].set_ruby(Ruby(parts=[RubyPart(text="はな")]))

        project.add_sentence(sentence)

        # 保存并加载
        file_path = tmp_path / "test.sug"
        SugProjectParser.save(project, str(file_path))
        loaded = SugProjectParser.load(str(file_path))

        # 验证
        assert len(loaded.sentences[0].rubies) == 2
        assert loaded.sentences[0].rubies[0].text == "あか"

    def test_save_and_load_multiple_singers(self, tmp_path):
        """测试保存和加载多演唱者项目"""
        project = Project()

        # 添加演唱者
        singer2 = Singer(name="和声", color="#4ECDC4")
        project.add_singer(singer2)

        # 添加歌词
        sentence1 = Sentence.from_text("主唱", project.get_default_singer().id)
        sentence2 = Sentence.from_text("和声", singer2.id)

        project.add_sentence(sentence1)
        project.add_sentence(sentence2)

        # 保存并加载
        file_path = tmp_path / "test.sug"
        SugProjectParser.save(project, str(file_path))
        loaded = SugProjectParser.load(str(file_path))

        # 验证
        assert len(loaded.singers) == 2
        assert len(loaded.sentences) == 2

    def test_load_nonexistent_file_raises_error(self, tmp_path):
        """测试加载不存在的文件应该报错"""
        with pytest.raises(SugParseError) as exc_info:
            SugProjectParser.load(str(tmp_path / "nonexistent.sug"))

        assert "文件不存在" in str(exc_info.value)

    def test_load_invalid_json_raises_error(self, tmp_path):
        """测试加载无效的 JSON 文件应该报错"""
        file_path = tmp_path / "invalid.sug"
        file_path.write_text("not valid json", encoding="utf-8")

        with pytest.raises(SugParseError) as exc_info:
            SugProjectParser.load(str(file_path))

        assert "JSON" in str(exc_info.value)


class TestLoadWithExtras:
    """load_with_extras：单次解析同时返回 Project 与附加字段"""

    def test_roundtrip_returns_project_and_extras(self, tmp_path):
        """保存时带 nicokara_tags/media_path，加载一并带回"""
        project = Project()
        singer = project.get_default_singer()
        project.add_sentence(Sentence.from_text("测试歌词", singer.id))

        file_path = tmp_path / "extras.sug"
        SugProjectParser.save(
            project,
            str(file_path),
            nicokara_tags={"tag1": {"enabled": True}},
            media_path="X:/media/song.mp3",
        )

        loaded, extras = SugProjectParser.load_with_extras(str(file_path))

        assert loaded.id == project.id
        assert loaded.sentences[0].text == "测试歌词"
        assert extras == {
            "nicokara_tags": {"tag1": {"enabled": True}},
            "media_path": "X:/media/song.mp3",
        }

    def test_no_extras_returns_empty_dict(self, tmp_path):
        """旧版 sug 无附加字段时 extras 为空 dict（不抛错）"""
        project = Project()
        file_path = tmp_path / "plain.sug"
        SugProjectParser.save(project, str(file_path))

        _, extras = SugProjectParser.load_with_extras(str(file_path))
        assert extras == {}

    def test_extras_extracted_before_migration(self, tmp_path):
        """v1.0 迁移会重建字典丢弃附加字段——extras 必须取自迁移前原始数据"""
        file_path = tmp_path / "v1_with_media.sug"
        file_path.write_text(
            """
{
  "version": "1.0",
  "id": "p1",
  "metadata": {},
  "audio_duration_ms": 0,
  "singers": [
    {
      "id": "s1",
      "name": "默认",
      "color": "#FF6B6B",
      "is_default": true,
      "display_priority": 0,
      "enabled": true,
      "backend_number": 1
    }
  ],
  "lines": [
    {
      "id": "l1",
      "singer_id": "s1",
      "text": "花",
      "chars": ["花"],
      "checkpoints": [{"char_idx": 0, "check_count": 1}],
      "timetags": [{"char_idx": 0, "checkpoint_idx": 0, "timestamp_ms": 1000}],
      "rubies": []
    }
  ],
  "media_path": "X:/media/song.mp3"
}
            """.strip(),
            encoding="utf-8",
        )

        project, extras = SugProjectParser.load_with_extras(str(file_path))

        assert len(project.sentences) == 1
        assert project.sentences[0].text == "花"
        assert extras == {"media_path": "X:/media/song.mp3"}

    def test_nonexistent_file_raises_error(self, tmp_path):
        with pytest.raises(SugParseError):
            SugProjectParser.load_with_extras(str(tmp_path / "nonexistent.sug"))


class TestRubyPlaceholderRoundtrip:
    """ruby 占位符（停顿符）的存档哨兵映射与不变式自愈"""

    def _make_project(self, ch: Character) -> Project:
        project = Project()
        singer = project.get_default_singer()
        sentence = Sentence(singer_id=singer.id, characters=[ch])
        project.add_sentence(sentence)
        return project

    def test_placeholder_parts_saved_as_sentinel(self):
        """占位 part（停顿符）序列化为 ^pause^ 哨兵 token"""
        ch = Character(
            char="寿",
            ruby=Ruby(parts=[RubyPart("す"), RubyPart("^"), RubyPart("^")]),
            check_count=3,
            timestamps=[1000, 1100, 1200],
        )
        data = SugProjectParser._sentence_to_dict(
            Sentence(singer_id="s1", characters=[ch])
        )
        texts = [p["text"] for p in data["characters"][0]["ruby"]["parts"]]
        assert texts == ["す", "^pause^", "^pause^"]

    def test_placeholder_roundtrip_lossless(self, tmp_path):
        """占位 part 保存→加载无损（不再被静默丢弃）"""
        ch = Character(
            char="寿",
            ruby=Ruby(parts=[RubyPart("す"), RubyPart("^"), RubyPart("^")]),
            check_count=3,
            timestamps=[1000, 1100, 1200],
        )
        project = self._make_project(ch)
        file_path = tmp_path / "placeholder.sug"
        SugProjectParser.save(project, str(file_path))
        loaded = SugProjectParser.load(str(file_path))

        loaded_ch = loaded.sentences[0].characters[0]
        assert loaded_ch.check_count == 3
        assert [p.text for p in loaded_ch.ruby.parts] == ["す", "^", "^"]

    def test_legacy_empty_parts_healed_to_placeholder(self, tmp_path):
        """旧版存档中的空文本 part 加载时映射为占位符，数量不丢"""
        sentence_dict = {
            "id": "sent1",
            "singer_id": "s1",
            "characters": [
                {
                    "char": "寿",
                    "check_count": 3,
                    "timestamps": [1000, 1100, 1200],
                    "sentence_end_ts": None,
                    "ruby": {
                        "parts": [
                            {"text": "す", "offset_ms": 0},
                            {"text": "", "offset_ms": 100},
                            {"text": "", "offset_ms": 200},
                        ]
                    },
                }
            ],
        }
        sentence = SugProjectParser._dict_to_sentence(sentence_dict)
        ch = sentence.characters[0]
        assert ch.check_count == 3
        assert [p.text for p in ch.ruby.parts] == ["す", "^", "^"]

    def test_legacy_dropped_parts_healed_by_padding(self):
        """旧版已丢空 part 的存量损伤（parts < cc）：加载时补占位符自愈"""
        sentence_dict = {
            "id": "sent1",
            "singer_id": "s1",
            "characters": [
                {
                    "char": "寿",
                    "check_count": 3,
                    "timestamps": [1000, 1100, 1200],
                    "sentence_end_ts": None,
                    "ruby": {"parts": [{"text": "す", "offset_ms": 0}]},
                }
            ],
        }
        sentence = SugProjectParser._dict_to_sentence(sentence_dict)
        ch = sentence.characters[0]
        assert ch.check_count == 3
        assert len(ch.ruby.parts) == 3
        assert [p.text for p in ch.ruby.parts] == ["す", "^", "^"]

    def test_excess_parts_healed_by_merging_tail(self):
        """parts > cc 的存量损伤：加载时合并尾段自愈"""
        sentence_dict = {
            "id": "sent1",
            "singer_id": "s1",
            "characters": [
                {
                    "char": "空",
                    "check_count": 2,
                    "timestamps": [1000, 1100],
                    "sentence_end_ts": None,
                    "ruby": {
                        "parts": [
                            {"text": "そ", "offset_ms": 0},
                            {"text": "ら", "offset_ms": 100},
                            {"text": "あ", "offset_ms": 200},
                        ]
                    },
                }
            ],
        }
        sentence = SugProjectParser._dict_to_sentence(sentence_dict)
        ch = sentence.characters[0]
        assert ch.check_count == 2
        assert [p.text for p in ch.ruby.parts] == ["そ", "らあ"]

    def test_no_mora_cc0_parts_kept_verbatim(self, tmp_path):
        """cc=0 无 mora 字符的多 part ruby 不受自愈影响，原样往返"""
        ch = Character(
            char="夢",
            ruby=Ruby(parts=[RubyPart("ゆ"), RubyPart("め")]),
            check_count=0,
        )
        project = self._make_project(ch)
        file_path = tmp_path / "nomora.sug"
        SugProjectParser.save(project, str(file_path))
        loaded = SugProjectParser.load(str(file_path))

        loaded_ch = loaded.sentences[0].characters[0]
        assert loaded_ch.check_count == 0
        assert [p.text for p in loaded_ch.ruby.parts] == ["ゆ", "め"]


def test_project_metadata_extra_roundtrip(tmp_path):
    """AMLL 平台 ID 等 extra 元数据随 .sug 保存/加载，旧文件缺省为空。"""
    project = Project()
    project.metadata.extra = {
        "ncmMusicId": "101126",
        "qqMusicId": ["a", "b"],
        "isrc": "HKG731554003",
    }
    project.add_sentence(Sentence.from_text("歌", project.singers[0].id))

    file_path = tmp_path / "extra.sug"
    SugProjectParser.save(project, str(file_path))
    loaded = SugProjectParser.load(str(file_path))

    assert loaded.metadata.extra == project.metadata.extra

    legacy = tmp_path / "legacy.sug"
    legacy.write_text(
        '{"version":"0.3.0","id":"p","metadata":{},"singers":[],"sentences":[]}',
        encoding="utf-8",
    )
    assert SugProjectParser.load(str(legacy)).metadata.extra == {}
