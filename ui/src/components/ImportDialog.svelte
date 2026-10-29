<script lang="ts">
  import { onMount } from "svelte";
  import { open as pickFiles } from "@tauri-apps/plugin-dialog";
  import { app, t } from "../lib/app.svelte";
  import type { MessageKey } from "../lib/i18n";
  import {
    PARTS,
    badWeights,
    declined,
    decisions,
    describeChange,
    detectedApplies,
    fileStatus,
    initialChoices,
    matchKey,
    partKey,
    resultCounts,
    undecided,
    whenText,
    withStations,
    type ChangeKind,
    type Choice,
    type CommitResult,
    type LogChoices,
    type Part,
    type ScanResult,
    type ScannedFile,
  } from "../lib/imports";
  import type { AirframeView, PlatformView } from "../lib/setup";
  import { teamsOf } from "../lib/fleet";
  import { engineCall, inDesktopApp, onProgress, pendingImports, type PendingFile } from "../lib/shell";
  import type { StationView } from "../lib/stations";

  type Phase = "pick" | "scanning" | "review" | "committing" | "done";
  let phase: Phase = $state("pick");
  let progress = $state("");
  let error = $state("");
  let scanned = $state<ScanResult | null>(null);
  let choices: Record<string, LogChoices> = $state({});
  let results: CommitResult[] = $state([]);
  let airframes: AirframeView[] = $state([]);
  let platforms: PlatformView[] = $state([]);
  let stations: StationView[] = $state([]);
  let station: string = $state("");
  let heldFlights: { id: string; log?: string; reason?: string; candidates?: { airframe_id: string; name: string }[] }[] =
    $state([]);
  let heldPick: Record<string, string> = $state({});
  // The watched files as they were when the scan started: an import-pending event during
  // review must not change which station or set-aside list the scanned files are recorded with.
  let scanPending: PendingFile[] = [];

  const files = $derived(scanned?.files ?? []);
  const waiting = $derived(undecided(files, choices));
  const unreadable = $derived(badWeights(files, choices));
  const newFlights = $derived(
    files
      .filter((f) => f.kind === "dataflash" && !["skip", "undecided"].includes(choices[f.id]?.choice.kind ?? ""))
      .reduce((n, f) => n + f.flights.filter((x) => !x.known).length, 0),
  );
  const buildChanges = $derived(
    files.filter((f) => {
      const c = choices[f.id];
      if (!c || (c.choice.kind !== "airframe" && c.choice.kind !== "new")) return false;
      return c.other.length > 0 || (detectedApplies(f, c.choice) && f.proposal.changes.some((x) => c.confirmed[x.part]));
    }).length,
  );
  const reasons: [ChangeKind, MessageKey][] = [
    ["upgrade", "stations.reason.upgrade"],
    ["failure", "stations.reason.failure"],
    ["test", "stations.reason.test"],
  ];

  function close() {
    app.importOpen = false;
  }

  async function loadLists() {
    try {
      [airframes, platforms, stations] = await Promise.all([
        engineCall<AirframeView[]>("airframe.list"),
        engineCall<PlatformView[]>("platform.list"),
        engineCall<StationView[]>("station.list"),
      ]);
      station = stations.find((s) => s.this_computer)?.current.id ?? "";
      heldFlights = await engineCall("import.held");
    } catch (e) {
      error = t("setup.error.engine", { message: String(e) });
    }
  }

  onMount(() => {
    let stop = () => {};
    if (inDesktopApp()) {
      loadLists();
      onProgress((p) => (progress = p.message)).then((u) => (stop = u));
    }
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && phase !== "committing" && close();
    window.addEventListener("keydown", onKey);
    return () => {
      stop();
      window.removeEventListener("keydown", onKey);
    };
  });

  async function choose() {
    error = "";
    const picked = await pickFiles({
      multiple: true,
      filters: [{ name: t("import.pick.hint"), extensions: ["bin", "BIN", "tlog", "param", "params"] }],
    });
    const paths = Array.isArray(picked) ? picked : picked ? [picked] : [];
    if (paths.length) await scanPaths(paths);
  }

  async function scanPaths(paths: string[]) {
    error = "";
    phase = "scanning";
    scanPending = $state.snapshot(app.pending);
    try {
      scanned = await engineCall<ScanResult>("import.scan", { paths });
      choices = Object.fromEntries(scanned.files.map((f) => [f.id, initialChoices(f)]));
      phase = "review";
    } catch (e) {
      error = t("setup.error.engine", { message: String(e) });
      phase = "pick";
    }
  }

  async function go() {
    if (!scanned || waiting.length) return;
    phase = "committing";
    try {
      results = await engineCall<CommitResult[]>("import.commit", {
        batch: scanned.batch,
        decisions: withStations(decisions(files, choices, partKey, station || null), files, scanPending),
      });
      const setAside = declined(files, choices, scanPending);
      if (setAside.length) await engineCall("import.ignore", { paths: setAside });
      app.pending = await pendingImports(true);
      phase = "done";
    } catch (e) {
      error = t("setup.error.engine", { message: String(e) });
      phase = "review";
    }
  }

  async function assign(flightId: string) {
    const pick = heldPick[flightId];
    if (!pick) return;
    try {
      await engineCall("import.resolve", { flights: [flightId], airframe_id: pick });
      heldFlights = await engineCall("import.held");
    } catch (e) {
      error = t("setup.error.engine", { message: String(e) });
    }
  }

  function selectValue(c: Choice): string {
    switch (c.kind) {
      case "airframe":
        return c.airframe_id;
      case "new":
        return "new";
      default:
        return c.kind;
    }
  }

  function setChoice(f: ScannedFile, value: string) {
    const c = choices[f.id]!;
    if (value === "new") {
      const platform = airframes.find((a) => a.id === f.proposal.airframe_id)?.platform.id ?? platforms[0]?.id ?? "";
      const team = airframes.find((a) => a.id === f.proposal.airframe_id)?.team ?? "";
      c.choice = { kind: "new", platform_id: platform, name: "", team };
    } else if (value === "hold" || value === "skip" || value === "import" || value === "undecided") {
      c.choice = { kind: value };
    } else {
      c.choice = { kind: "airframe", airframe_id: value };
    }
  }

  function decide(f: ScannedFile, how: "replaced" | "new" | "later") {
    if (how === "replaced" && f.proposal.airframe_id) setChoice(f, f.proposal.airframe_id);
    else setChoice(f, how === "new" ? "new" : "hold");
  }

  function airframeName(c: Choice, f: ScannedFile): string {
    if (c.kind === "airframe") return airframes.find((a) => a.id === c.airframe_id)?.name ?? f.proposal.airframe_name ?? "";
    if (c.kind === "new") return c.name || t("import.airframe.new");
    return "";
  }

  function toggleOther(c: LogChoices, p: Part, on: boolean) {
    c.other = on ? [...c.other, p] : c.other.filter((x) => x !== p);
  }

  const counts = $derived(resultCounts(results));
