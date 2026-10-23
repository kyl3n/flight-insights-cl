<script lang="ts">
  // One flight's report (spec: User interface, "Flight report"; mock-up FlightReport):
  // what it measured against each value's range, and every finding by severity with
  // the evidence it rests on. Selecting a finding opens the inspector beside the list.
  // The written summary arrives with the AI layer; this view stands without it.
  import { app, t } from "../lib/app.svelte";
  import { getRegistry } from "../lib/data";
  import { CATALOGS, type MessageKey } from "../lib/i18n";
  import {
    findingAction,
    findingTitle,
    formatDuration,
    formatNumber,
    formatTakeoff,
    groupMetrics,
    severityLook,
    sortFindings,
    stageKey,
    subsystem,
    type Analysis,
    type Baseline,
    type FindingRow,
    type FlightRow,
    type Registry,
  } from "../lib/report";
  import { engineCall } from "../lib/shell";
  import { STATUS } from "../lib/status";
  import StatusBadge from "./StatusBadge.svelte";
  import Value from "./Value.svelte";
  import Workbench from "./Workbench.svelte";

  let { flightId }: { flightId: string } = $props();

  let reg = $state<Registry | null>(null);
  let analysis = $state<Analysis | null>(null);
  let flight = $state<FlightRow | null>(null);
  let busy = $state(false);
  let error = $state("");
  let selected = $state<string | null>(null);
  let tab = $state<"report" | "filters">("report");

  async function load(force = false) {
    busy = true;
    error = "";
    try {
      reg = await getRegistry();
      analysis = await engineCall<Analysis>("analysis.run", { flight_id: flightId, force });
      const rows = await engineCall<FlightRow[]>("flight.list", { flight_id: flightId });
      flight = rows[0] ?? null;
    } catch (e) {
      error = String(e);
    } finally {
      busy = false;
    }
  }

  $effect(() => {
    void flightId;
    selected = null;
    load();
  });

  const findings = $derived(analysis ? sortFindings(analysis.findings) : []);
  const chosen = $derived(findings.find((f) => f.id === selected) ?? null);
  const skipped = $derived(
    analysis
      ? Object.entries(analysis.analyzers)
          .filter(([, o]) => o.skipped?.startsWith("failed"))
          .map(([name]) => name)
      : [],
  );
  const evidence = $derived(new Map((analysis?.evidence ?? []).map((e) => [e.id, e])));

  const baselines = new Map<string, Promise<Baseline | null>>();
  function baselineFor(metric: string, segment: string): () => Promise<Baseline | null> {
    return () => {
      const key = `${metric}|${segment}`;
      if (!baselines.has(key) && flight) {
        baselines.set(
          key,
          engineCall<{ baseline: Baseline | null }>("trend.series", {
            airframe_id: flight.airframe_id,
            metric,
            segment,
            payload: flight.payload,
          }).then((s) => s.baseline),
        );
      }
      return baselines.get(key) ?? Promise.resolve(null);
    };
  }

  function subsystemName(key: string): string {
    const k = `subsystem.${key}`;
    return k in CATALOGS.en ? t(k as MessageKey) : key;
  }

  function shown(v: unknown): string {
    if (typeof v === "number") return formatNumber(v, app.locale);
    if (typeof v === "string") return v;
    return JSON.stringify(v);
  }

  const durationText = $derived.by(() => {
    const d = formatDuration(flight?.duration_s ?? null);
    return d ? t("flights.duration", d) : "";
  });
</script>

