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

## Decode speed

The spec's target is 500 MB decoded in under 60 s on a 4-core laptop, with every sample decoded. CI checks it on a 500 MB raw-IMU log built from a fixture:

```sh
uv run python bench/make_large_log.py ../tests/fixtures/sitl/4.7.0/copter-quad-rawimu.bin /tmp/large.bin
uv run python bench/bench_decode.py /tmp/large.bin --max-seconds 60
```

On one core of the development machine: 14.5 million messages indexed in 9 s and decoded in 8 s, so 17 s in total, with a peak memory of about 2 GB.
