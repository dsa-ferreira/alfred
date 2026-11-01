"""Startup errors for invalid configurations never print a secret value."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from alfred.cli import main
from tests.config.test_config import MINIMAL, SECRETS

# Unmistakable values so a leak is easy to spot.
LEAKY = {
    "ALFRED_TELEGRAM_BOT_TOKEN": "LEAK-bot-token-1",
    "ALFRED_DATABASE_URL": "mysql://alfred:LEAK-db-password-2@db.lan/alfred",
    "ALFRED_SERVICE_TOKEN_BACKUP": "LEAK-service-token-3",
    "ALFRED_SERVICE_TOKEN_DISK": "LEAK-service-token-3",  # duplicate of backup's
    "ALFRED_MCP_TOKEN_MCPARR": "LEAK-mcp-token-4",
}

INVALID_CONFIGS = {
    "duplicate tokens and bad db url": MINIMAL
    + '\n[[services]]\nname = "disk-check"\ntoken_env = "ALFRED_SERVICE_TOKEN_DISK"\n',
    "unknown key and bad url": MINIMAL.replace("http://mcparr.lan:8090/mcp", "nope")
    + "\n[history]\nmax_mesages = 1\n",
    "token_env not an ALFRED variable": MINIMAL.replace(
        "ALFRED_SERVICE_TOKEN_BACKUP", "LEAK-service-token-3"
    ),
}


@pytest.mark.parametrize("text", INVALID_CONFIGS.values(), ids=INVALID_CONFIGS.keys())
def test_startup_error_output_has_no_secrets(
    text: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "alfred.toml"
    path.write_text(text)
    for name in SECRETS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALFRED_CONFIG", str(path))
    for name, value in LEAKY.items():
        monkeypatch.setenv(name, value)

    with caplog.at_level(logging.DEBUG):
        assert main(["serve"]) == 2
    captured = capsys.readouterr()
    output = captured.out + captured.err + caplog.text
    assert "invalid configuration" in output
    assert "LEAK-" not in output
