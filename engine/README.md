# Flight Insights engine

The Python analysis engine, run by the desktop shell as a sidecar and usable on its own as the `fi-engine` CLI. See `docs/SPEC.md`, section "System architecture".

```sh
uv sync
uv run fi-engine --version
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

The engine opens no network port and makes no network requests.

## The Phase 0 gate

The engine must decode every `.bin` log exactly as pymavlink does: the same messages, in the same order, with the same values. CI runs this on the scrubbed fixtures. To run it on real logs, which never leave your machine:

```sh
uv run fi-engine gate --corpus "$FI_CORPUS_DIR" --report gate-report.json
```

The terminal lists each log by file name. The JSON report names logs only by a hash of their content, and holds message types, column names, counts and positions, never values, so you can share it. Exit status: 0 if every log matches, 1 otherwise, 2 if no logs were found.

Known differences from pymavlink 2.4.50, where pymavlink is wrong, are listed in `src/flight_insights/dataflash/index.py`; a log hitting them shows up as `mismatch` or `reference_failed`.
