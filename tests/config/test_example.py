"""``alfred.toml.example`` loads, and every config field is documented in the README."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from pydantic import BaseModel

from alfred.config import FileConfig, McpServerEntry, ServiceEntry, load_config

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "alfred.toml.example"
README = ROOT / "README.md"


def test_example_loads() -> None:
    env = {
        "ALFRED_CONFIG": str(EXAMPLE),
        "ALFRED_TELEGRAM_BOT_TOKEN": "t",
        "ALFRED_DATABASE_URL": "postgresql://u:p@h/d",
        "ALFRED_SERVICE_TOKEN_BACKUP": "a",
        "ALFRED_MCP_TOKEN_MCPARR": "b",
        "ALFRED_MCP_TOKEN_COOKBOOK": "c",
    }
    config = load_config(env)
    assert [s.name for s in config.mcp_servers] == ["mcparr", "cookbook"]


def test_example_shows_every_default() -> None:
    data = tomllib.loads(EXAMPLE.read_text())
    for section, model in FileConfig.model_fields.items():
        annotation = model.annotation
        if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
            continue
        for key, info in annotation.model_fields.items():
            if key == "system_prompt":
                assert re.search(r"^# system_prompt =", EXAMPLE.read_text(), re.M)
                continue
            assert key in data[section], f"{section}.{key} missing from the example"
            if not info.is_required():
                assert data[section][key] == info.default, f"{section}.{key} default differs"


def _documented_keys() -> set[str]:
    return set(re.findall(r"^\| `([a-z_.\[\]]+)` \|", README.read_text(), re.M))


def test_every_field_in_readme() -> None:
    documented = _documented_keys()
    expected: set[str] = set()
    for section, info in FileConfig.model_fields.items():
        annotation = info.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            expected |= {f"{section}.{key}" for key in annotation.model_fields}
    expected |= {f"services.{key}" for key in ServiceEntry.model_fields}
    expected |= {f"mcp_servers.{key}" for key in McpServerEntry.model_fields}
    expected |= {"[[services]]", "[[mcp_servers]]"}
    assert expected <= documented, expected - documented


def test_every_variable_in_readme() -> None:
    text = README.read_text()
    for var in ("ALFRED_CONFIG", "ALFRED_TELEGRAM_BOT_TOKEN", "ALFRED_DATABASE_URL"):
        assert f"| `{var}` |" in text


def test_mcp_section_documents_server_fields() -> None:
    section = README.read_text().split("## MCP servers", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", section, re.M))
    assert documented == set(McpServerEntry.model_fields)
