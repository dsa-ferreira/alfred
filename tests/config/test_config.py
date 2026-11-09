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
    assert config.ai.max_requests_per_turn == 12
    assert config.ai.tool_result_max_chars == 4000
    assert config.ai.persona is None
    assert config.ai.owner_address == "sir"
    assert config.history.max_entries == 30
    assert config.queue.retry_initial_s == 30
    assert config.queue.retry_max_s == 900
    assert config.mcp.list_timeout_s == 10
    assert config.mcp.call_timeout_s == 60
    assert config.mcp.instructions_max_chars == 2000
    assert config.server.host == "0.0.0.0"
    assert config.server.port == 8080
    assert config.server.log_level == "INFO"
    assert config.mcp_servers[0].exclude_tools == frozenset()
    assert config.mcp_servers[0].use_server_instructions is True


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
instructions_max_chars = 500

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
    assert config.mcp.instructions_max_chars == 500
    assert (config.server.host, config.server.port, config.server.log_level) == (
        "127.0.0.1",
        9000,
        "DEBUG",
    )
    assert config.services == () and config.mcp_servers == ()


def test_server_instructions_can_be_muted(tmp_path: Path) -> None:
    text = MINIMAL.replace(
        'token_env = "ALFRED_MCP_TOKEN_MCPARR"',
        'token_env = "ALFRED_MCP_TOKEN_MCPARR"\nuse_server_instructions = false',
    )
    config = load_config(env_for(write(tmp_path, text)))
    assert config.mcp_servers[0].use_server_instructions is False


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


def test_instructions_max_chars_must_be_positive(tmp_path: Path) -> None:
    found = problems(MINIMAL + "\n[mcp]\ninstructions_max_chars = 0\n", tmp_path)
    assert any("mcp.instructions_max_chars" in p for p in found)


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


# --- Conversation memory and timezone -------------------------------------------


def test_memory_defaults(tmp_path: Path) -> None:
    config = load_config(env_for(write(tmp_path, MINIMAL)))
    memory = config.memory
    assert config.summary_model == "some-model"  # the [ai] model
    assert memory.summary_model is None
    assert memory.embedding_base_url == "http://ragman.lan"
    assert memory.embedding_model == "nomic-embed-text:v1.5"
    assert memory.embedding_timeout_s == 30
    assert (memory.document_prefix, memory.query_prefix) == ("search_document: ", "search_query: ")
    assert memory.recall_max_chars == 32000
    assert memory.summary_input_max_chars == 60000
    assert memory.search_results == 5


def test_memory_custom_values(tmp_path: Path) -> None:
    text = (
        MINIMAL
        + """
[memory]
summary_model = "small-model"
embedding_base_url = "https://embed.example.com/"
embedding_model = "bge-m3"
embedding_timeout_s = 5
document_prefix = ""
query_prefix = "query: "
recall_max_chars = 16000
summary_input_max_chars = 20000
search_results = 3
"""
    )
    config = load_config(env_for(write(tmp_path, text)))
    memory = config.memory
    assert config.summary_model == "small-model"
    assert memory.embedding_base_url == "https://embed.example.com"
    assert memory.embedding_model == "bge-m3"
    assert memory.embedding_timeout_s == 5
    assert (memory.document_prefix, memory.query_prefix) == ("", "query: ")
    assert memory.recall_max_chars == 16000
    assert memory.summary_input_max_chars == 20000
    assert memory.search_results == 3


def test_memory_invalid_url(tmp_path: Path) -> None:
    found = problems(MINIMAL + '\n[memory]\nembedding_base_url = "ragman.lan"\n', tmp_path)
    assert any("memory.embedding_base_url" in p for p in found)


@pytest.mark.parametrize(
    "key", ["recall_max_chars", "summary_input_max_chars", "search_results", "embedding_timeout_s"]
)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_memory_caps_must_be_positive(tmp_path: Path, key: str, value: str) -> None:
    found = problems(MINIMAL + f"\n[memory]\n{key} = {value}\n", tmp_path)
    assert any(f"memory.{key}" in p for p in found)


def test_memory_unknown_key(tmp_path: Path) -> None:
    found = problems(MINIMAL + "\n[memory]\nrecall_max_char = 100\n", tmp_path)
    assert any("memory.recall_max_char" in p and "unknown key" in p for p in found)


def test_web_defaults(tmp_path: Path) -> None:
    web = load_config(env_for(write(tmp_path, MINIMAL))).web
    assert web.searxng_url == "http://oracle.lan"
    assert web.search_results == 5
    assert web.fetch_page_chars == 8000
    assert web.fetch_timeout_s == 20
    assert web.fetch_max_bytes == 5_000_000


def test_web_custom_values(tmp_path: Path) -> None:
    text = (
        MINIMAL
        + """
[web]
searxng_url = "https://search.example.com/"
search_results = 3
fetch_page_chars = 4000
fetch_timeout_s = 5
fetch_max_bytes = 1000000
"""
    )
    web = load_config(env_for(write(tmp_path, text))).web
    assert web.searxng_url == "https://search.example.com"
    assert web.search_results == 3
    assert web.fetch_page_chars == 4000
    assert web.fetch_timeout_s == 5
    assert web.fetch_max_bytes == 1_000_000


