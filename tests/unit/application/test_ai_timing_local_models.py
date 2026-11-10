"""自定义 / 本地对齐模型：目录校验、模型目录扫描与编排服务接线。

全部离线、无 Qt、无真实权重：用最小目录树模拟用户「直接把模型文件夹
放进模型目录」的形态。
"""

import json
from pathlib import Path

import pytest

from strange_uta_game.backend.application.ai_timing.alignment import (
    AlignmentResult,
    EmissionSpan,
)
from strange_uta_game.backend.application.ai_timing.local_models import (
    LOCAL_MODEL_SUPPORT_HINT,
    has_local_model_intent,
    is_url_reference,
    portable_model_reference,
    resolve_local_model_path,
    scan_local_models,
    validate_local_model_dir,
)
from strange_uta_game.backend.application.ai_timing.models import (
    MANIFEST_NAME,
    ModelManifest,
    ModelRegistry,
    filter_model_files,
)
from strange_uta_game.backend.application.ai_timing.resolver import (
    PronunciationResolver,
)
from strange_uta_game.backend.application.ai_timing.runtime import AiRuntimeManager
from strange_uta_game.backend.application.ai_timing.service import (
    AiTimingError,
    AiTimingService,
)
from strange_uta_game.backend.application.ai_timing.settings import AiTimingSettings
from strange_uta_game.backend.application.ai_timing.vocals import (
    AiCache,
    VocalPreparationService,
)
from strange_uta_game.backend.domain import (
    Character,
    Project,
    Sentence,
)
from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
    DummyAnalyzer,
)

# 1MiB + 1：刚好越过「看起来是 Git LFS 指针」的下限
_WEIGHT_BYTES = b"\0" * (1024 * 1024 + 1)
_LATN_VOCAB = {chr(c): i for i, c in enumerate(range(ord("a"), ord("z") + 1))}


