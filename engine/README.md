# Flight Insights engine

The Python analysis engine, run by the desktop shell as a sidecar and usable on its own as the `fi-engine` CLI. See `docs/SPEC.md`, section "System architecture".

```sh
uv sync
uv run fi-engine --version
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

The engine opens no network port and makes no network requests.
