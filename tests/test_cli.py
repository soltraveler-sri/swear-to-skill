from __future__ import annotations

import pytest

from s2s.cli import COMMAND_ISSUES, main


@pytest.mark.parametrize("command, issue", COMMAND_ISSUES.items())
def test_each_stub_exits_successfully(command: str, issue: int, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([command]) == 0
    assert capsys.readouterr().out == f"{command}: not implemented yet (issue #{issue})\n"


def test_stub_commands_accept_their_documented_scaffold_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["schedule", "install"]) == 0
    assert main(["autonomy", "on"]) == 0
    assert main(["approve", "proposal-1"]) == 0
    assert main(["meter", "--open"]) == 0
    assert main(["pump", "--background"]) == 0
    assert "not implemented yet" in capsys.readouterr().out
