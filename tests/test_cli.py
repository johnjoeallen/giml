import pytest

from giml import __version__
from giml.cli import ExitCode, main


def test_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"giml {__version__}"


def test_no_command_is_a_configuration_error(capsys):
    assert main([]) == ExitCode.CONFIGURATION
    assert "usage: giml" in capsys.readouterr().err
