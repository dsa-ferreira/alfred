from __future__ import annotations

from pathlib import Path

import pytest

from alfred.config import ConfigError, load_config

MINIMAL = """
[telegram]
owner_user_id = 42

[ai]
model = "some-model"

[[services]]
name = "backup"
token_env = "ALFRED_SERVICE_TOKEN_BACKUP"

[[mcp_servers]]
name = "mcparr"
url = "http://mcparr.lan:8090/mcp"
token_env = "ALFRED_MCP_TOKEN_MCPARR"
"""

DISK_CHECK = '\n[[services]]\nname = "disk-check"\ntoken_env = "ALFRED_SERVICE_TOKEN_DISK"\n'

SECRETS = {
    "ALFRED_TELEGRAM_BOT_TOKEN": "123:bot-secret-value",
    "ALFRED_DATABASE_URL": "postgresql://alfred:db-pass-xyz@db.lan/alfred",
    "ALFRED_SERVICE_TOKEN_BACKUP": "svc-secret-backup",
    "ALFRED_MCP_TOKEN_MCPARR": "mcp-secret-mcparr",
}


def write(tmp_path: Path, text: str, name: str = "alfred.toml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def env_for(path: Path, **extra: str) -> dict[str, str]:
    return {"ALFRED_CONFIG": str(path), **SECRETS, **extra}


def problems(text: str, tmp_path: Path, **extra: str) -> list[str]:
    path = write(tmp_path, text)
    with pytest.raises(ConfigError) as exc:
        load_config(env_for(path, **extra))
    return exc.value.problems


# --- Config file location -------------------------------------------------------


def test_custom_path(tmp_path: Path) -> None:
    path = write(tmp_path, MINIMAL, "custom.toml")
    config = load_config(env_for(path))
    assert config.telegram.owner_user_id == 42


def test_default_path_is_alfred_toml_in_working_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, MINIMAL)
    monkeypatch.chdir(tmp_path)
    config = load_config(dict(SECRETS))
    assert config.ai.model == "some-model"


def test_missing_file_names_path(tmp_path: Path) -> None:
    missing = tmp_path / "nope.toml"
    with pytest.raises(ConfigError) as exc:
        load_config(env_for(missing))
    assert str(missing) in str(exc.value)


def test_unparseable_file_names_path(tmp_path: Path) -> None:
    path = write(tmp_path, "[telegram\nowner_user_id = ")
    with pytest.raises(ConfigError) as exc:
        load_config(env_for(path))
    assert str(path) in str(exc.value)


def test_serve_exits_non_zero_on_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from alfred.cli import main

    missing = tmp_path / "nope.toml"
    monkeypatch.setenv("ALFRED_CONFIG", str(missing))
    assert main(["serve"]) != 0
    assert str(missing) in capsys.readouterr().err


# --- All tunables in the file ---------------------------------------------------


def test_minimal_file_takes_defaults(tmp_path: Path) -> None:
    config = load_config(env_for(write(tmp_path, MINIMAL)))
    assert config.ai.base_url == "http://ai.lan"
    assert config.ai.request_timeout_s == 300
    assert config.ai.max_requests_per_turn == 8
    assert config.ai.tool_result_max_chars == 4000
    assert config.ai.persona is None
    assert config.ai.owner_address == "sir"
    assert config.history.max_entries == 30
    assert config.queue.retry_initial_s == 30
    assert config.queue.retry_max_s == 900
    assert config.mcp.list_timeout_s == 10
    assert config.mcp.call_timeout_s == 60
    assert config.server.host == "0.0.0.0"
    assert config.server.port == 8080
    assert config.server.log_level == "INFO"
    assert config.mcp_servers[0].exclude_tools == frozenset()


def test_every_setting_can_be_changed(tmp_path: Path) -> None:
    text = """
[telegram]
owner_user_id = 7

[ai]
base_url = "https://llm.example.com/"
model = "other"
request_timeout_s = 60
max_requests_per_turn = 3
tool_result_max_chars = 100
persona = "Be terse."
owner_address = "Mr. Ferreira"

[history]
max_entries = 50

[queue]
retry_initial_s = 5
retry_max_s = 10

[mcp]
list_timeout_s = 2
call_timeout_s = 3

[server]
host = "127.0.0.1"
port = 9000
log_level = "DEBUG"
"""
    config = load_config(env_for(write(tmp_path, text)))
    assert config.ai.base_url == "https://llm.example.com"
    assert config.ai.model == "other"
    assert config.ai.request_timeout_s == 60
    assert config.ai.max_requests_per_turn == 3
    assert config.ai.tool_result_max_chars == 100
    assert config.ai.persona == "Be terse."
    assert config.ai.owner_address == "Mr. Ferreira"
    assert config.history.max_entries == 50
    assert (config.queue.retry_initial_s, config.queue.retry_max_s) == (5, 10)
    assert (config.mcp.list_timeout_s, config.mcp.call_timeout_s) == (2, 3)
    assert (config.server.host, config.server.port, config.server.log_level) == (
        "127.0.0.1",
        9000,
        "DEBUG",
    )
    assert config.services == () and config.mcp_servers == ()


# --- Strict validation ----------------------------------------------------------


def test_unknown_key_is_named(tmp_path: Path) -> None:
    found = problems(MINIMAL + "\n[history]\nmax_mesages = 30\n", tmp_path)
    assert any("history.max_mesages" in p and "unknown key" in p for p in found)


def test_old_system_prompt_key_is_rejected(tmp_path: Path) -> None:
    text = MINIMAL.replace(
        'model = "some-model"', 'model = "some-model"\nsystem_prompt = "Be terse."'
    )
    found = problems(text, tmp_path)
    assert any("ai.system_prompt" in p and "unknown key" in p for p in found)


