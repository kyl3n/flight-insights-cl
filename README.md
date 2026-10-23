# Flight Insights

A desktop app, for Windows and Linux first, that analyzes ArduPilot `.bin` logs, ground-station tlogs and parameter files. It gives deep insight into flight performance, finds parameter errors and settings that are wrong for the aircraft type, and recommends tuning changes with the reasoning behind them.

- **Design spec:** [`docs/SPEC.md`](docs/SPEC.md)
- **Screen mock-ups:** [`docs/mockups/`](docs/mockups/)
- **Instructions for Claude Code:** [`CLAUDE.md`](CLAUDE.md)

Status: Phase 0 (foundations) in progress.

## Repository layout

| Path | What it holds |
| --- | --- |
| `engine/` | Python 3.12+ analysis engine, run as the shell's sidecar and as the `fi-engine` CLI |
| `shell/` | Tauri v2 desktop shell (Rust, in `shell/src-tauri/`) |
| `ui/` | Svelte 5 + TypeScript user interface |
| `tools/` | Repository checks; fixture scrubber and SITL scenarios (`tools/fixtures/`) |
| `tests/fixtures/` | Scrubbed SITL logs, tlogs and parameter files: the only place flight data may be committed (see its README) |

## Development

Requires [uv](https://docs.astral.sh/uv/), Node 22 with pnpm, and Rust stable. On Linux, Tauri also needs `libwebkit2gtk-4.1-dev`, `libayatana-appindicator3-dev` and `librsvg2-dev`.

```sh
pnpm install                      # UI and Tauri CLI
pnpm tauri dev                    # run the desktop app against the UI dev server

cd engine && uv sync && uv run pytest
cd ui && pnpm check && pnpm test
cd shell/src-tauri && cargo clippy --all-targets && cargo test

python3 tools/check_repo_files.py # no flight data or keys outside tests/fixtures/
```
