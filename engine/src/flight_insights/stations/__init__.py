"""Ground station profiles: the computer, software, connections, radios, antennas
and clock on the ground side, versioned like an airframe's build."""

from flight_insights.stations.computer import FactsError, computer_answers, hash_host
from flight_insights.stations.detect import Installed, installed_software, software_answers
from flight_insights.stations.folders import add_folder, list_folders, remove_folder
from flight_insights.stations.records import (
    create_station,
    list_stations,
    record_computer,
    rename_station,
    save_profile,
    station_for_host,
    station_view,
)

__all__ = [
    "FactsError",
    "Installed",
    "add_folder",
    "computer_answers",
    "create_station",
    "hash_host",
    "installed_software",
    "list_folders",
    "list_stations",
    "record_computer",
    "remove_folder",
    "rename_station",
    "save_profile",
    "software_answers",
    "station_for_host",
    "station_view",
]
