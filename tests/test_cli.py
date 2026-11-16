import dataclasses

import pytest

import alfred.config
from alfred.cli import _main
from alfred.kernel.access import AllowList
from tests.helpers import ANA, DIOGO, make_config


def test_help_lists_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        _main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "serve" in out
    assert "migrate" in out


def test_unknown_alfred_tool_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ana = dataclasses.replace(ANA, tools=AllowList.of("alfred_nope"))
    monkeypatch.setattr(alfred.config, "load_config", lambda: make_config(users=(DIOGO, ana)))
    assert _main(["serve"]) == 2
    assert "users[1] (ana): tools entry 'alfred_nope' is not an alfred tool" in (
        capsys.readouterr().err
    )
