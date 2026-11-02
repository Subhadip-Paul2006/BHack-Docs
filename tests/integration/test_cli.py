"""CLI behaviour tests: version, --help, unknown subcommand against argparse."""

from __future__ import annotations

import pytest

from ps05.cli.main import cli_entrypoint


def test_version_exits_zero_and_prints_package_version(capsys: pytest.CaptureFixture):
    assert cli_entrypoint(["version"]) == 0
    captured = capsys.readouterr()
    assert "0.1.0" in captured.out
    assert "ps05" in captured.out


def test_help_lists_the_real_subcommands(capsys: pytest.CaptureFixture):
    with pytest.raises(SystemExit) as excinfo:
        cli_entrypoint(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for subcommand in ("serve", "worker", "migrate", "version"):
        assert subcommand in out


def test_unknown_subcommand_exits_nonzero():
    # subparsers are built with required=True, so an unknown command is an
    # argparse error -> SystemExit(2) before any handler dispatches.
    with pytest.raises(SystemExit) as excinfo:
        cli_entrypoint(["frobnicate"])
    assert excinfo.value.code != 0
