<script lang="ts">
  import { app, t } from "../lib/app.svelte";
  import {
    display,
    isDetected,
    parseInput,
    stepSummary,
    text,
    type PlatformView,
    type SetupField,
    type SetupSchema,
    type Spec,
  } from "../lib/setup";
  import { engineCall } from "../lib/shell";

  let { schema, platform: initial }: { schema: SetupSchema; platform: PlatformView } = $props();

  // The latest saved state; until the first save, the platform as opened.
  let saved: PlatformView | null = $state(null);
  const platform = $derived(saved ?? initial);
  let current = $state(0);
  let drafts: Record<string, string | boolean> = $state({});
  let errors: Record<string, string> = $state({});
  let saving = $state(false);
  let message = $state("");
  let prefillPath = $state("");
  let found: Spec | null = $state(null);

  const step = $derived(schema.steps[current]!);
  const percent = $derived(platform.completeness.percent);

  function value(f: SetupField): string | boolean {
    const draft = drafts[f.id];
    if (draft !== undefined) return draft;
    const a = platform.spec[f.id];
    return f.type === "boolean" ? a?.value === true : display(a);
  }

  async function save(): Promise<boolean> {
    const values: Record<string, unknown> = {};
    errors = {};
    for (const f of step.fields) {
      if (!(f.id in drafts)) continue;
      const parsed = parseInput(f, drafts[f.id] ?? "");
      if (!parsed.ok) errors[f.id] = t(parsed.error);
      else values[f.id] = parsed.value;
    }
    if (Object.keys(errors).length) return false;
    if (!Object.keys(values).length) return true;
    saving = true;
    try {
      saved = await engineCall<PlatformView>("platform.save", {
        id: platform.id,
        build_version: platform.reference_build.version,
        values,
      });
      drafts = {};
      message = t("setup.saved");
      return true;
    } catch (e) {
      message = t("setup.error.engine", { message: String(e) });
      return false;
    } finally {
      saving = false;
    }
  }

  async function go(index: number) {
    if (await save()) current = index;
  }

  async function finish() {
    if (await save()) app.setupPlatform = null;
  }

  async function readFile() {
    message = "";
    try {
      found = await engineCall<Spec>("platform.detect", { path: prefillPath });
      message = t("setup.prefill.found", { count: Object.keys(found).length });
    } catch (e) {
      found = null;
      message = t("setup.error.engine", { message: String(e) });
    }
  }

  async function applyFound() {
    if (!found) return;
    const values = Object.fromEntries(
      Object.entries(found)
        .filter(([id]) => id !== "vehicle" && id !== "name")
        .map(([id, a]) => [id, a.value]),
    );
    try {
      saved = await engineCall<PlatformView>("platform.save", {
        id: platform.id,
        build_version: platform.reference_build.version,
        values,
        detected: true,
        sources: Object.fromEntries(Object.entries(found).map(([id, a]) => [id, a.source])),
      });
      found = null;
      message = t("setup.saved");
    } catch (e) {
      message = t("setup.error.engine", { message: String(e) });
    }
  }
</script>

