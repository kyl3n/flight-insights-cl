"""Weather as context for a flight's findings (spec: Weather and wind, "What the app does
with it"; rules in `knowledge/rules/weather.json`, "space_weather").

A GNSS or compass finding on a flight flown while the planetary K-index was elevated
carries that K-index and a note, so a disturbance is weighed against the sky before it
is blamed on the aircraft. Context only: no severity, confidence or recommendation
changes. It reads the flight's kept weather record and works nothing out.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flight_insights.knowledge import Pack
from flight_insights.store.db import Store
from flight_insights.weather import record


def space_weather(store: Store, pack: Pack, result: Mapping[str, Any]) -> None:
    """Adds `context` to the findings of an analysis result (`analysis_results`) that
    the K-index during the flight bears on."""
    rules: Mapping[str, Any] = pack.json("rules/weather.json")["space_weather"]
    kept = record.kept(store, [str(result["flight"])]).get(str(result["flight"]))
    kp = None if kept is None else kept.get("kp_max")
    if not isinstance(kp, int | float) or kp < float(rules["kp_elevated"]):
        return
    codes = tuple(str(c) for c in rules["codes"])
    for finding in result["findings"]:
        if str(finding["code"]).startswith(codes):
            note = {lang: str(text).format(kp=f"{kp:g}") for lang, text in rules["note"].items()}
            finding["context"] = {"kp_max": kp, "note": note}
