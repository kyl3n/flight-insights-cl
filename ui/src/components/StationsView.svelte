<script lang="ts">
  import { untrack } from "svelte";
  import { app, t } from "../lib/app.svelte";
  import type { MessageKey } from "../lib/i18n";
  import { isDetected, text, type SetupField } from "../lib/setup";
  import { detectThisComputer, engineCall, inDesktopApp } from "../lib/shell";
  import {
    CHANGE_REASONS,
    entriesChanged,
    entriesOf,
    needsReason,
    parsePart,
    parseRows,
    reasonKey,
    rowOf,
    specOf,
    type ChangeReason,
    type Row,
    type StationSchema,
    type StationView,
  } from "../lib/stations";

  const ready = $derived(app.shell.engine === "ready");
  let schema: StationSchema | null = $state(null);
  let stations: StationView[] = $state([]);
  let selectedId: string | null = $state(null);
  let newName = $state("");
  let message = $state("");
  let busy = $state(false);

  // What the person typed: one record per single part, a list of rows per list part.
  let drafts: Record<string, Row> = $state({});
  let rows: Record<string, Row[]> = $state({});
  let errors: Record<string, string> = $state({});
  let reasonKind: ChangeReason = $state("upgrade");
  let reasonText = $state("");

  const station = $derived(stations.find((s) => s.id === selectedId) ?? stations[0] ?? null);

  function reset(view: StationView | null) {
    drafts = {};
    errors = {};
    reasonText = "";
    rows = {};
    if (!view || !schema) return;
    for (const step of schema.steps.filter((s) => s.repeat)) {
      rows[step.id] = entriesOf(view.current.profile, step.id).map((e) => rowOf(step.fields, e));
    }
  }

  function replace(view: StationView) {
    stations = stations.some((s) => s.id === view.id)
      ? stations.map((s) => (s.id === view.id ? view : s))
      : [...stations, view];
    selectedId = view.id;
    reset(view);
  }

  async function load() {
    try {
      schema ??= await engineCall<StationSchema>("station.schema");
      stations = await engineCall<StationView[]>("station.list");
      reset(station);
    } catch (e) {
      message = t("setup.error.engine", { message: String(e) });
    }
  }

  $effect(() => {
    if (ready) untrack(load); // load reads state it sets: run it untracked
  });

  function select(id: string) {
    selectedId = id;
    reset(stations.find((s) => s.id === id) ?? null);
    message = "";
  }

  async function create() {
    try {
      replace(await engineCall<StationView>("station.create", { name: newName }));
      newName = "";
    } catch (e) {
      message = t("setup.error.engine", { message: String(e) });
    }
  }

  async function detect() {
    if (!station) return;
    busy = true;
    try {
      replace(await detectThisComputer<StationView>(station.id, station.version));
      message = t("stations.detect.done");
    } catch (e) {
      message = t("setup.error.engine", { message: String(e) });
    } finally {
      busy = false;
    }
  }

  async function save() {
    if (!station || !schema) return;
    const profile: Record<string, unknown> = {};
    const found: Record<string, string> = {};
    for (const step of schema.steps) {
      if (step.repeat) {
        const parsed = parseRows(step.fields, rows[step.id] ?? []);
        for (const [k, e] of Object.entries(parsed.errors)) found[`${step.id}/${k}`] = t(e);
        if (entriesChanged(parsed.entries, entriesOf(station.current.profile, step.id))) {
          profile[step.id] = parsed.entries;
        }
      } else {
        const parsed = parsePart(step.fields, drafts[step.id] ?? {});
        for (const [k, e] of Object.entries(parsed.errors)) found[`${step.id}/${k}`] = t(e);
        if (Object.keys(parsed.values).length) profile[step.id] = parsed.values;
      }
    }
    errors = found;
    if (Object.keys(found).length || !Object.keys(profile).length) return;
    busy = true;
    try {
      const change = needsReason(station) ? { reason_kind: reasonKind, reason: reasonText } : {};
      replace(
        await engineCall<StationView>("station.save", {
          id: station.id,
          version: station.version,
          profile,
          ...change,
        }),
      );
      message = t("setup.saved");
    } catch (e) {
      message = t("setup.error.engine", { message: String(e) });
    } finally {
      busy = false;
    }
  }

  function value(part: string, f: SetupField): string | boolean {
    const draft = drafts[part]?.[f.id];
    if (draft !== undefined) return draft;
    return rowOf([f], specOf(station?.current.profile ?? {}, part))[f.id] ?? "";
  }

  function edit(part: string, field: string, v: string | boolean) {
    drafts[part] = { ...(drafts[part] ?? {}), [field]: v };
  }

  function editRow(part: string, i: number, field: string, v: string | boolean) {
    const list = rows[part] ?? [];
    list[i] = { ...(list[i] ?? {}), [field]: v };
    rows[part] = list;
  }

  function reasonLabel(kind: string | null): string {
    const key: MessageKey | null = reasonKey(kind);
    return key ? t(key) : (kind ?? "");
  }