def _make_model_dir(
    root: Path,
    name: str = "my-model",
    *,
    architectures=("Wav2Vec2ForCTC",),
    model_type: str = "wav2vec2",
    vocab=None,
    weight: bytes = _WEIGHT_BYTES,
    tokenizer_name: str = "vocab.json",
    preprocessor: bool = True,
) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    config = {"model_type": model_type}
    if architectures is not None:
        config["architectures"] = list(architectures)
    (directory / "config.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    if weight is not None:
        (directory / "model.safetensors").write_bytes(weight)
    if tokenizer_name == "vocab.json":
        (directory / "vocab.json").write_text(
            json.dumps(_LATN_VOCAB if vocab is None else vocab),
            encoding="utf-8",
        )
    elif tokenizer_name:
        (directory / tokenizer_name).write_text("dummy", encoding="utf-8")
    if preprocessor:
        (directory / "preprocessor_config.json").write_text(
            json.dumps({"sampling_rate": 16000}), encoding="utf-8"
        )
    return directory


class TestValidateLocalModelDir:
    def test_ok(self, tmp_path):
        directory = _make_model_dir(tmp_path)
        status = validate_local_model_dir(directory)
        assert status.is_ready, status.message
        assert status.model_dir == directory

    def test_missing_directory(self, tmp_path):
        status = validate_local_model_dir(tmp_path / "nope")
        assert status.state == "missing"
        assert "不存在" in status.message

    def test_not_a_model_dir(self, tmp_path):
        (tmp_path / "empty").mkdir()
        status = validate_local_model_dir(tmp_path / "empty")
        assert status.state == "corrupt"
        assert "config.json" in status.message

    def test_rejects_non_wav2vec2_architecture(self, tmp_path):
        directory = _make_model_dir(
            tmp_path, architectures=("WhisperForConditionalGeneration",)
        )
        status = validate_local_model_dir(directory)
        assert status.state == "corrupt"
        assert "Wav2Vec2" in status.message
        assert LOCAL_MODEL_SUPPORT_HINT.split("（")[0] in status.message

    def test_rejects_non_wav2vec2_model_type(self, tmp_path):
        directory = _make_model_dir(
            tmp_path, architectures=None, model_type="whisper"
        )
        status = validate_local_model_dir(directory)
        assert status.state == "corrupt"
        assert "whisper" in status.message

    def test_git_lfs_pointer_detected(self, tmp_path):
        directory = _make_model_dir(
            tmp_path,
            weight=b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\n",
        )
        status = validate_local_model_dir(directory)
        assert status.state == "corrupt"
        assert "Git LFS" in status.message

    def test_missing_weights(self, tmp_path):
        directory = _make_model_dir(tmp_path, weight=None)
        status = validate_local_model_dir(directory)
        assert status.state == "corrupt"
        assert "权重" in status.message

    def test_missing_tokenizer(self, tmp_path):
        directory = _make_model_dir(tmp_path, tokenizer_name="")
        status = validate_local_model_dir(directory)
        assert status.state == "corrupt"
        assert "分词器" in status.message

    def test_non_latn_vocab_rejected(self, tmp_path):
        """假名词表能用 HF 下载但跑不出正确时间轴——必须提前拒绝。"""
        directory = _make_model_dir(
            tmp_path, vocab={"あ": 0, "い": 1, "う": 2, "え": 3, "お": 4}
        )
        status = validate_local_model_dir(directory)
        assert status.state == "corrupt"
        assert "罗马字" in status.message

    def test_missing_preprocessor_is_tolerated(self, tmp_path):
        """没有 preprocessor_config.json 仍可用（provider 侧有默认特征参数）。"""
        directory = _make_model_dir(tmp_path, preprocessor=False)
        assert validate_local_model_dir(directory).is_ready

    def test_architectures_as_string_is_accepted(self, tmp_path):
        """config.json 里 architectures 写成字符串（手写/转换脚本常见）不能误杀。"""
        directory = _make_model_dir(tmp_path)
        config_path = directory / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["architectures"] = "Wav2Vec2ForCTC"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        assert validate_local_model_dir(directory).is_ready

    def test_architecture_match_is_case_insensitive(self, tmp_path):
        directory = _make_model_dir(tmp_path)
        config_path = directory / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["architectures"] = ["wav2vec2forctc"]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        assert validate_local_model_dir(directory).is_ready

    def test_unknown_architecture_is_rejected(self, tmp_path):
        directory = _make_model_dir(tmp_path, architectures=None, model_type="")
        status = validate_local_model_dir(directory)
        assert status.state == "corrupt"
        assert "无法确认" in status.message

    def test_sentencepiece_only_tokenizer_rejected(self, tmp_path):
        """SentencePiece 二进制词表无法校验是否罗马字：明确拒绝而不是放行。"""
        directory = _make_model_dir(tmp_path, tokenizer_name="tokenizer.model")
        status = validate_local_model_dir(directory)
        assert status.state == "corrupt"
        assert "SentencePiece" in status.message

    def test_unparseable_tokenizer_is_rejected(self, tmp_path):
        directory = _make_model_dir(tmp_path, tokenizer_name="tokenizer.json")
        status = validate_local_model_dir(directory)
        assert status.state == "corrupt"
        assert "无法解析" in status.message

    def test_tokenizer_json_vocab_dict_is_checked(self, tmp_path):
        directory = _make_model_dir(tmp_path, tokenizer_name="")
        (directory / "tokenizer.json").write_text(
            json.dumps({"model": {"vocab": _LATN_VOCAB}}), encoding="utf-8"
        )
        assert validate_local_model_dir(directory).is_ready


class TestResolveLocalModelPath:
    def test_absolute_path(self, tmp_path):
        directory = _make_model_dir(tmp_path / "models")
        assert resolve_local_model_path(str(directory), tmp_path / "models") == (
            directory.resolve()
        )

    def test_folder_name_inside_model_root(self, tmp_path):
        root = tmp_path / "ai_models"
        directory = _make_model_dir(root, name="my-model")
        assert resolve_local_model_path("my-model", root) == directory.resolve()

    def test_quoted_and_padded_input(self, tmp_path):
        root = tmp_path / "ai_models"
        directory = _make_model_dir(root, name="my-model")
        assert resolve_local_model_path(f'  "{directory}"  ', root) == (
            directory.resolve()
        )

    def test_repo_id_is_not_local(self, tmp_path):
        root = tmp_path / "ai_models"
        root.mkdir()
        assert resolve_local_model_path("NextFire/demo", root) is None

    def test_empty_is_not_local(self, tmp_path):
        assert resolve_local_model_path("   ", tmp_path) is None

    def test_intent_detection(self, tmp_path):
        assert has_local_model_intent(str(tmp_path)) is True
        assert has_local_model_intent("NextFire/demo") is False
        assert has_local_model_intent("") is False

    def test_intent_accepts_folder_name_inside_model_root(self, tmp_path):
        """只填模型根下的文件夹名也要被认成本地模型（否则 UI 显示下载按钮）。"""
        root = tmp_path / "ai_models"
        _make_model_dir(root, name="my-model")
        assert has_local_model_intent("my-model", root) is True
        # 不传模型根、当前目录也没有该文件夹 → 无法判定为本地路径
        assert has_local_model_intent("my-model") is False

    def test_url_is_not_a_local_model(self, tmp_path):
        url = "https://huggingface.co/NextFire/mms-300m-ForcedAligner-karaoke-ja-Latn"
        assert is_url_reference(url)
        assert is_url_reference("  作者/仓库 ") is False
        assert has_local_model_intent(url, tmp_path) is False
        assert resolve_local_model_path(url, tmp_path) is None

    def test_portable_reference_inside_and_outside_root(self, tmp_path):
        root = tmp_path / "ai_models"
        directory = _make_model_dir(root, name="my-model")
        assert portable_model_reference(str(directory), root) == "my-model"
        outside = tmp_path / "elsewhere"
        assert portable_model_reference(str(outside), root) == str(outside)
        # 模型根本身保留绝对路径（避免设置里出现无意义的 "."）
        assert portable_model_reference(str(root), root) == str(root)


class TestScanLocalModels:
    def test_finds_direct_and_nested_models(self, tmp_path):
        root = tmp_path / "ai_models"
        direct = _make_model_dir(root, name="a-model")
        nested = _make_model_dir(root / "downloaded", name="b-model")
        paths = {item.path for item in scan_local_models(root)}
        assert direct in paths
        assert nested in paths

    def test_skips_managed_downloads(self, tmp_path):
        root = tmp_path / "ai_models"
        managed = _make_model_dir(root, name="NextFire__demo")
        (managed / MANIFEST_NAME).write_text("{}", encoding="utf-8")
        assert scan_local_models(root) == []

    def test_detects_files_dumped_into_root(self, tmp_path):
        root = tmp_path / "ai_models"
        _make_model_dir(root, name="ignored")
        # 直接把模型文件摊在模型根里：根目录本身也算候选
        (root / "config.json").write_text('{"model_type": "wav2vec2"}', encoding="utf-8")
        labels = {item.label for item in scan_local_models(root)}
        assert "（模型目录根）" in labels

    def test_missing_root_is_empty(self, tmp_path):
        assert scan_local_models(tmp_path / "nope") == []


class TestFilterModelFilesForCustomRepos:
    def test_bin_allowed_when_no_safetensors(self):
        names = [
            item[0]
            for item in filter_model_files(
                [("config.json", 10), ("pytorch_model.bin", 1000)]
            )
        ]
        assert "pytorch_model.bin" in names

    def test_bin_dropped_when_safetensors_present(self):
        names = [
            item[0]
            for item in filter_model_files(
                [
                    ("model.safetensors", 1000),
                    ("pytorch_model.bin", 1000),
                ]
            )
        ]
        assert names == ["model.safetensors"]

    def test_sentencepiece_model_allowed(self):
        names = [item[0] for item in filter_model_files([("tokenizer.model", 10)])]
        assert names == ["tokenizer.model"]

    def test_training_artifacts_bin_excluded(self):
        """非权重 .bin（训练产物）不进下载清单。"""
        names = [
            item[0]
            for item in filter_model_files(
                [
                    ("config.json", 10),
                    ("training_args.bin", 10),
                    ("rng_state.pth", 10),
                ]
            )
        ]
        assert names == ["config.json"]

    def test_bin_shards_kept_when_bin_index_present(self):
        """分片 bin 仓库带 index.json 时，不能因为存在 safetensors 就丢 bin。"""
        names = [
            item[0]
            for item in filter_model_files(
                [
                    ("model.safetensors", 1000),
                    ("pytorch_model.bin.index.json", 100),
                    ("pytorch_model-00001-of-00002.bin", 1000),
                ]
            )
        ]
        assert "pytorch_model-00001-of-00002.bin" in names
        assert "pytorch_model.bin.index.json" in names


class _FakeWorker:
    def __init__(self):
        self.calls = []

    def run(self, request, audio_path, model_spec, on_progress=None, timeout_s=None):
        self.calls.append((request, audio_path, model_spec))
        return AlignmentResult(
            annotation_digest=request.annotation_digest,
            model_id=str(model_spec.get("model_id", "")),
            spans=[
                EmissionSpan(
                    token_index=token.index,
                    start_ms=i * 100,
                    end_ms=(i + 1) * 100,
                )
                for i, token in enumerate(request.tokens)
            ],
        )


def _project() -> Project:
    project = Project()
    project.sentences = [
        Sentence(
            singer_id="s1",
            characters=[
                Character(char="あ", check_count=1, ruby=None, singer_id="s1"),
                Character(char="か", check_count=1, ruby=None, singer_id="s1"),
            ],
        )
    ]
    return project


def _make_service(tmp_path, settings, worker):
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"audio-bytes")
    (tmp_path / "song_人声.wav").write_bytes(b"vocal-bytes")
    cache = AiCache(tmp_path / "ai_cache")
    return AiTimingService(
        settings=settings,
        cache=cache,
        registry=ModelRegistry(tmp_path / "models"),
        runtime=AiRuntimeManager(),
        vocal_service=VocalPreparationService(cache),
        resolver=PronunciationResolver(analyzer=DummyAnalyzer(), chinese_mode=False),
        worker_factory=lambda python: worker,
        separation_identity=lambda: {"model": "inst_v1e", "stem": "人声", "params": {}},
    )


