"""Platforms, airframes and build configuration, with the guided setup."""

from flight_insights.platforms.completeness import Completeness, Gap, completeness
from flight_insights.platforms.detect import detect
from flight_insights.platforms.records import (
    airframe_view,
    create_airframe,
    create_platform,
    list_airframes,
    list_platforms,
    platform_view,
    record_change,
    save_answers,
    teams,
    update_airframe,
)
from flight_insights.platforms.schema import Field, Schema, SetupError, check_value, load_schema

__all__ = [
    "Completeness",
    "Field",
    "Gap",
    "Schema",
    "SetupError",
    "airframe_view",
    "check_value",
    "completeness",
    "create_airframe",
    "create_platform",
    "detect",
    "list_airframes",
    "list_platforms",
    "load_schema",
    "platform_view",
    "record_change",
    "save_answers",
    "teams",
    "update_airframe",
]