</script>

{#snippet box(f: SetupField, v: string | boolean, error: string | undefined, onchange: (v: string | boolean) => void)}
  {#if f.type === "boolean"}
    <label class="check">
      <input type="checkbox" checked={v === true} onchange={(e) => onchange(e.currentTarget.checked)} />
      {text(f.label, app.locale)}
    </label>
  {:else}
    <label>
      <span>
        {text(f.label, app.locale)}
        {#if f.required}<span class="req">· {t("setup.required")}</span>{/if}
      </span>
      {#if f.type === "choice"}
        <select value={v} onchange={(e) => onchange(e.currentTarget.value)} aria-invalid={error !== undefined}>
          <option value="">—</option>
          {#each f.choices ?? [] as c (c)}<option value={c}>{c}</option>{/each}
        </select>
      {:else}
        <span class="input">
          <input
            type="text"
            inputmode={f.type === "number" || f.type === "integer" ? "decimal" : "text"}
            value={v}
            oninput={(e) => onchange(e.currentTarget.value)}
            aria-invalid={error !== undefined}
          />
          {#if f.unit}<span class="unit">{f.unit}</span>{/if}
        </span>
      {/if}
    </label>
  {/if}
  {#if error}<span class="error">{error}</span>{/if}
{/snippet}

<main class="stations">
  {#if !inDesktopApp()}
    <p class="faint">{t("stations.unavailable")}</p>
  {:else}
    <nav aria-labelledby="stations-title">
      <h1 id="stations-title">{t("stations.title")}</h1>
      {#if stations.length}
        <ul>
          {#each stations as s (s.id)}
            <li>
              <button type="button" class:current={s.id === station?.id} onclick={() => select(s.id)}>
                <span>{s.name}</span>
                <span class="sub">
                  {t("stations.version", { number: s.current.number })}
                  {#if s.this_computer}· {t("stations.thisComputer")}{/if}
                </span>
              </button>
            </li>
          {/each}
        </ul>
      {:else}
        <p class="faint">{t("stations.empty")}</p>
      {/if}
      <label>
        {t("stations.new.name")}
        <span class="row">
          <input type="text" bind:value={newName} />
          <button type="button" onclick={create} disabled={!newName.trim() || !ready}>
            {t("stations.new.create")}
          </button>
        </span>
      </label>
    </nav>

    {#if station && schema}
      <section class="profile" aria-labelledby="station-name">
        <div class="head">
          <div>
            <h2 id="station-name">{station.name}</h2>
            <p class="faint">
              {t("stations.version", { number: station.current.number })} ·
              {needsReason(station)
                ? t("stations.inUse", { number: station.current.number + 1 })
                : t("stations.settingUp")}
            </p>
          </div>
          <button type="button" onclick={detect} disabled={busy}>{t("stations.detect")}</button>
        </div>

        {#each schema.steps as step (step.id)}
          <fieldset>
            <legend>{text(step.title, app.locale)}</legend>
            <p class="faint">{text(step.about, app.locale)}</p>
            {#if step.repeat}
              {#each rows[step.id] ?? [] as row, i (i)}
                <div class="entry">
                  <div class="fields">
                    {#each step.fields as f (f.id)}
                      <div class="field">
                        {@render box(f, row[f.id] ?? "", errors[`${step.id}/${i}/${f.id}`], (v) =>
                          editRow(step.id, i, f.id, v),
                        )}
                      </div>
                    {/each}
                  </div>
                  <button
                    type="button"
                    class="link"
                    onclick={() => (rows[step.id] = (rows[step.id] ?? []).filter((_, j) => j !== i))}
                  >
                    {t("stations.removeEntry")}
                  </button>
                </div>
              {:else}
                <p class="faint">{t("stations.noEntries")}</p>
              {/each}
              <button type="button" onclick={() => (rows[step.id] = [...(rows[step.id] ?? []), {}])}>
                {t("stations.addEntry")}
              </button>
            {:else}
              {@const spec = specOf(station.current.profile, step.id)}
              <div class="fields">
                {#each step.fields as f (f.id)}
                  <div class="field">
                    {@render box(f, value(step.id, f), errors[`${step.id}/${f.id}`], (v) => edit(step.id, f.id, v))}
                    {#if isDetected(spec[f.id]) && drafts[step.id]?.[f.id] === undefined}
                      <span class="detected">
                        {t("setup.detected", { source: spec[f.id]?.source.replace(/^detected:? ?/, "") || "—" })}
                      </span>
                    {/if}
                    {#if f.why}<span class="why">{text(f.why, app.locale)}</span>{/if}
                  </div>
                {/each}
              </div>
            {/if}
          </fieldset>
        {/each}

        <div class="save">
          {#if needsReason(station)}
            <label>
              {t("stations.reason.label")}
              <select bind:value={reasonKind}>
                {#each CHANGE_REASONS as r (r)}<option value={r}>{reasonLabel(r)}</option>{/each}
              </select>
            </label>
            <label class="grow">
              {t("stations.reason.text")}
              <input type="text" bind:value={reasonText} />
            </label>
          {/if}
          <span class="status" aria-live="polite">{busy ? t("setup.saving") : message}</span>
          <button type="button" class="primary" onclick={save} disabled={busy}>{t("stations.save")}</button>
        </div>
      </section>

      <aside aria-labelledby="history-title">
        <h2 id="history-title">{t("stations.history")}</h2>
        <ol>
          {#each [...station.history].reverse() as v (v.id)}
            <li>
              <span class="mono">{t("stations.version", { number: v.number })}</span>
              <span class="faint">{v.effective_date ?? ""} · {reasonLabel(v.reason_kind)}</span>
              {#if v.reason}<span>{v.reason}</span>{/if}
            </li>
          {/each}
        </ol>
      </aside>
    {:else if message}
      <p class="status">{message}</p>
    {/if}
  {/if}
</main>

<style>
  .stations {
    display: flex;
    flex-wrap: wrap;
    gap: 24px;
    padding: 24px;
    align-items: flex-start;
  }
  nav {
    flex: 0 0 240px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  h1 {
    margin: 0;
    font-size: 22px;
  }
  h2 {
    margin: 0;
    font-size: 17px;
  }
  ul,
  ol {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  nav li button {
    width: 100%;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    padding: 8px 10px;
    border: 0;
    border-radius: 8px;
    background: transparent;
    color: var(--text-muted);
    text-align: left;
  }
  nav li button.current {
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    color: var(--text);
  }
  .sub {
    font-size: 12px;
    color: var(--text-faint);
  }
  .profile {
    flex: 999 1 480px;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 12px;
    flex-wrap: wrap;
  }
  fieldset {
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 14px 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
  }
  legend {
    font-weight: 600;
    padding: 0 6px;
  }
  .fields {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 14px;
  }
  .entry {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border-soft);
  }
  .field,
  label {
    display: flex;
    flex-direction: column;
    gap: 5px;
    font-size: 13px;
  }
  label.check {
    flex-direction: row;
    align-items: center;
    gap: 8px;
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
    flex: 1 1 auto;
  }
  [aria-invalid="true"] {
    border-color: var(--out);
  }
  .input,
  .row {
    display: flex;
    gap: 6px;
    align-items: center;
  }
  .unit,
  .why,
  .faint {
    font-size: 12px;
    color: var(--text-faint);
  }
  .req {
    color: var(--caution);
    font-size: 12px;
  }
  .detected {
    font-size: 12px;
    color: var(--link);
  }
  .error {
    font-size: 12px;
    color: var(--out);
  }
  button {
    padding: 8px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface);
    color: var(--text);
    font-size: 13px;
    cursor: pointer;
    align-self: flex-start;
  }
  button.link {
    border: 0;
    background: transparent;
    color: var(--link);
    padding: 0;
  }
  .primary {
    border: 0;
    background: var(--accent);
    color: var(--accent-text);
    font-weight: 600;
  }
  button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  .save {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 12px;
  }
  .grow {
    flex: 1 1 200px;
  }
  .status {
    flex: 1 1 auto;
    font-size: 12px;
    color: var(--text-faint);
  }
  aside {
    flex: 1 1 260px;
    max-width: 320px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    font-size: 13px;
  }
  aside li {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding-top: 8px;
    border-top: 1px solid var(--border-soft);
  }
  p {
    margin: 0;
  }
</style>
