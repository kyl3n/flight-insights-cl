"""The battery model on ArduCopter 4.7.0 SITL's heavy-lift pack (tests/fixtures/sitl/4.7.0/
copter-heavylift-discharge): the X8 hovers on a pack shrunk to 3 Ah, with SITL's 0.024 Ohm
and 50.4 V full, until the capacity failsafe switches it to LAND. Fitted on the hover from
arming to the failsafe, the pack's capacity comes back within 3% and its resistance within
10%, whatever capacity it is rated at."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from flight_insights.dataflash import read_dataflash, si
from flight_insights.history import log_firmware
from flight_insights.knowledge import load_pack
from flight_insights.performance.battery import chemistry, fit_pack
from flight_insights.segment import find_boots, flights, timeline

LOG = (
    Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0/copter-heavylift-discharge.bin"
)
CAPACITY_AH = 3.0  # SIM_BATT_CAP_AH
RESISTANCE = 0.024  # Callisto.json
CELLS = 12  # 50.4 V full at 4.2 V a cell


@pytest.fixture(scope="module")
def hover() -> dict[str, Any]:
    """BAT from arming to the failsafe's LAND, the motors working (above 30 A); the log has
    no rows before arming (LOG_DISARMED 0), so the pack's rest is its first quiet sample."""
    pack = load_pack()
    with read_dataflash(LOG) as df:
        vehicle, firmware = log_firmware(df)
        [flight] = flights(df, find_boots(timeline(df)), pack, vehicle, firmware)
        bat = df.table("BAT")
        t = np.asarray(bat.column("TimeUS").to_numpy(), np.int64)
        volts, amps = si.values(bat, "Volt"), si.values(bat, "Curr")
    [land] = [m for m in flight.modes if m.name == "LAND"]
    armed = (t >= flight.arm_us) & (t <= flight.disarm_us)
    quiet = np.flatnonzero(armed & (amps < 1.0))[0]
    use = armed & (t < land.start_us) & (amps > 30.0)
    return {
        "t": t[use] / 1e6, "v": volts[use], "i": amps[use],
        "rest_v": float(volts[quiet]), "rest_a": float(amps[quiet]),
        "rules": dict(pack.json("rules/ocv.json")),
    }  # fmt: skip


@pytest.mark.parametrize("rated_ah", [3.0, 4.0])
def test_the_simulators_pack_comes_back_from_its_discharge(
    hover: dict[str, Any], rated_ah: float
) -> None:
    rules = hover["rules"]
    fit = fit_pack(hover["t"], hover["v"], hover["i"], hover["rest_v"], hover["rest_a"], CELLS,
                   chemistry(rules, "LiPo"), rated_ah, rules)  # fmt: skip
    assert fit.capacity_ah == pytest.approx(CAPACITY_AH, rel=0.03)
    assert fit.resistance == pytest.approx(RESISTANCE, rel=0.10)
    assert fit.start_soc > 0.97 and fit.end_soc < 0.2  # most of the curve
    assert fit.used_ah == pytest.approx(2.55, abs=0.03)  # the firmware's count at the failsafe