def test_web_invalid_searxng_url(tmp_path: Path) -> None:
    found = problems(MINIMAL + '\n[web]\nsearxng_url = "oracle.lan"\n', tmp_path)
    assert any(p.startswith("web.searxng_url:") for p in found), found


@pytest.mark.parametrize(
    "key", ["search_results", "fetch_page_chars", "fetch_timeout_s", "fetch_max_bytes"]
)
def test_web_limits_must_be_positive(tmp_path: Path, key: str) -> None:
    found = problems(MINIMAL + f"\n[web]\n{key} = 0\n", tmp_path)
    assert any(f"web.{key}" in p for p in found)


def test_default_timezone(tmp_path: Path) -> None:
    assert load_config(env_for(write(tmp_path, MINIMAL))).ai.timezone == "Europe/Lisbon"


def test_custom_timezone(tmp_path: Path) -> None:
    text = MINIMAL.replace('model = "some-model"', 'model = "some-model"\ntimezone = "Asia/Tokyo"')
    assert load_config(env_for(write(tmp_path, text))).ai.timezone == "Asia/Tokyo"


@pytest.mark.parametrize("zone", ["Europe/Lisboa", "", "../etc/passwd"])
def test_unknown_timezone_is_rejected(tmp_path: Path, zone: str) -> None:
    text = MINIMAL.replace('model = "some-model"', f'model = "some-model"\ntimezone = "{zone}"')
    found = problems(text, tmp_path)
    assert any(p.startswith("ai.timezone:") for p in found), found


# --- Brains ---------------------------------------------------------------------


def test_brains_default_to_none(tmp_path: Path) -> None:
    assert load_config(env_for(write(tmp_path, MINIMAL))).ai.models == {}


def test_brains_are_read(tmp_path: Path) -> None:
    text = MINIMAL + '\n[ai.models]\nsome = "some-model"\nbonsai = "bonsai:8b"\n'
    config = load_config(env_for(write(tmp_path, text)))
    assert {a: (e.model, e.vision) for a, e in config.ai.models.items()} == {
        "some": ("some-model", False),
        "bonsai": ("bonsai:8b", False),
    }


@pytest.mark.parametrize("alias", ["Gemma", "-gemma", "gem_ma"])
def test_bad_brain_alias_is_named(tmp_path: Path, alias: str) -> None:
    found = problems(MINIMAL + f'\n[ai.models]\n"{alias}" = "gemma4:26b-a4b-it-qat"\n', tmp_path)
    assert any(p.startswith(f"ai.models.{alias}:") for p in found), found


def test_same_brain_tag_twice_names_both_aliases(tmp_path: Path) -> None:
    text = (
        MINIMAL + '\n[ai.models]\ngemma = "gemma4:26b-a4b-it-qat"\ng4 = "gemma4:26b-a4b-it-qat"\n'
    )
    found = problems(text, tmp_path)
    assert any(p.startswith("ai.models:") and "'gemma'" in p and "'g4'" in p for p in found)


def test_empty_brain_tag_is_named(tmp_path: Path) -> None:
    found = problems(MINIMAL + '\n[ai.models]\nbonsai = ""\n', tmp_path)
    assert any(p.startswith("ai.models.bonsai") for p in found), found


def test_alias_clashing_with_the_unaliased_startup_brain(tmp_path: Path) -> None:
    found = problems(MINIMAL + '\n[ai.models]\nsome-model = "bonsai:8b"\n', tmp_path)
    assert any(p.startswith("ai.models:") and "'some-model'" in p for p in found), found


# --- Reminders ------------------------------------------------------------------


def test_reminder_defaults(tmp_path: Path) -> None:
    reminders = load_config(env_for(write(tmp_path, MINIMAL))).reminders
    assert reminders.default_time == "09:00"
    assert reminders.check_interval_s == 30


def test_reminder_custom_values(tmp_path: Path) -> None:
    text = MINIMAL + '\n[reminders]\ndefault_time = "07:30"\ncheck_interval_s = 10\n'
    reminders = load_config(env_for(write(tmp_path, text))).reminders
    assert reminders.default_time == "07:30"
    assert reminders.check_interval_s == 10


@pytest.mark.parametrize("value", ["9am", "9:00", "24:00", "12:60", ""])
def test_invalid_default_time(tmp_path: Path, value: str) -> None:
    found = problems(MINIMAL + f'\n[reminders]\ndefault_time = "{value}"\n', tmp_path)
    assert any(p.startswith("reminders.default_time:") for p in found), found


@pytest.mark.parametrize("value", ["0", "61", "300"])
def test_check_interval_between_0_and_60(tmp_path: Path, value: str) -> None:
    found = problems(MINIMAL + f"\n[reminders]\ncheck_interval_s = {value}\n", tmp_path)
    assert any(p.startswith("reminders.check_interval_s:") for p in found), found


# --- Vision (change add-attachments) ----------------------------------------------


