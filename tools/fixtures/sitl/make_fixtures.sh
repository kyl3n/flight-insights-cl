#!/usr/bin/env bash
# Fly SITL scenarios, scrub their files and add them to tests/fixtures/.
#
# Usage: make_fixtures.sh ARDUPILOT_DIR VERSION FIXTURES_DIR SCENARIO...
#   ARDUPILOT_DIR  checkout with `./waf copter heli plane` built for --board sitl
#   VERSION        firmware version of that checkout, e.g. 4.7.0
#   FIXTURES_DIR   usually tests/fixtures; files go to FIXTURES_DIR/sitl/VERSION/
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
ardupilot="$1" version="$2" fixtures="$3"
shift 3
max_bytes=$((8 * 1024 * 1024))

raw="$(mktemp -d)"
trap 'rm -rf "$raw"' EXIT

uv run "$here/run_scenario.py" --ardupilot "$ardupilot" --version "$version" --out "$raw" "$@"

dest="$fixtures/sitl/$version"
for scenario in "$@"; do
  uv run "$here/../scrub_fixture.py" --out "$dest" --manifest "$fixtures/SCRUBBED.sha256" \
    "$raw/$scenario.bin" "$raw/$scenario.tlog" "$raw/$scenario.param" "$raw/$scenario.params"
  cp "$raw/$scenario.yaml" "$dest/"
done

for f in "$dest"/*; do
  if (( $(stat -c %s "$f") > max_bytes )); then
    echo "$f is larger than 8 MB" >&2
    exit 1
  fi
done
