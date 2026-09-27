import os
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from carbon.settings import Settings, get_settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate tests from the developer's shell environment."""
    for key in list(os.environ):
        if key.startswith("CARBON_"):
            monkeypatch.delenv(key)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def load(**kwargs) -> Settings:
    # _env_file=None so a local .env never leaks into tests.
    return Settings(_env_file=None, **kwargs)


def test_defaults_load():
    s = load()
    assert s.env == "local"
    assert s.cors_origins == ["http://localhost:3000"]
    assert s.search_provider == "none"
    assert s.search_api_key is None
    assert s.dynamodb_table == "carbon-local"
    assert s.s3_bucket == "carbon-artifacts-local"
    assert s.sqs_queue_url is None
    assert s.max_words_per_side == 5_000
    assert s.max_sources == 10
    assert s.max_queries_per_check == 20
    assert s.daily_query_cap == 1_000
    assert s.containment_threshold == 0.05
    assert s.paraphrase_threshold == 0.80


def test_env_vars_override_defaults(monkeypatch):
    monkeypatch.setenv("CARBON_ENV", "prod")
    monkeypatch.setenv("CARBON_CORS_ORIGINS", "https://carbon.app, https://www.carbon.app")
    monkeypatch.setenv("CARBON_SEARCH_PROVIDER", "brave")
    monkeypatch.setenv("CARBON_SEARCH_API_KEY", "test-key")
    monkeypatch.setenv("CARBON_DYNAMODB_TABLE", "carbon-prod")
    monkeypatch.setenv("CARBON_S3_BUCKET", "carbon-artifacts-prod")
    monkeypatch.setenv("CARBON_SQS_QUEUE_URL", "https://sqs.example/queue")
    monkeypatch.setenv("CARBON_MAX_WORDS_PER_SIDE", "8000")
    monkeypatch.setenv("CARBON_MAX_SOURCES", "5")
    monkeypatch.setenv("CARBON_MAX_QUERIES_PER_CHECK", "12")
    monkeypatch.setenv("CARBON_DAILY_QUERY_CAP", "300")
    monkeypatch.setenv("CARBON_CONTAINMENT_THRESHOLD", "0.1")
    monkeypatch.setenv("CARBON_PARAPHRASE_THRESHOLD", "0.9")

    s = load()
    assert s.env == "prod"
    assert s.cors_origins == ["https://carbon.app", "https://www.carbon.app"]
    assert s.search_provider == "brave"
    assert s.search_api_key is not None
    assert s.search_api_key.get_secret_value() == "test-key"
    assert s.dynamodb_table == "carbon-prod"
    assert s.s3_bucket == "carbon-artifacts-prod"
    assert s.sqs_queue_url == "https://sqs.example/queue"
    assert s.max_words_per_side == 8_000
    assert s.max_sources == 5
    assert s.max_queries_per_check == 12
    assert s.daily_query_cap == 300
    assert s.containment_threshold == 0.1
    assert s.paraphrase_threshold == 0.9


def test_env_file_is_read(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("CARBON_MAX_SOURCES=3\nCARBON_SQS_QUEUE_URL=\n")
    s = Settings(_env_file=env_file)
    assert s.max_sources == 3
    assert s.sqs_queue_url is None  # blank falls back to default


def test_env_example_matches_defaults():
    assert Settings(_env_file=ENV_EXAMPLE) == load()


def test_env_example_lists_every_field():
    keys = set(re.findall(r"^(CARBON_\w+)=", ENV_EXAMPLE.read_text(), re.MULTILINE))
    assert keys == {f"CARBON_{name.upper()}" for name in Settings.model_fields}


def test_secret_not_in_repr(monkeypatch):
    monkeypatch.setenv("CARBON_SEARCH_API_KEY", "super-secret")
    s = load()
    assert "super-secret" not in repr(s)


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("CARBON_ENV", "staging"),
        ("CARBON_SEARCH_PROVIDER", "altavista"),
        ("CARBON_MAX_SOURCES", "0"),
        ("CARBON_PARAPHRASE_THRESHOLD", "1.5"),
    ],
)
def test_invalid_values_rejected(monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    with pytest.raises(ValidationError):
        load()


PROD_OK = {
    "env": "prod",
    "cors_origins": ["https://carbon.app"],
    "search_provider": "brave",
    "search_api_key": "k",
    "sqs_queue_url": "https://sqs.example/queue",
}


def test_prod_accepts_complete_config():
    assert load(**PROD_OK).env == "prod"


@pytest.mark.parametrize(
    "overrides",
    [
        {"cors_origins": ["*"]},
        {"search_api_key": None},
        {"sqs_queue_url": None},
    ],
)
def test_prod_rejects_unsafe_config(overrides):
    with pytest.raises(ValidationError):
        load(**(PROD_OK | overrides))


def test_get_settings_is_cached(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    first = get_settings()
    monkeypatch.setenv("CARBON_MAX_SOURCES", "7")
    assert get_settings() is first
    get_settings.cache_clear()
    assert get_settings().max_sources == 7
