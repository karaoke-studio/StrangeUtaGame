"""Wav2Vec2LatnProvider 的模型守卫（架构 / CTC 头 / blank）回归测试。

不加载真实 torch/transformers：用假模块替换 ``sys.modules["transformers"]``
与 ``_import_torch``，只验证守卫生效时抛中文错误、合法模型能拿到正确 blank。
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from strange_uta_game.backend.application.ai_timing.worker.providers import (
    AlignmentProviderError,
    Wav2Vec2LatnProvider,
)


class _FakeTorch:
    class cuda:  # noqa: N801 - 模拟 torch.cuda 命名空间
        @staticmethod
        def is_available():
            return False

    class backends:  # noqa: N801 - 模拟 torch.backends
        mps = SimpleNamespace(is_available=lambda: False)

    @staticmethod
    def device(name):
        return name


def _make_transformers(
    *,
    architectures=("Wav2Vec2ForCTC",),
    pad_token_id=29,
    vocab_size=32,
    tokenizer_pad=29,
    missing_keys=(),
    processor_error=None,
):
    config = SimpleNamespace(
        architectures=architectures,
        pad_token_id=pad_token_id,
        vocab_size=vocab_size,
    )

    class _Model:
        def __init__(self):
            self.config = config

        def to(self, device):
            return self

    class _Processor:
        def __init__(self, tokenizer=None, feature_extractor=None):
            self.tokenizer = tokenizer or SimpleNamespace(
                pad_token_id=tokenizer_pad
            )

        @classmethod
        def from_pretrained(cls, _model_id):
            if processor_error is not None:
                raise processor_error
            return cls()

    class _ForCTC:
        @staticmethod
        def from_pretrained(_model_id, output_loading_info=False):
            model = _Model()
            if output_loading_info:
                return model, {"missing_keys": list(missing_keys)}
            return model

    class _Tokenizer:
        @staticmethod
        def from_pretrained(_model_id):
            return SimpleNamespace(pad_token_id=tokenizer_pad)

    def _feature_extractor(**kwargs):
        return SimpleNamespace(**kwargs)

    return SimpleNamespace(
        Wav2Vec2Processor=_Processor,
        Wav2Vec2ForCTC=_ForCTC,
        Wav2Vec2CTCTokenizer=_Tokenizer,
        Wav2Vec2FeatureExtractor=_feature_extractor,
    )


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    """带 processor 配置的最小模型目录（走 from_pretrained 路径）。"""
    directory = tmp_path / "model"
    directory.mkdir()
    (directory / "config.json").write_text(
        json.dumps({"model_type": "wav2vec2"}), encoding="utf-8"
    )
    (directory / "preprocessor_config.json").write_text(
        json.dumps({"sampling_rate": 16000}), encoding="utf-8"
    )
    return directory


def _load(monkeypatch, transformers, directory: Path):
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    monkeypatch.setattr(
        Wav2Vec2LatnProvider, "_import_torch", lambda self: _FakeTorch()
    )
    provider = Wav2Vec2LatnProvider()
    provider.load(
        {"model_id": str(directory), "device": "cpu"},
        progress=lambda percent, message: None,
        cancel=lambda: False,
    )
    return provider


class TestArchitectureGuard:
    def test_rejects_non_ctc_architecture(self, monkeypatch, model_dir):
        transformers = _make_transformers(architectures=("Wav2Vec2Model",))
        with pytest.raises(AlignmentProviderError, match="不是 Wav2Vec2 CTC"):
            _load(monkeypatch, transformers, model_dir)

    def test_accepts_architecture_as_string(self, monkeypatch, model_dir):
        transformers = _make_transformers(architectures="Wav2Vec2ForCTC")
        provider = _load(monkeypatch, transformers, model_dir)
        assert provider._ctc_blank == 29


class TestCtcHeadGuard:
    def test_rejects_missing_lm_head_weights(self, monkeypatch, model_dir):
        """基座权重会让 from_pretrained 随机初始化 CTC 头 → 必须拒绝。"""
        transformers = _make_transformers(
            missing_keys=("lm_head.bias", "lm_head.weight")
        )
        with pytest.raises(AlignmentProviderError, match="CTC 分类头"):
            _load(monkeypatch, transformers, model_dir)

    def test_other_missing_keys_are_tolerated(self, monkeypatch, model_dir):
        transformers = _make_transformers(
            missing_keys=("wav2vec2.masked_spec_embed",)
        )
        assert _load(monkeypatch, transformers, model_dir)._ctc_blank == 29


class TestBlankResolution:
    def test_falls_back_to_tokenizer_pad(self, monkeypatch, model_dir):
        transformers = _make_transformers(pad_token_id=None, tokenizer_pad=7)
        provider = _load(monkeypatch, transformers, model_dir)
        assert provider._ctc_blank == 7

    def test_rejects_missing_blank(self, monkeypatch, model_dir):
        transformers = _make_transformers(pad_token_id=None, tokenizer_pad=None)
        with pytest.raises(AlignmentProviderError, match="未声明 CTC blank"):
            _load(monkeypatch, transformers, model_dir)

    def test_rejects_out_of_vocab_blank(self, monkeypatch, model_dir):
        transformers = _make_transformers(pad_token_id=None, tokenizer_pad=99)
        with pytest.raises(AlignmentProviderError, match="超出词表范围"):
            _load(monkeypatch, transformers, model_dir)


class TestProcessorFallback:
    def test_falls_back_when_no_processor_config(self, monkeypatch, tmp_path):
        """没有 processor/preprocessor 配置：用默认特征参数拼出 processor。"""
        directory = tmp_path / "bare-model"
        directory.mkdir()
        (directory / "config.json").write_text(
            json.dumps({"model_type": "wav2vec2"}), encoding="utf-8"
        )
        transformers = _make_transformers(
            processor_error=OSError("no processor config")
        )
        provider = _load(monkeypatch, transformers, directory)
        assert provider._ctc_blank == 29

    def test_processor_error_is_not_masked_when_config_present(
        self, monkeypatch, model_dir
    ):
        """配置存在却加载失败：保留原始错误，不静默换成默认参数。"""
        transformers = _make_transformers(
            processor_error=OSError("tokenizer broken")
        )
        with pytest.raises(AlignmentProviderError, match="tokenizer broken"):
            _load(monkeypatch, transformers, model_dir)