def test_vision_declared(tmp_path: Path) -> None:
    text = MINIMAL + (
        '\n[ai.models]\ngemma = { model = "gemma4:26b-a4b-it-qat", vision = true }\n'
        'bonsai = "bonsai:8b"\n'
    )
    config = load_config(env_for(write(tmp_path, text)))
    assert config.ai.models["gemma"].vision is True
    assert config.ai.models["gemma"].model == "gemma4:26b-a4b-it-qat"
    assert config.ai.models["bonsai"].vision is False


def test_vision_defaults_to_false(tmp_path: Path) -> None:
    assert load_config(env_for(write(tmp_path, MINIMAL))).ai.vision is False


def test_startup_model_without_an_alias_declares_vision(tmp_path: Path) -> None:
    text = MINIMAL.replace('model = "some-model"', 'model = "qwen3:14b"\nvision = true')
    config = load_config(env_for(write(tmp_path, text)))
    assert config.ai.vision is True


def test_same_tag_twice_one_as_a_table(tmp_path: Path) -> None:
    found = problems(
        MINIMAL + '\n[ai.models]\ngemma = "gemma4:26b-a4b-it-qat"\n'
        'g4 = { model = "gemma4:26b-a4b-it-qat", vision = true }\n',
        tmp_path,
    )
    assert any(p.startswith("ai.models:") and "'gemma'" in p and "'g4'" in p for p in found)


def test_unknown_key_in_a_brain_table(tmp_path: Path) -> None:
    found = problems(
        MINIMAL + '\n[ai.models]\ngemma = { model = "gemma4:26b-a4b-it-qat", vison = true }\n',
        tmp_path,
    )
    assert found == ["ai.models.gemma.vison: unknown key"], found


def test_brain_table_needs_a_model(tmp_path: Path) -> None:
    found = problems(MINIMAL + "\n[ai.models]\ngemma = { vision = true }\n", tmp_path)
    assert found == ["ai.models.gemma.model: required but not set"], found


# --- Attachments (change add-attachments) ----------------------------------------

ATTACHMENTS = '\n[attachments]\nendpoint_url = "http://garage.lan:3900"\nbucket = "alfred"\n'
S3 = {"ALFRED_S3_ACCESS_KEY_ID": "GKkey-id", "ALFRED_S3_SECRET_ACCESS_KEY": "s3-secret-value"}


def test_attachments_off_by_default(tmp_path: Path) -> None:
    config = load_config(env_for(write(tmp_path, MINIMAL)))
    assert config.attachments is None
    assert config.s3_key is None


def test_attachment_defaults(tmp_path: Path) -> None:
    config = load_config(env_for(write(tmp_path, MINIMAL + ATTACHMENTS), **S3))
    a = config.attachments
    assert a is not None
    assert (a.endpoint_url, a.bucket, a.region) == ("http://garage.lan:3900", "alfred", "garage")
    assert a.max_bytes == 20_000_000
    assert a.inline_turns == 1
    assert a.first_page_chars == 4000
    assert a.read_chars == 8000
    assert a.image_max_px == 1536
    assert a.album_wait_s == 2
    assert config.s3_key is not None
    assert config.s3_key.access_key_id.get_secret_value() == "GKkey-id"
    assert config.s3_key.secret_access_key.get_secret_value() == "s3-secret-value"


def test_attachment_custom_values(tmp_path: Path) -> None:
    text = (
        MINIMAL
        + ATTACHMENTS
        + (
            'region = "eu"\nmax_bytes = 1000\ninline_turns = 2\nfirst_page_chars = 10\n'
            "read_chars = 20\nimage_max_px = 800\nalbum_wait_s = 0.5\n"
        )
    )
    a = load_config(env_for(write(tmp_path, text), **S3)).attachments
    assert a is not None
    assert (a.region, a.max_bytes, a.inline_turns) == ("eu", 1000, 2)
    assert (a.first_page_chars, a.read_chars, a.image_max_px, a.album_wait_s) == (10, 20, 800, 0.5)


def test_size_limit_above_telegrams(tmp_path: Path) -> None:
    found = problems(MINIMAL + ATTACHMENTS + "max_bytes = 50000000\n", tmp_path, **S3)
    assert any(p.startswith("attachments.max_bytes:") and "20 MB" in p for p in found), found


def test_attachments_need_endpoint_and_bucket(tmp_path: Path) -> None:
    found = problems(MINIMAL + "\n[attachments]\n", tmp_path, **S3)
    assert "attachments.endpoint_url: required but not set" in found
    assert "attachments.bucket: required but not set" in found


def test_attachments_invalid_endpoint(tmp_path: Path) -> None:
    text = MINIMAL + ATTACHMENTS.replace("http://garage.lan:3900", "garage.lan")
    found = problems(text, tmp_path, **S3)
    assert any(p.startswith("attachments.endpoint_url:") for p in found), found


@pytest.mark.parametrize("missing", list(S3))
def test_object_store_key_missing(tmp_path: Path, missing: str) -> None:
    env = {k: v for k, v in S3.items() if k != missing}
    found = problems(MINIMAL + ATTACHMENTS, tmp_path, **env)
    assert any(p.startswith(f"{missing} is required") for p in found), found
