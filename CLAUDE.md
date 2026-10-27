# Flight Insights

A desktop app, for Windows and Linux first, that analyzes ArduPilot flight data. It reads DataFlash `.bin` logs, ground-station tlogs and parameter files. It reports on the aircraft's performance and finds parameter errors and settings that are wrong for the aircraft type. It recommends tuning changes and explains the reasoning for each one.

## Source of truth

- `docs/SPEC.md` is the design spec. Before starting a task, read the sections it touches.
- `docs/mockups/` holds the screen mock-ups. Use them as the layout reference for the UI.
- If the code and the spec disagree, stop and ask. Don't quietly diverge from the spec.
- If the work changes the design, update `docs/SPEC.md` in the same pull request and call out the change in the PR description. The master copy of the spec lives in Claude Docs, and it is updated from these changes.

## How to work

- Build in the roadmap's phase order (`docs/SPEC.md`, section "Roadmap and milestones"). Phase 0 comes first. Each phase ends with a gate; don't start the next phase until its gate passes.
- Before a large change, plan it and get approval. Keep pull requests small and focused.
- Keep the stack the spec sets out:
  - a Tauri v2 shell in Rust;
  - a Python 3.12+ engine run as a sidecar, frozen with PyInstaller or Nuitka;
  - a TypeScript UI;
  - JSON-RPC 2.0 over stdio between the shell and the engine, with Apache Arrow IPC files for bulk arrays;
  - SQLite plus Parquet for storage.

  Ask before swapping any part of it.
- Accuracy comes before speed. Analyze full-rate samples and don't downsample to save time.
- The oldest supported firmware is ArduPilot 4.7.0.
- Decode logs from their own FMT, FMTU, UNIT and MULT messages. Never hard-code message layouts or units.
- The engine's decoded output must match what pymavlink decodes. That is the Phase 0 gate.
- Never write parameters to a flight controller automatically. Recommendations are exported for a person to review and load.
- Every analyzer ships with tests. Every finding carries the evidence it was based on.

## Location and privacy (non-negotiable)

- Nothing sent to any server may contain coordinates, or anything else that reveals where the aircraft flew. All location work runs on the laptop.
- The engine opens no network port and makes no network requests. Only the Rust shell talks to the network, and only to five endpoints: the update server, the Google Drive API, the Claude API, the Open-Meteo API and NOAA's Space Weather Prediction Center (SWPC).
- Every AI packet goes through the engine's location scrubber, which drops everything not on an allowlist. The Rust shell then checks the packet again with a separate implementation just before sending it. Any change to what a packet can contain needs a scrubber test.
- Weather is downloaded for whole regional tiles. Never request weather for an exact position.
- Everything written to the shared Google Drive is encrypted on the laptop before upload.
- Never commit real flight logs, tlogs or parameter files from real aircraft. Test fixtures must have positions removed or shifted, and they go in `tests/fixtures/` only. `.gitignore` blocks log and parameter files everywhere else.
- Never commit keys, tokens or recovery material. Secrets live in the OS keychain, never in plain files or logs.