<main>
  <button type="button" class="back" onclick={() => (app.scope.flight = null)}>← {t("report.back")}</button>

  <header>
    <div>
      <h1>{formatTakeoff(flight?.utc_us ?? null, app.locale) ?? t("flights.noTime")}</h1>
      <p class="faint">
        {[flight?.airframe, durationText, flight?.modes.join(", "),
          flight?.firmware ? t("report.firmware", { firmware: flight.firmware }) : "",
          flight?.payload ? t("report.payload", { payload: flight.payload }) : ""]
          .filter(Boolean)
          .join(" · ")}
      </p>
    </div>
    <button type="button" class="primary" disabled={busy} onclick={() => load(true)}>
      {busy ? t("report.analysing") : t("report.analyse")}
    </button>
  </header>

  <div role="tablist" class="tabs" aria-label={t("report.findings")}>
    {#each [["report", "report.tab.report"], ["filters", "report.tab.filters"]] as const as [id, label] (id)}
      <button type="button" role="tab" aria-selected={tab === id} onclick={() => (tab = id)}>{t(label)}</button>
    {/each}
  </div>

  {#if tab === "filters"}
    <Workbench {flightId} />
  {:else}
  {#if error}<p class="error">{t("report.error", { error })}</p>{/if}
  {#if analysis && !analysis.current}<p class="notice">{t("report.stale")}</p>{/if}
  {#if skipped.length}<p class="notice">{t("report.partial", { names: skipped.join(", ") })}</p>{/if}

  {#if analysis && reg}
    {@const registry = reg}
    <section aria-labelledby="values-title">
      <h2 id="values-title">{t("report.values")}</h2>
      {#each groupMetrics(analysis.metrics, registry) as [group, values] (group)}
        <div class="group">
          <h3>{subsystemName(group)}</h3>
          <div class="values">
            {#each values as v (`${v.metric}|${v.segment}`)}
              {@const metric = registry.metrics[v.metric]}
              {#if metric}
                <Value
                  id={v.metric}
                  {metric}
                  value={v.value}
                  zone={v.zone}
                  context={t(`segment.${v.segment}` as MessageKey)}
                  loadBaseline={baselineFor(v.metric, v.segment)}
                />
              {/if}
            {/each}
          </div>
        </div>
      {/each}
    </section>

    <div class="split">
      <section aria-labelledby="findings-title" class="findings">
        <h2 id="findings-title">
          {t("report.findings")}
          <span class="faint">{t("report.findings.count", { count: findings.length })}</span>
        </h2>
        {#if findings.length === 0}
          <p class="faint">{t("report.findings.none")}</p>
        {/if}
        <ul>
          {#each findings as f (f.id)}
            {@const look = severityLook(f.severity)}
            <li>
              <button
                type="button"
                class="finding"
                aria-pressed={selected === f.id}
                onclick={() => (selected = selected === f.id ? null : f.id)}
              >
                <span class="sev" style:color="var({STATUS[look.status].tone})">
                  {STATUS[look.status].glyph} {t(look.label)}
                </span>
                <span class="title">{findingTitle(registry, f, app.locale)}</span>
                <span class="faint">
                  {subsystemName(subsystem(f.code))}{f.stage ? ` · ${t(stageKey(f.stage))}` : ""}
                </span>
              </button>
            </li>
          {/each}
        </ul>
      </section>

      <aside class="inspector" aria-live="polite">
        {#if chosen}
          {@render inspector(registry, chosen)}
        {:else}
          <p class="faint">{t("inspect.none")}</p>
        {/if}
      </aside>
    </div>
  {/if}
  {/if}
</main>

{#snippet inspector(registry: Registry, f: FindingRow)}
  {@const look = severityLook(f.severity)}
  <div class="inspect-head">
    <StatusBadge status={look.status} label={t(look.label)} />
    <span class="faint">
      {f.stage ? t("inspect.stage", { stage: t(stageKey(f.stage)) }) : ""}
      · {t("inspect.confidence", { level: t(`confidence.${f.confidence}` as MessageKey) })}
    </span>
  </div>
  <h3>{findingTitle(registry, f, app.locale)}</h3>
  <section>
    <h4>{t("inspect.before")}</h4>
    <p>{findingAction(registry, f, app.locale)}</p>
  </section>
  {#if f.changes.length}
    <section>
      <h4>{t("inspect.changes")}</h4>
      <table class="changes">
        <tbody>
          {#each f.changes as c (c.param)}
            <tr>
              <th scope="row" class="mono">{c.param}</th>
              <td class="mono">{c.current === null ? "–" : formatNumber(c.current, app.locale)}</td>
              <td aria-hidden="true">→</td>
              <td class="mono">{c.proposed === null ? "–" : formatNumber(c.proposed, app.locale)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
      <p class="faint">{t("inspect.changes.note")}</p>
    </section>
  {/if}
  <section>
    <h4>{t("inspect.evidence")}</h4>
    <ul class="evidence">
      {#each f.evidence as id (id)}
        {@const ev = evidence.get(id)}
        <li>
          <p>{ev?.summary ?? id}</p>
          {#if ev?.messages.length}<p class="faint mono">{ev.messages.join(", ")}</p>{/if}
          {#if ev}
            <dl>
              {#each Object.entries(ev.values).slice(0, 12) as [k, v] (k)}
                <dt class="mono">{k}</dt>
                <dd class="mono">{shown(v)}</dd>
              {/each}
            </dl>
          {/if}
        </li>
      {/each}
    </ul>
  </section>
  <button type="button" class="link" onclick={() => (selected = null)}>{t("inspect.close")}</button>
{/snippet}

<style>
  main {
    padding: 16px 24px 32px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 12px;
  }
  h1 {
    margin: 0;
    font-size: 20px;
  }
  h2 {
    margin: 0 0 10px;
    font-size: 15px;
  }
  h3 {
    margin: 0 0 8px;
    font-size: 13px;
    color: var(--text-muted);
  }
  h4 {
    margin: 0 0 4px;
    font-size: 12px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  p {
    margin: 0;
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
    font-weight: normal;
  }
  .error {
    color: var(--out);
  }
  .notice {
    padding: 8px 12px;
    border: 1px solid color-mix(in srgb, var(--caution) 50%, var(--border));
    border-radius: 8px;
    font-size: 13px;
  }
  .back,
  .link {
    align-self: flex-start;
    background: none;
    border: none;
    padding: 0;
    color: var(--link);
    cursor: pointer;
    font-size: 13px;
  }
  .primary {
    background: var(--accent);
    color: var(--accent-text);
    border: none;
    border-radius: 8px;
    padding: 8px 14px;
    font-weight: 600;
    cursor: pointer;
  }
  .primary:disabled {
    opacity: 0.6;
    cursor: default;
  }
  .group {
    margin-bottom: 12px;
  }
  .tabs {
    display: flex;
    gap: 4px;
    border-bottom: 1px solid var(--border-soft);
  }
  .tabs button {
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text-muted);
    padding: 6px 12px;
    cursor: pointer;
    font-size: 14px;
  }
  .tabs button[aria-selected="true"] {
    color: var(--text);
    border-bottom-color: var(--accent);
  }
  .values {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .split {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    align-items: flex-start;
  }
  .findings {
    flex: 3 1 420px;
    min-width: 0;
  }
  .findings ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .finding {
    width: 100%;
    display: grid;
    grid-template-columns: 120px 1fr;
    gap: 2px 12px;
    padding: 10px 12px;
    text-align: left;
    background: var(--surface);
    border: 1px solid var(--border-soft);
    border-radius: 10px;
    color: var(--text);
    cursor: pointer;
  }
  .finding[aria-pressed="true"] {
    border-color: var(--accent);
  }
  .finding .faint {
    grid-column: 2;
  }
  .sev {
    font-size: 12px;
    font-weight: 600;
  }
  .inspector {
    flex: 2 1 300px;
    min-width: 0;
    padding: 14px 16px;
    background: var(--surface);
    border: 1px solid var(--border-soft);
    border-radius: 12px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .inspect-head {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
  }
  .changes th,
  .changes td {
    padding: 2px 10px 2px 0;
    text-align: left;
    font-weight: normal;
  }
  .evidence {
    margin: 0;
    padding-left: 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 2px 10px;
    margin: 4px 0 0;
    font-size: 12px;
  }
  dt {
    color: var(--text-muted);
  }
  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
</style>