</script>

<div class="backdrop" role="presentation" onclick={(e) => e.target === e.currentTarget && phase !== "committing" && close()}>
  <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="import-title">
    <header>
      <div>
        <h1 id="import-title">{t("import.title")}</h1>
        {#if files.length}<p class="faint">{t("import.from", { count: files.length })}</p>{/if}
      </div>
      <button type="button" class="close" aria-label={t("import.cancel")} onclick={close} disabled={phase === "committing"}>
        ✕
      </button>
    </header>

    {#if !inDesktopApp()}
      <p class="faint">{t("import.unavailable")}</p>
    {:else if phase === "pick" || phase === "scanning"}
      {#if app.pending.length}
        <section class="watched">
          <p>{t("import.watched", { count: app.pending.length })}</p>
          <ul>
            {#each app.pending.slice(0, 6) as p (p.path)}
              <li><span class="mono">{p.name}</span> <span class="faint">{p.station}</span></li>
            {/each}
          </ul>
          <button type="button" class="primary" onclick={() => scanPaths(app.pending.map((p) => p.path))} disabled={phase === "scanning"}>
            {t("import.watched.review")}
          </button>
        </section>
      {/if}
      <section class="pick">
        <button type="button" class="primary" onclick={choose} disabled={phase === "scanning"}>{t("import.pick")}</button>
        <p class="faint">{phase === "scanning" ? t("import.scanning", { name: progress || "…" }) : t("import.pick.hint")}</p>
      </section>
      {#if heldFlights.length}
        <section aria-labelledby="held-title">
          <h2 id="held-title">{t("import.held.title")}</h2>
          <ul class="held">
            {#each heldFlights as h (h.id)}
              <li>
                <span class="mono">{h.log ?? h.id}</span>
                <span class="faint">{h.reason === "decision" ? t("import.decide.question") : t("import.match.none")}</span>
                <select bind:value={heldPick[h.id]} aria-label={t("import.airframe.for", { file: h.log ?? h.id })}>
                  <option value="">—</option>
                  {#each airframes as a (a.id)}<option value={a.id}>{a.name}</option>{/each}
                </select>
                <button type="button" onclick={() => assign(h.id)} disabled={!heldPick[h.id]}>{t("import.held.assign")}</button>
              </li>
            {/each}
          </ul>
        </section>
      {/if}
    {:else if phase === "done"}
      <section>
        <p>
          {t("import.result", {
            imported: counts.imported, held: counts.held, linked: counts.linked,
            skipped: counts.skipped + counts.duplicate, errors: counts.error,
          })}
        </p>
        <ul class="results">
          {#each results as r (r.log)}
            {@const f = files.find((x) => x.id === r.log)}
            <li><span class="mono">{f?.name}</span> <span class="faint">{r.status}{r.message ? ` · ${r.message}` : ""}</span></li>
          {/each}
        </ul>
      </section>
    {:else}
      <div class="table" role="table" aria-label={t("import.title")}>
        <div class="row head" role="row">
          <span role="columnheader">{t("import.col.log")}</span>
          <span role="columnheader">{t("import.col.flight")}</span>
          <span role="columnheader">{t("import.col.airframe")}</span>
          <span role="columnheader">{t("import.col.match")}</span>
          <span role="columnheader">{t("import.col.changes")}</span>
        </div>
        {#each files as f (f.id)}
          {@const c = choices[f.id]!}
          {@const st = fileStatus(f, c.choice)}
          <div class="row" role="row" class:ask={st.glyph === "▲"}>
            <span role="cell"><span class="mono">{f.name}</span><span class="faint">{whenText(f, app.locale, (count) => t("import.minutes", { count }))}</span></span>
            <span role="cell" class="mono">{f.flights.length || "—"}</span>
            <span role="cell">
              <select
                value={selectValue(c.choice)}
                onchange={(e) => setChoice(f, e.currentTarget.value)}
                aria-label={t("import.airframe.for", { file: f.name })}
                disabled={f.proposal.status === "error"}
              >
                {#if f.kind === "dataflash"}
                  {#if c.choice.kind === "undecided"}<option value="undecided">—</option>{/if}
                  {#each airframes as a (a.id)}<option value={a.id}>{a.name}</option>{/each}
                  <option value="new">{t("import.airframe.new")}</option>
                  <option value="hold">{t("import.airframe.hold")}</option>
                {:else}
                  <option value="import">{t("import.airframe.store")}</option>
                {/if}
                <option value="skip">{t("import.airframe.skip")}</option>
              </select>
            </span>
            <span role="cell" class="faint">{t(matchKey(f.proposal))}</span>
            <span role="cell" class="status">
              <span aria-hidden="true" class="glyph g{st.glyph === "●" ? "ok" : st.glyph === "▲" ? "ask" : st.glyph === "■" ? "out" : "off"}">{st.glyph}</span>
              {detectedApplies(f, c.choice) && f.proposal.changes.length
                ? t("import.changes.count", { count: f.proposal.changes.length })
                : t(st.key)}
            </span>
          </div>
        {/each}
      </div>

      {#each files as f (f.id)}
        {@const c = choices[f.id]!}
        {#if f.proposal.status === "decision"}
          <section class="decide" aria-labelledby="decide-{f.id}">
            <h2 id="decide-{f.id}">▲ {t("import.decide.title", { file: f.name })}</h2>
            <p class="q">{t("import.decide.question")}</p>
            <div class="cards" role="radiogroup" aria-label={t("import.decide.question")}>
              {#each [
                ["replaced", t("import.decide.replaced", { airframe: f.proposal.airframe_name ?? "" }), t("import.decide.replacedSub")],
                ["new", t("import.decide.new"), t("import.decide.newSub", { platform: airframes.find((a) => a.id === f.proposal.airframe_id)?.platform.name ?? "" })],
                ["later", t("import.decide.later"), t("import.decide.laterSub")],
              ] as [how, label, sub] (how)}
                {@const on = (how === "replaced" && c.choice.kind === "airframe") || (how === "new" && c.choice.kind === "new") || (how === "later" && c.choice.kind === "hold")}
                <button type="button" role="radio" aria-checked={on} class:on onclick={() => decide(f, how as "replaced" | "new" | "later")}>
                  <span>{label}</span><span class="faint">{sub}</span>
                </button>
              {/each}
            </div>
          </section>
        {/if}
        {#if c.choice.kind === "new"}
          {@const choice = c.choice}
          <section class="newframe">
            <label>
              {t("import.new.platform")}
              <select bind:value={choice.platform_id}>
                {#each platforms as p (p.id)}<option value={p.id}>{p.name}</option>{/each}
              </select>
            </label>
            <label class="grow">
              {t("import.new.name")}
              <input type="text" bind:value={choice.name} aria-invalid={!choice.name.trim()} />
            </label>
            <label>
              {t("import.new.team")}
              <input type="text" list="import-teams" maxlength="60" placeholder={t("team.none")} bind:value={choice.team} />
            </label>
          </section>
        {/if}
        {#if f.kind === "dataflash" && (c.choice.kind === "airframe" || c.choice.kind === "new")}
          <section class="changes" aria-labelledby="changes-{f.id}">
            <h2 id="changes-{f.id}">{t("import.changes.title", { airframe: airframeName(c.choice, f) })} <span class="faint mono">{f.name}</span></h2>
            {#if detectedApplies(f, c.choice)}
              {#each f.proposal.changes as x (x.part)}
                <label class="check">
                  <input type="checkbox" bind:checked={c.confirmed[x.part]} />
                  <span><span class="mono">{x.part}</span> {describeChange(x)} <span class="faint">({x.evidence})</span></span>
                </label>
              {/each}
            {/if}
            <p>{t("import.changes.other")}</p>
            <div class="parts">
              {#each PARTS as p (p)}
                <label class="chip">
                  <input type="checkbox" checked={c.other.includes(p)} onchange={(e) => toggleOther(c, p, e.currentTarget.checked)} />
                  {t(`import.part.${p}` as MessageKey)}
                </label>
              {/each}
              <label class="chip">
                <input type="checkbox" checked={c.other.length === 0} onchange={(e) => e.currentTarget.checked && (c.other = [])} />
                {t("import.changes.nothing")}
              </label>
            </div>
            {#if c.other.length}
              <label class="inline">
                {t("import.changes.why")}
                <select bind:value={c.otherReason}>
                  {#each reasons as [k, label] (k)}<option value={k}>{t(label)}</option>{/each}
                </select>
              </label>
            {/if}
          </section>
        {/if}
      {/each}

      <section class="picks" aria-labelledby="picks-title">
        <h2 id="picks-title">{t("import.picks.title")}</h2>
        <label class="inline">
          {t("import.picks.station")}
          <select bind:value={station}>
            <option value="">{t("import.picks.noStation")}</option>
            {#each stations as s (s.id)}
              <option value={s.current.id}>{s.name} · {t("stations.version", { number: s.current.number })}</option>
            {/each}
          </select>
        </label>
        {#each files.filter((x) => x.kind === "dataflash" && choices[x.id]?.choice.kind !== "skip") as f (f.id)}
          {@const c = choices[f.id]!}
          <div class="pickrow">
            <span class="mono">{f.name}</span>
            <label>{t("import.picks.packs")}<input type="text" bind:value={c.packs} placeholder={t("import.picks.packsHint")} /></label>
            <label>{t("import.picks.payload")}<input type="text" bind:value={c.payload} /></label>
            <label>
              {t("import.picks.weight")}
              <input type="text" inputmode="decimal" bind:value={c.takeoffKg} placeholder={t("import.picks.weightHint")}
                aria-invalid={unreadable.includes(f)} />
            </label>
          </div>
        {/each}
      </section>
    {/if}

    {#if unreadable.length}<p class="error" role="alert">{t("import.weightInvalid", { max: 1000 })}</p>{/if}
    {#if error}<p class="error" role="alert">{error}</p>{/if}

    <footer>
      {#if phase === "review" || phase === "committing"}
        <span class="foot" class:waiting={waiting.length > 0}>
          {waiting.length
            ? t("import.foot.decide", { file: waiting[0]?.name ?? "" })
            : t("import.foot.ready", { flights: newFlights, changes: buildChanges })}
        </span>
        <button type="button" onclick={close} disabled={phase === "committing"}>{t("import.cancel")}</button>
        <button type="button" class="primary" onclick={go} disabled={waiting.length > 0 || unreadable.length > 0 || phase === "committing"}>
          {phase === "committing" ? t("setup.saving") : t("import.go", { count: files.length })}
        </button>
      {:else}
        <span class="foot"></span>
        <button type="button" class:primary={phase === "done"} onclick={close}>
          {phase === "done" ? t("import.done") : t("import.cancel")}
        </button>
      {/if}
    </footer>
  </div>
</div>

<datalist id="import-teams">
  {#each teamsOf(airframes).teams as team (team)}<option value={team}></option>{/each}
</datalist>

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    background: rgba(6, 10, 14, 0.6);
    display: grid;
    place-items: start center;
    padding: 40px 16px;
    overflow-y: auto;
    z-index: 10;
  }
  .dialog {
    width: min(980px, 100%);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 20px 22px;
    display: flex;
    flex-direction: column;
    gap: 16px;
    box-sizing: border-box;
  }
  header,
  footer {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  header {
    justify-content: space-between;
  }
  h1 {
    margin: 0;
    font-size: 19px;
  }
  h2 {
    margin: 0 0 8px;
    font-size: 14px;
  }
  p {
    margin: 0;
  }
  .faint {
    font-size: 12px;
    color: var(--text-faint);
  }
  .watched {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 14px 16px;
    border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--border));
    border-radius: 10px;
    font-size: 13px;
  }
  .watched ul {
    margin: 0;
    padding-left: 18px;
  }
  .watched button {
    align-self: flex-start;
  }
  .pick {
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
    padding: 18px;
    border: 1px dashed var(--border);
    border-radius: 10px;
  }
  .table {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
  }
  .row {
    display: grid;
    grid-template-columns: minmax(150px, 1.4fr) 60px minmax(140px, 1.1fr) minmax(130px, 1.2fr) minmax(130px, 1fr);
    gap: 10px;
    align-items: center;
    padding: 9px 12px;
    border-top: 1px solid var(--border-soft);
    font-size: 13px;
  }
  .row.head {
    border-top: 0;
    font-size: 12px;
    color: var(--text-faint);
    background: var(--surface-2);
  }
  .row.ask {
    background: color-mix(in srgb, var(--caution) 6%, transparent);
  }
  .row span[role="cell"]:first-child {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .status {
    display: flex;
    gap: 6px;
    align-items: center;
  }
  .glyph.gok {
    color: var(--ok);
  }
  .glyph.gask {
    color: var(--caution);
  }
  .glyph.gout {
    color: var(--out);
  }
  .glyph.goff {
    color: var(--text-faint);
  }
  section.decide,
  section.changes,
  section.picks,
  section.newframe {
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  section.decide {
    border-color: color-mix(in srgb, var(--caution) 55%, var(--border));
  }
  section.decide h2 {
    color: var(--caution);
  }
  .q {
    font-weight: 600;
  }
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 10px;
  }
  .cards button {
    display: flex;
    flex-direction: column;
    gap: 4px;
    text-align: left;
    align-items: flex-start;
  }
  .cards button.on {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 14%, transparent);
  }
  section.newframe {
    flex-direction: row;
    flex-wrap: wrap;
    align-items: flex-end;
  }
  .grow {
    flex: 1 1 220px;
  }
  .parts {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .chip {
    flex-direction: row;
    align-items: center;
    gap: 6px;
    padding: 5px 10px;
    border: 1px solid var(--border);
    border-radius: 999px;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 5px;
    font-size: 13px;
  }
  label.check {
    flex-direction: row;
    align-items: flex-start;
    gap: 8px;
  }
  label.inline {
    flex-direction: row;
    align-items: center;
    gap: 8px;
  }
  .pickrow {
    display: grid;
    grid-template-columns: minmax(140px, 1fr) minmax(160px, 1.2fr) minmax(140px, 1fr);
    gap: 10px;
    align-items: end;
  }
  .held,
  .results {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
    font-size: 13px;
  }
  .held li {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
  }
  input[type="text"],
  select {
    padding: 7px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-2);
    color: var(--text);
    font-size: 13px;
    min-width: 0;
  }
  [aria-invalid="true"] {
    border-color: var(--out);
  }
  button {
    padding: 8px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface);
    color: var(--text);
    font-size: 13px;
    cursor: pointer;
  }
  .primary {
    border: 0;
    background: var(--accent);
    color: var(--accent-text);
    font-weight: 600;
  }
  button:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .close {
    border: 0;
    background: transparent;
    font-size: 16px;
  }
  footer {
    justify-content: flex-end;
    border-top: 1px solid var(--border-soft);
    padding-top: 14px;
  }
  .foot {
    flex: 1 1 auto;
    font-size: 13px;
    color: var(--ok);
  }
  .foot.waiting {
    color: var(--caution);
  }
  .error {
    color: var(--out);
    font-size: 13px;
  }
  @media (max-width: 720px) {
    .row {
      grid-template-columns: 1fr 1fr;
    }
    .row.head {
      display: none;
    }
    .pickrow {
      grid-template-columns: 1fr;
    }
  }
</style>
