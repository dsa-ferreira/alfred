"""``alfred.toml.example`` loads, and every config field is documented in the README."""

from __future__ import annotations

import re
import tomllib
import types
from pathlib import Path
from typing import Any

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


def test_example_brain_tables_load(tmp_path: Path) -> None:
    """The commented ``[ai.models]`` example, uncommented, gives gemma and bonsai vision."""
    text = EXAMPLE.read_text()
    block = text.split("# [ai.models]\n", 1)[1].split("\n\n", 1)[0]
    uncommented = "[ai.models]\n" + "\n".join(line[2:] for line in block.splitlines())
    models = tomllib.loads(uncommented)["ai"]["models"]
    assert models["gemma"] == {"model": "gemma4:26b-a4b-it-qat", "vision": True}
    assert models["bonsai"]["vision"] is True
    path = tmp_path / "alfred.toml"
    path.write_text(text.replace("# [ai.models]\n" + block, uncommented))
    env = {
        "ALFRED_CONFIG": str(path),
        "ALFRED_TELEGRAM_BOT_TOKEN": "t",
        "ALFRED_DATABASE_URL": "postgresql://u:p@h/d",
        "ALFRED_SERVICE_TOKEN_BACKUP": "a",
        "ALFRED_MCP_TOKEN_MCPARR": "b",
        "ALFRED_MCP_TOKEN_COOKBOOK": "c",
    }
    config = load_config(env)
    assert config.ai.models["gemma"].vision and not config.ai.models["qwen"].vision


def _section(annotation: Any) -> tuple[type[BaseModel] | None, bool]:
    """The section's model, and whether the section is optional (``Model | None``)."""
    if isinstance(annotation, types.UnionType):
        models = [a for a in annotation.__args__ if a is not type(None)]
        if len(models) == 1 and issubclass(models[0], BaseModel):
            return models[0], True
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation, False
    return None, False


def test_optional_section_shown_commented_out() -> None:
    """An optional section is off by default, so the example shows it commented out."""
    text = EXAMPLE.read_text()
    for section, field in FileConfig.model_fields.items():
        model, optional = _section(field.annotation)
        if model is None or not optional:
            continue
        assert re.search(rf"^# \[{section}\]$", text, re.M), section
        body = text.split(f"# [{section}]", 1)[1].split("\n\n", 1)[0]
        commented = tomllib.loads(
            "\n".join(line[2:] for line in body.splitlines() if re.match(r"# [a-z_]+ =", line))
        )
        for key, info in model.model_fields.items():
            assert key in commented, f"{section}.{key} missing from the example"
            if not info.is_required():
                assert commented[key] == info.default, f"{section}.{key} default differs"


def test_example_shows_every_default() -> None:
    data = tomllib.loads(EXAMPLE.read_text())
    for section, field in FileConfig.model_fields.items():
        annotation, optional = _section(field.annotation)
        if annotation is None or optional:
            continue
        for key, info in annotation.model_fields.items():
            if key in ("persona", "summary_model"):  # optional, no default value
                assert re.search(rf"^# {key} =", EXAMPLE.read_text(), re.M)
                continue
            if key == "models":  # a table, shown commented out
                assert re.search(rf"^# \[{section}\.{key}\]$", EXAMPLE.read_text(), re.M)
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
        annotation, _ = _section(info.annotation)
        if annotation is not None:
            expected |= {f"{section}.{key}" for key in annotation.model_fields}
    expected |= {f"services.{key}" for key in ServiceEntry.model_fields}
    expected |= {f"mcp_servers.{key}" for key in McpServerEntry.model_fields}
    expected |= {"[[services]]", "[[mcp_servers]]"}
    assert expected <= documented, expected - documented


def test_every_variable_in_readme() -> None:
    text = README.read_text()
    for var in (
        "ALFRED_CONFIG",
        "ALFRED_TELEGRAM_BOT_TOKEN",
        "ALFRED_DATABASE_URL",
        "ALFRED_S3_ACCESS_KEY_ID",
        "ALFRED_S3_SECRET_ACCESS_KEY",
    ):
        assert f"| `{var}` |" in text


def test_mcp_section_documents_server_fields() -> None:
    section = README.read_text().split("## MCP servers", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", section, re.M))
    assert documented == set(McpServerEntry.model_fields)
