import json

from strange_uta_game.frontend.settings.app_settings import AppSettings


def test_first_start_enables_preview_guide_from_packaged_config(tmp_path, monkeypatch):
    monkeypatch.setattr(AppSettings, "get_config_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(AppSettings, "_default_provider", None)

    settings = AppSettings()

    assert AppSettings.DEFAULT_SETTINGS["timing"]["preview_guide_enabled"] is True
    assert settings.get("timing.preview_guide_enabled") is True
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["timing"]["preview_guide_enabled"] is True


def test_existing_disabled_preview_guide_is_preserved(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"timing": {"preview_guide_enabled": false}}', encoding="utf-8"
    )

    settings = AppSettings(config_path=str(config_path))
    settings.save()

    assert settings.get("timing.preview_guide_enabled") is False
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["timing"]["preview_guide_enabled"] is False
