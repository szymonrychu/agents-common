import os

import pytest
from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from agents_common.settings import BaseServiceSettings, CsvList, SettingsError, load_settings

pytestmark = pytest.mark.critical


class WebhookSettings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="WEBHOOK_", extra="ignore")

    hosts: CsvList = []


def _env(monkeypatch, **kw):
    for k in list(os.environ):
        if k.startswith(("WEBHOOK_", "OTHER_")):
            monkeypatch.delenv(k)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_settings_errors_name_every_bad_variable_without_secrets(monkeypatch):
    _env(monkeypatch, WEBHOOK_HTTP_PORT="abc", WEBHOOK_ORCHESTRATOR_TOKEN="s3cr3t-value")
    with pytest.raises(SettingsError) as ei:
        load_settings(WebhookSettings)
    msg = str(ei.value)
    assert "WEBHOOK_DATABASE_URL: required" in msg
    assert "WEBHOOK_ORCHESTRATOR_URL: required" in msg
    assert "WEBHOOK_HTTP_PORT" in msg
    assert "s3cr3t-value" not in msg


def test_settings_repr_hides_secret(monkeypatch):
    _env(
        monkeypatch,
        WEBHOOK_DATABASE_URL="postgresql://u:pw-secret@h/d",
        WEBHOOK_ORCHESTRATOR_URL="http://o",
        WEBHOOK_ORCHESTRATOR_TOKEN="tok-secret",
    )
    s = load_settings(WebhookSettings)
    assert isinstance(s.orchestrator_token, SecretStr)
    assert "tok-secret" not in repr(s)
    assert "pw-secret" not in repr(s)