class TestServiceUsesLocalModel:
    def test_snapshot_reports_local_model_ready(self, tmp_path):
        root = tmp_path / "ai_models"
        directory = _make_model_dir(root)
        settings = AiTimingSettings(
            provider="wav2vec2",
            model_root=str(root),
            wav2vec2_model_id=str(directory),
        )
        service = _make_service(tmp_path, settings, _FakeWorker())
        snap = service.snapshot(_project(), str(tmp_path / "song.flac"), probe_runtime=False)
        assert service.uses_local_model
        assert snap.model is not None and snap.model.is_ready
        assert snap.ready, snap.blocking_reasons

    def test_execute_resolves_folder_name_to_absolute_directory(self, tmp_path):
        """相对文件夹名必须在执行时解析成绝对目录再交给 worker。"""
        root = tmp_path / "ai_models"
        directory = _make_model_dir(root, name="my-model")
        settings = AiTimingSettings(
            provider="wav2vec2",
            model_root=str(root),
            wav2vec2_model_id="my-model",
        )
        worker = _FakeWorker()
        service = _make_service(tmp_path, settings, worker)
        service.execute(_project(), str(tmp_path / "song.flac"))
        assert worker.calls, "worker 未被调用"
        assert worker.calls[0][2]["model_id"] == str(directory.resolve())

    def test_execute_reuses_cache_and_invalidates_on_weight_change(self, tmp_path):
        """本地模型：第二次执行命中缓存；权重被替换后必须重新推理。"""
        import os
        import time as _time

        root = tmp_path / "ai_models"
        directory = _make_model_dir(root)
        settings = AiTimingSettings(
            provider="wav2vec2",
            model_root=str(root),
            wav2vec2_model_id="my-model",
        )
        # _make_model_dir 默认目录名就是 my-model
        worker = _FakeWorker()
        service = _make_service(tmp_path, settings, worker)
        audio = str(tmp_path / "song.flac")
        service.execute(_project(), audio)
        service.execute(_project(), audio)
        assert len(worker.calls) == 1, "第二次执行应命中对齐缓存"
        # 同尺寸覆盖 + 明显不同的 mtime：指纹必须变化
        weight = directory / "model.safetensors"
        weight.write_bytes(_WEIGHT_BYTES)
        stamp = _time.time() + 3600
        os.utime(weight, (stamp, stamp))
        service.execute(_project(), audio)
        assert len(worker.calls) == 2, "替换权重后必须重新推理"

    def test_folder_name_in_model_root_resolves(self, tmp_path):
        root = tmp_path / "ai_models"
        directory = _make_model_dir(root, name="my-model")
        settings = AiTimingSettings(
            provider="wav2vec2",
            model_root=str(root),
            wav2vec2_model_id="my-model",
        )
        service = _make_service(tmp_path, settings, _FakeWorker())
        assert service.local_model_path == directory.resolve()
        # 只填文件夹名也必须被认成「本地模型」（否则弹窗会显示下载按钮）
        assert service.uses_local_model
        assert service.model_status().is_ready

    def test_broken_local_path_blocks_with_reason(self, tmp_path):
        broken = tmp_path / "missing-model"
        settings = AiTimingSettings(
            provider="wav2vec2",
            model_root=str(tmp_path / "ai_models"),
            wav2vec2_model_id=str(broken),
        )
        worker = _FakeWorker()
        service = _make_service(tmp_path, settings, worker)
        with pytest.raises(AiTimingError, match="本地模型目录不可用"):
            service.execute(_project(), str(tmp_path / "song.flac"))
        assert worker.calls == []

    def test_cache_key_covers_all_weight_files_and_config(self, tmp_path):
        """指纹覆盖全部权重与配置文件，而不只是最大权重。"""
        root = tmp_path / "ai_models"
        directory = _make_model_dir(root, name="my-model")
        settings = AiTimingSettings(
            provider="wav2vec2",
            model_root=str(root),
            wav2vec2_model_id="my-model",
        )
        service = _make_service(tmp_path, settings, _FakeWorker())
        key = service.model_cache_key
        assert key.startswith(f"{directory.resolve()}#"), key
        # 未修改 → 指纹稳定
        assert service.model_cache_key == key
        # 改动词表（不是权重）也必须让缓存失效
        (directory / "vocab.json").write_text(
            json.dumps(_LATN_VOCAB, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        assert service.model_cache_key != key


class TestRegistryStillWorksForRepoIDs:
    """仓库 ID 路径不受本地模型分支影响（回归）。"""

    def test_registry_download_still_validates(self, tmp_path):
        root = tmp_path / "models"
        _make_model_dir(root, name="a__b")  # slug of repo id "a/b"
        registry = ModelRegistry(root)
        registry.register(
            ModelManifest(model_id="a/b", provider="wav2vec2", revision="main")
        )
        settings = AiTimingSettings(
            provider="wav2vec2",
            model_root=str(root),
            wav2vec2_model_id="a/b",
        )
        service = _make_service(tmp_path, settings, _FakeWorker())
        assert service.local_model_path is None
        assert not service.uses_local_model
        assert service.model_status().is_ready

    def test_incompatible_custom_repo_reported_before_run(self, tmp_path):
        """自定义仓库下完即校验架构：Whisper 之类在弹窗里就说清楚。"""
        root = tmp_path / "models"
        _make_model_dir(
            root,
            name="a__b",
            architectures=("WhisperForConditionalGeneration",),
        )
        registry = ModelRegistry(root)
        registry.register(
            ModelManifest(model_id="a/b", provider="wav2vec2", revision="main")
        )
        settings = AiTimingSettings(
            provider="wav2vec2",
            model_root=str(root),
            wav2vec2_model_id="a/b",
        )
        worker = _FakeWorker()
        service = _make_service(tmp_path, settings, worker)
        status = service.model_status()
        assert status.state == "corrupt"
        assert "Wav2Vec2" in status.message
        with pytest.raises(AiTimingError, match="自定义模型不兼容"):
            service.execute(_project(), str(tmp_path / "song.flac"))
        assert worker.calls == []


class TestUrlInput:
    def test_url_gets_actionable_message(self, tmp_path):
        settings = AiTimingSettings(
            provider="wav2vec2",
            wav2vec2_model_id=(
                "https://huggingface.co/NextFire/"
                "mms-300m-ForcedAligner-karaoke-ja-Latn"
            ),
        )
        service = _make_service(tmp_path, settings, _FakeWorker())
        status = service.model_status()
        assert status.state == "missing"
        assert "网页地址" in status.message
        assert "仓库 ID" in status.message