<main class="setup">
  <nav aria-label={t("setup.steps")}>
    <ol>
      {#each schema.steps as s, i (s.id)}
        {@const state = platform.completeness.steps[s.id]}
        {@const summary = stepSummary(s, state, platform.spec)}
        <li aria-current={i === current ? "step" : undefined} class:current={i === current}>
          <button type="button" onclick={() => go(i)}>
            <span aria-hidden="true" class="mark" class:done={state === "done"}>
              {state === "done" ? "✓" : i + 1}
            </span>
            <span class="names">
              <span>{text(s.title, app.locale)}</span>
              <span class="sub" class:left={summary.key === "setup.step.left"}>
                {t(summary.key, { count: summary.count ?? 0 })}
              </span>
            </span>
          </button>
        </li>
      {/each}
    </ol>
  </nav>

  <section class="form" aria-labelledby="step-title">
    <p class="platform mono">{platform.name}</p>
    <h1 id="step-title">{text(step.title, app.locale)}</h1>
    <p class="about">{text(step.about, app.locale)}</p>

    {#if current === 0}
      <div class="prefill">
        <label>
          {t("setup.prefill")}
          <span class="row">
            <input type="text" bind:value={prefillPath} placeholder={t("setup.prefill.path")} />
            <button type="button" onclick={readFile} disabled={!prefillPath.trim()}>
              {t("setup.prefill.read")}
            </button>
          </span>
        </label>
        {#if found}
          <ul class="found">
            {#each Object.entries(found) as [id, a] (id)}
              <li><span class="mono">{id}</span> = {String(a.value)} <span class="faint">({a.source})</span></li>
            {/each}
          </ul>
          <button type="button" onclick={applyFound}>{t("setup.prefill.apply")}</button>
        {/if}
      </div>
    {/if}

    {#if step.id === "review"}
      <p>{platform.completeness.gaps.length ? t("setup.gaps") : t("setup.noGaps")}</p>
    {/if}

    <div class="fields">
      {#each step.fields as f (f.id)}
        {@const answer = platform.spec[f.id]}
        <div class="field">
          {#if f.type === "boolean"}
            <label class="check">
              <input
                type="checkbox"
                checked={value(f) === true}
                onchange={(e) => (drafts[f.id] = e.currentTarget.checked)}
              />
              {text(f.label, app.locale)}
              {#if f.required}<span class="req">· {t("setup.required")}</span>{/if}
            </label>
          {:else}
            <label>
              <span>
                {text(f.label, app.locale)}
                {#if f.required}<span class="req">· {t("setup.required")}</span>{/if}
              </span>
              {#if f.type === "choice"}
                <select
                  value={value(f)}
                  onchange={(e) => (drafts[f.id] = e.currentTarget.value)}
                  aria-invalid={f.id in errors}
                  disabled={f.id === "vehicle"}
                >
                  <option value="">—</option>
                  {#each f.choices ?? [] as c (c)}<option value={c}>{c}</option>{/each}
                </select>
              {:else}
                <span class="input">
                  <input
                    type="text"
                    inputmode={f.type === "number" || f.type === "integer" ? "decimal" : "text"}
                    value={value(f)}
                    oninput={(e) => (drafts[f.id] = e.currentTarget.value)}
                    aria-invalid={f.id in errors}
                  />
                  {#if f.unit}<span class="unit">{f.unit}</span>{/if}
                </span>
              {/if}
            </label>
          {/if}
          {#if errors[f.id]}<span class="error">{errors[f.id]}</span>{/if}
          {#if isDetected(answer) && !(f.id in drafts)}
            <span class="detected">{t("setup.detected", { source: answer?.source.replace(/^detected:? ?/, "") || "—" })}</span>
          {/if}
          {#if f.why}<span class="why">{text(f.why, app.locale)}</span>{/if}
        </div>
      {/each}
    </div>

    <div class="nav">
      {#if current > 0}
        <button type="button" onclick={() => go(current - 1)} disabled={saving}>
          {t("setup.back", { step: text(schema.steps[current - 1]?.title, app.locale) })}
        </button>
      {/if}
      <span class="status" aria-live="polite">{saving ? t("setup.saving") : message}</span>
      {#if current < schema.steps.length - 1}
        <button type="button" class="primary" onclick={() => go(current + 1)} disabled={saving}>
          {t("setup.next", { step: text(schema.steps[current + 1]?.title, app.locale) })}
        </button>
      {:else}
        <button type="button" class="primary" onclick={finish} disabled={saving}>{t("setup.finish")}</button>
      {/if}
    </div>
  </section>

  <aside aria-labelledby="comp-title">
    <h2 id="comp-title">{t("setup.before")}</h2>
    <div class="meter">
      <span>{t("setup.complete")}</span><span class="mono">{percent}%</span>
    </div>
    <span aria-hidden="true" class="bar"><span style:width="{percent}%"></span></span>
    {#if platform.completeness.gaps.length}
      <p>{t("setup.gaps")}</p>
      <ul class="gaps">
        {#each platform.completeness.gaps as g (g.field)}
          {@const field = schema.steps.find((s) => s.id === g.step)?.fields.find((x) => x.id === g.field)}
          {@const stepTitle = schema.steps.find((s) => s.id === g.step)?.title}
          <li>
            <span class="what">{text(field?.label, app.locale) || g.label}</span>
            <span class="faint">{text(stepTitle, app.locale)}</span>
            <span class="blocks">{text(field?.prevents, app.locale) || g.prevents}</span>
          </li>
        {/each}
      </ul>
    {:else}
      <p>{t("setup.noGaps")}</p>
    {/if}
    <p class="faint">{t("setup.lastStep")}</p>
  </aside>
</main>

<style>
  .setup {
    display: flex;
    flex-wrap: wrap;
    gap: 24px;
    padding: 24px;
    align-items: flex-start;
  }
  nav {
    flex: 0 0 240px;
  }
  ol {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  li button {
    width: 100%;
    display: flex;
    gap: 10px;
    align-items: center;
    padding: 8px 10px;
    border: 0;
    border-radius: 8px;
    background: transparent;
    color: var(--text-muted);
    text-align: left;
    cursor: pointer;
  }
  li.current button {
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    color: var(--text);
  }
  .mark {
    flex: 0 0 24px;
    height: 24px;
    border-radius: 50%;
    display: grid;
    place-items: center;
    font-size: 12px;
    font-weight: 600;
    background: var(--raised);
  }
  .mark.done {
    background: var(--ok);
    color: var(--accent-text);
  }
  .current .mark:not(.done) {
    background: var(--accent);
    color: var(--accent-text);
  }
  .names {
    display: flex;
    flex-direction: column;
    font-size: 13px;
  }
  .sub {
    font-size: 12px;
    color: var(--text-faint);
  }
  .sub.left {
    color: var(--caution);
  }
  .form {
    flex: 999 1 480px;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  h1 {
    margin: 0;
    font-size: 22px;
  }
  .platform,
  .about {
    margin: 0;
    color: var(--text-muted);
  }
  .fields {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 16px;
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
  .prefill {
    padding: 12px;
    border: 1px dashed var(--border);
    border-radius: 10px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .found {
    margin: 0;
    padding-left: 18px;
    font-size: 12px;
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
    opacity: 0.6;
    cursor: not-allowed;
  }
  .nav {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-top: 8px;
  }
  .status {
    flex: 1 1 auto;
    font-size: 12px;
    color: var(--text-faint);
  }
  aside {
    flex: 1 1 280px;
    max-width: 340px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    font-size: 13px;
  }
  h2 {
    margin: 0;
    font-size: 15px;
  }
  .meter {
    display: flex;
    justify-content: space-between;
  }
  .bar {
    display: block;
    height: 6px;
    border-radius: 3px;
    background: var(--raised);
  }
  .bar span {
    display: block;
    height: 6px;
    border-radius: 3px;
    background: var(--accent);
  }
  .gaps {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .gaps li {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding-top: 8px;
    border-top: 1px solid var(--border-soft);
  }
  .what {
    font-weight: 600;
  }
  .blocks {
    color: var(--caution);
    font-size: 12px;
  }
  p {
    margin: 0;
  }
</style>
