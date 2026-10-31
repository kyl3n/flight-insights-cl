"""The validation runner on open measured data committed with credit
(tests/fixtures/props/piljek: Piljek 2023, CC0 1.0, T-Motor G30x10.5 and G32x11 on three
motors, see its README). The 4.0 V tables only, to keep CI quick: each propeller predicted
from the other on the same motor as the app predicts a candidate. Holds the model to the
targets on real tables, so a change that makes it worse fails here."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from validate import validate

FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "props" / "piljek"


def test_open_tables_meet_the_targets(tmp_path: Path) -> None:
    for p in FIXTURE.glob("*_4.0V.json"):
        shutil.copy(p, tmp_path / p.name)
    got = validate(sorted(tmp_path.glob("*.json")))
    assert (got["tables"], got["motors"], got["pairs"]) == (8, 4, 8)
    assert got["meets_targets"] and not got["enough"]  # 8 pairs: two short of the 10
    h = got["hover"]
    assert h["median"] < 0.03 and h["p90"] < 0.06 and h["inside_95"] > 0.9
    assert got["most_thrust"]["largest"] < 0.06