def test_empty_owner_address_is_rejected(tmp_path: Path) -> None:
    text = MINIMAL.replace('model = "some-model"', 'model = "some-model"\nowner_address = ""')
    found = problems(text, tmp_path)
    assert any("ai.owner_address" in p for p in found)


def test_wrong_type(tmp_path: Path) -> None:
    found = problems(MINIMAL.replace("owner_user_id = 42", 'owner_user_id = "me"'), tmp_path)
    assert any("telegram.owner_user_id" in p for p in found)


def test_several_problems_reported_together(tmp_path: Path) -> None:
    text = MINIMAL.replace("http://mcparr.lan:8090/mcp", "not a url") + (
        '\n[[mcp_servers]]\nname = "mcparr"\nurl = "http://x.lan/mcp"\n'
        'token_env = "ALFRED_MCP_TOKEN_MCPARR"\n'
    )
    found = problems(text, tmp_path)
    assert any("mcp_servers[0] (mcparr).url" in p for p in found)
    assert any("duplicate name 'mcparr'" in p for p in found)


def test_duplicate_service_names(tmp_path: Path) -> None:
    text = MINIMAL + '\n[[services]]\nname = "backup"\ntoken_env = "ALFRED_SERVICE_TOKEN_B2"\n'
    found = problems(text, tmp_path, ALFRED_SERVICE_TOKEN_B2="other")
    assert any("services: duplicate name 'backup'" in p for p in found)


@pytest.mark.parametrize("name", ["Backup", "back_up", "back up", "-x", ""])
def test_name_rules(tmp_path: Path, name: str) -> None:
    found = problems(MINIMAL.replace('name = "backup"', f'name = "{name}"'), tmp_path)
    assert any(p.startswith("services[0]") and ".name" in p for p in found)


def test_reserved_server_name(tmp_path: Path) -> None:
    found = problems(MINIMAL.replace('name = "mcparr"', 'name = "alfred"'), tmp_path)
    assert any("'alfred' is reserved" in p for p in found)


def test_token_env_must_be_alfred_variable(tmp_path: Path) -> None:
    text = MINIMAL.replace("ALFRED_SERVICE_TOKEN_BACKUP", "BACKUP_TOKEN")
    found = problems(text, tmp_path)
    assert any("services[0] (backup).token_env" in p for p in found)


def test_retry_max_below_initial(tmp_path: Path) -> None:
    found = problems(MINIMAL + "\n[queue]\nretry_initial_s = 60\nretry_max_s = 10\n", tmp_path)
    assert any("retry_max_s" in p for p in found)


def test_missing_owner(tmp_path: Path) -> None:
    found = problems(MINIMAL.replace("[telegram]\nowner_user_id = 42\n", ""), tmp_path)
    assert any("telegram: required" in p for p in found)


# --- Secrets --------------------------------------------------------------------


def test_secrets_resolved(tmp_path: Path) -> None:
    config = load_config(env_for(write(tmp_path, MINIMAL)))
    assert config.telegram_bot_token.get_secret_value() == SECRETS["ALFRED_TELEGRAM_BOT_TOKEN"]
    assert config.services[0].token.get_secret_value() == "svc-secret-backup"
    assert config.mcp_servers[0].token.get_secret_value() == "mcp-secret-mcparr"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgresql://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        ("postgres://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
    ],
)
def test_database_url_normalised(tmp_path: Path, raw: str, expected: str) -> None:
    config = load_config(env_for(write(tmp_path, MINIMAL), ALFRED_DATABASE_URL=raw))
    assert config.database_url.get_secret_value() == expected


@pytest.mark.parametrize("value", ["", "   "])
def test_missing_referenced_variable(tmp_path: Path, value: str) -> None:
    found = problems(MINIMAL, tmp_path, ALFRED_MCP_TOKEN_MCPARR=value)
    assert any("ALFRED_MCP_TOKEN_MCPARR" in p and "mcp_servers[0] (mcparr)" in p for p in found)


def test_missing_bot_token_and_database_url(tmp_path: Path) -> None:
    path = write(tmp_path, MINIMAL)
    env = env_for(path)
    del env["ALFRED_TELEGRAM_BOT_TOKEN"], env["ALFRED_DATABASE_URL"]
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert any("ALFRED_TELEGRAM_BOT_TOKEN" in p for p in exc.value.problems)
    assert any("ALFRED_DATABASE_URL" in p for p in exc.value.problems)


def test_duplicate_service_tokens_name_both_services(tmp_path: Path) -> None:
    text = MINIMAL + DISK_CHECK
    found = problems(text, tmp_path, ALFRED_SERVICE_TOKEN_DISK="svc-secret-backup")
    dup = [p for p in found if "same token" in p]
    assert dup and "'backup'" in dup[0] and "'disk-check'" in dup[0]
    assert all("svc-secret-backup" not in p for p in found)


def test_config_and_secret_problems_together(tmp_path: Path) -> None:
    text = MINIMAL.replace("owner_user_id = 42", "owner_user_id = -1")
    found = problems(text, tmp_path, ALFRED_SERVICE_TOKEN_BACKUP="")
    assert any("owner_user_id" in p for p in found)
    assert any("ALFRED_SERVICE_TOKEN_BACKUP" in p for p in found)


def test_resolved_config_repr_hides_secrets(tmp_path: Path) -> None:
    config = load_config(env_for(write(tmp_path, MINIMAL)))
    text = repr(config)
    for secret in SECRETS.values():
        assert secret not in text
    assert "db-pass-xyz" not in text
