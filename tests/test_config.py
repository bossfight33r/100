from pathlib import Path

import pytest

from clipfactory.config import ConfigError, Settings, load_accounts, load_campaigns

ROOT = Path(__file__).resolve().parents[1]


def test_example_campaign_loads():
    campaigns = load_campaigns(ROOT / "config" / "campaigns")
    c = campaigns["example"]
    assert c.rate_per_1k_views == 1.5
    assert "#shorts" in c.must_include_tags
    assert c.clip_min_sec <= c.clip_max_sec


def test_accounts_fall_back_to_example(tmp_path):
    assert load_accounts(tmp_path / "accounts.yaml") == {}
    accounts = load_accounts(ROOT / "config" / "accounts.yaml")
    assert "yt_main" in accounts  # accounts.yaml отсутствует -> берётся accounts.example.yaml


def test_campaign_id_from_filename(tmp_path):
    (tmp_path / "promo.yaml").write_text(
        "name: P\nrate_per_1k_views: 2\nplatforms: [youtube]\n", encoding="utf-8"
    )
    assert load_campaigns(tmp_path)["promo"].name == "P"


def test_invalid_campaign_reports_file(tmp_path):
    (tmp_path / "bad.yaml").write_text("name: P\nplatforms: [youtube]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="bad.yaml"):
        load_campaigns(tmp_path)


def test_duplicate_account_ids(tmp_path):
    f = tmp_path / "accounts.yaml"
    f.write_text(
        "accounts:\n  - {id: a, platform: youtube, name: A}\n  - {id: a, platform: tiktok, name: B}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_accounts(f)


def test_settings_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CF_ADMIN_IDS", "1, 2,3")
    monkeypatch.setenv("CF_TRANSCRIBER", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    s = Settings()
    assert s.admin_ids == [1, 2, 3]
    assert s.transcriber == "fake"
    assert s.anthropic_api_key.get_secret_value() == "sk-ant-test"
    assert "sk-ant-test" not in repr(s)


def test_unknown_campaign(monkeypatch):
    monkeypatch.chdir(ROOT)
    with pytest.raises(ConfigError, match="example"):
        Settings().campaign("nope")
