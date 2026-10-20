import pytest

from flight_insights import __version__
from flight_insights.cli import main


def test_version_flag_prints_engine_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"fi-engine {__version__}"


def test_no_arguments_exits_cleanly() -> None:
    assert main([]) == 0
