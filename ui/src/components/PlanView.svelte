<script lang="ts">
  // The airframe's tuning plan (spec: Recommendation engine; mock-up Plan). One stage
  // at a time: what to change for the next flight, each with why, its evidence, what
  // it should do and how the flight checks it. A person accepts or rejects; only
  // accepted changes go into the exported file, and nothing reaches the aircraft from
  // here. Earlier recommendations show how the next flights graded them.
  import { save } from "@tauri-apps/plugin-dialog";
  import { app, t } from "../lib/app.svelte";
  import { latestFlight } from "../lib/data";
  import type { MessageKey } from "../lib/i18n";
  import {
    decisionCounts,
    groupByFinding,
    groupDecision,
    outcomes,
    partition,
    stageStates,
    testCard,
    type HistoryRow,
    type Plan,
    type RecGroup,
  } from "../lib/plan";
  import { formatNumber, formatTakeoff, stageKey, type FlightRow } from "../lib/report";
  import { engineCall, inDesktopApp } from "../lib/shell";
  import AirframePicker from "./AirframePicker.svelte";
  import StatusBadge from "./StatusBadge.svelte";

  let flight = $state<FlightRow | null>(null);
  let plan = $state<Plan | null>(null);
  let history = $state<HistoryRow[]>([]);
  let loading = $state(false);
  let error = $state("");
  let saved = $state("");
  let step = $state(1);

  async function load(airframe: string) {
    loading = true;
    error = "";
    try {
      flight = await latestFlight(airframe);
      plan = flight ? await engineCall<Plan>("recommend.plan", { flight_id: flight.id }) : null;
      history = await engineCall<HistoryRow[]>("recommend.history", { airframe_id: airframe });
      step = Math.min(step, Math.max(plan?.steps.length ?? 1, 1));
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    const airframe = app.scope.airframe;
    saved = "";
    if (airframe && app.shell.engine === "ready") load(airframe);
    else plan = null;
  });

  async function decide(g: RecGroup, status: "accepted" | "rejected" | "proposed") {
    if (!app.scope.airframe) return;
    try {
      // The changes of one finding are decided together; each is its own record.
      for (const rec of g.recs) {
        if (!rec.id || rec.record_version === undefined || rec.record_status === status) continue;
        await engineCall("recommend.set_status", { id: rec.id, status, version: rec.record_version });
      }
    } catch (e) {
      error = String(e);
    }
    await load(app.scope.airframe);
  }

  async function exportFile(kind: "mission planner" | "qgroundcontrol") {
    if (!plan || !flight) return;
    saved = "";
    const extension = kind === "mission planner" ? "param" : "params";
    try {
      const path = await save({
        defaultPath: `${flight.airframe}-step${step}.${extension}`,
        filters: [{ name: kind === "mission planner" ? "Mission Planner" : "QGroundControl", extensions: [extension] }],
      });
      if (!path) return;
      const out = await engineCall<{ path: string }>("recommend.export", {
        flight_id: plan.flight, kind, step, lang: app.locale, path,
      });
      saved = t("plan.export.saved", { path: out.path });
    } catch (e) {
      error = String(e);
    }
  }

  const parts = $derived(plan ? partition(plan) : { now: [], waiting: [], blocked: [] });
  const counts = $derived(plan ? decisionCounts(plan) : { accepted: 0, open: 0 });
  const card = $derived(plan ? testCard(plan, app.locale) : { first: [], checks: [] });
  const graded = $derived(outcomes(history));
  const when = $derived(formatTakeoff(flight?.utc_us ?? null, app.locale) ?? t("flights.noTime"));

  function value(v: number | null): string {
    return v === null ? "–" : formatNumber(v, app.locale);
  }
</script>

<main>
  <div class="top">
    <div>
      <h1>{flight ? t("plan.title", { airframe: flight.airframe }) : t("nav.plan")}</h1>
      <p class="faint">{t("plan.subtitle")}</p>
      {#if flight}<p class="faint">{t("plan.from", { when })}</p>{/if}
    </div>
    <AirframePicker />
  </div>
  {#if error}<p class="error">{error}</p>{/if}

  {#if app.scope.airframe && !loading && !flight}
    <p class="faint">{t("plan.noFlights")}</p>
  {/if}

  {#if plan}
    <ol class="stages" aria-label={t("nav.plan")}>
      {#each stageStates(plan) as s (s.stage)}
        <li class={s.state} aria-current={s.state === "now" ? "step" : undefined}>
          <span class="mark" aria-hidden="true">{s.state === "clear" ? "✓" : s.state === "now" ? "●" : "○"}</span>
          <span>{t(stageKey(s.stage))}</span>
          <span class="faint">{t(`plan.stage.${s.state}` as MessageKey)}{s.count ? ` · ${s.count}` : ""}</span>
        </li>
      {/each}
    </ol>

    <div class="split">
      <section class="recs" aria-labelledby="now-title">
        <h2 id="now-title">{t("plan.now")}</h2>
        {#if parts.now.length === 0}<p class="faint">{t("plan.empty")}</p>{/if}
        {#each groupByFinding(parts.now) as g (g.key)}
          {@render recCard(g)}
        {/each}

        {#if parts.waiting.length}
          <h2>{t("plan.waiting")}</h2>
          <ul class="brief">
            {#each parts.waiting as r (r.id ?? `${r.param}|${r.finding}`)}
              <li>
                <span>{r.why[app.locale]}</span>
                <span class="faint">{r.param ? `${r.param} · ` : ""}{t(stageKey(r.stage))}</span>
              </li>
            {/each}
          </ul>
        {/if}

        {#if parts.blocked.length}
          <h2>{t("plan.blocked")}</h2>
          <ul class="brief">
            {#each parts.blocked as r (`${r.param}|${r.proposed}`)}
              <li>
                <span class="mono">{r.param} → {value(r.proposed)}</span>
                <span class="faint">{r.notes.map((n) => n.text[app.locale]).join(" ")}</span>
              </li>
            {/each}
          </ul>
        {/if}
      </section>

      <aside>
        <section class="panel" aria-labelledby="export-title">
          <h2 id="export-title">{t("plan.export")}</h2>
          <p>{t("plan.export.counts", counts)}</p>
          {#if plan.steps.length > 1}
            <label class="faint">
              <select bind:value={step}>
                {#each plan.steps as s (s.number)}
                  <option value={s.number}>{t("plan.export.step", { step: s.number, steps: plan.steps.length })}</option>
                {/each}
              </select>
            </label>
          {/if}
          <button type="button" class="primary" disabled={!counts.accepted || !inDesktopApp()} onclick={() => exportFile("mission planner")}>
            {t("plan.export.mp")}
          </button>
          <button type="button" class="secondary" disabled={!counts.accepted || !inDesktopApp()} onclick={() => exportFile("qgroundcontrol")}>
            {t("plan.export.qgc")}
          </button>
          <p class="faint">{t("plan.export.note", { firmware: plan.firmware })}</p>
          {#if !inDesktopApp()}<p class="faint">{t("plan.export.unavailable")}</p>{/if}
          {#if saved}<p class="ok" role="status">{saved}</p>{/if}
        </section>

        <section class="panel" aria-labelledby="card-title">
          <h2 id="card-title">{t("plan.card")}</h2>
          {#if card.first.length}
            <h3>{t("plan.card.first")}</h3>
            <ul>{#each card.first as line (line)}<li>{line}</li>{/each}</ul>
          {/if}
          <ul>{#each card.checks as line (line)}<li>{line}</li>{/each}</ul>
          <p class="faint">{t("plan.card.after")}</p>
        </section>

        <section class="panel" aria-labelledby="outcomes-title">
          <h2 id="outcomes-title">{t("plan.outcomes")}</h2>
          {#if graded.length === 0}<p class="faint">{t("plan.outcomes.none")}</p>{/if}
          <ul class="brief">
            {#each graded as h (h.id)}
              <li>
                <span class="mono">{h.param ?? h.detail.code}</span>
                <StatusBadge
                  status={h.status === "confirmed" ? "ok" : h.status === "regressed" ? "out" : "caution"}
                  label={t(`grade.${h.status.replace(" ", "_")}` as MessageKey)}
                />
              </li>
            {/each}
          </ul>
        </section>
      </aside>
    </div>
  {/if}
</main>

{#snippet recCard(g: RecGroup)}
  {@const r = g.lead}
  {@const decision = groupDecision(g)}
  {@const names = new Set(g.recs.map((x) => x.param))}
  {@const extra = [...new Set(g.recs.flatMap((x) => x.requires))].filter((q) => q !== "reboot" && !names.has(q.split("=")[0] ?? ""))}
  <article class="rec" class:disputed={r.status === "disputed"}>
    <header>
      <StatusBadge
        status={r.kind === "safety" ? "out" : r.kind === "stability" ? "caution" : "info"}
        label={t(`plan.kind.${r.kind}` as MessageKey)}
      />
      <span class="faint">{t(stageKey(r.stage))} · {r.source.replace("finding:", "")}</span>
    </header>
    <h3>{r.why[app.locale]}</h3>
    {#if r.param === null}
      <p class="faint">{t("plan.action")}</p>
    {:else}
      <table>
        <thead>
          <tr>
            <th scope="col">{t("plan.col.param", { firmware: plan?.firmware ?? "" })}</th>
            <th scope="col">{t("plan.col.now")}</th>
            <th scope="col">{t("plan.col.proposed")}</th>
          </tr>
        </thead>
        <tbody>
          {#each g.recs as x (x.param)}
            <tr>
              <td class="mono">{x.param}{x.reboot ? " ⟳" : ""}</td>
              <td class="mono">{value(x.current)}</td>
              <td class="mono">
                {value(x.proposed)}{x.target !== null ? ` (${t("plan.target", { target: value(x.target) })})` : ""}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
    {#if r.status === "disputed"}<p class="caution">{t("plan.disputed")}</p>{/if}
    {#each [...new Map(g.recs.flatMap((x) => x.notes).filter((n) => n.reason !== "waiting").map((n) => [n.text.en, n])).values()] as n (n.text.en)}
      <p class="faint">{n.text[app.locale]}</p>
    {/each}
    <p>{r.expected_effect[app.locale]}</p>
    {#if r.verify}<p><strong>{t("plan.verify")}</strong> {r.verify[app.locale]}</p>{/if}
    {#if extra.length}
      <p class="faint mono">{t("plan.requires", { list: extra.join(", ") })}</p>
    {/if}
    {#if g.recs.some((x) => x.reboot)}<p class="faint">⟳ {t("plan.reboot")}</p>{/if}
    {#if r.param !== null && r.status === "next" && decision !== "none"}
      <div class="actions">
        {#if decision === "accepted" || decision === "rejected" || decision === "applied"}
          <StatusBadge
            status={decision === "rejected" ? "info" : "ok"}
            label={t(`plan.status.${decision}` as MessageKey)}
          />
          {#if decision !== "applied"}
            <button type="button" class="link" onclick={() => decide(g, "proposed")}>{t("plan.undo")}</button>
          {/if}
        {:else}
          <button type="button" class="primary" onclick={() => decide(g, "accepted")}>{t("plan.accept")}</button>
          <button type="button" class="secondary" onclick={() => decide(g, "rejected")}>{t("plan.reject")}</button>
        {/if}
      </div>
    {/if}
  </article>
{/snippet}

<style>
  main {
    padding: 16px 24px 32px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .top {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
    flex-wrap: wrap;
  }
  h1 {
    margin: 0 0 4px;
    font-size: 20px;
  }
  h2 {
    margin: 8px 0 10px;
    font-size: 15px;
  }
  h3 {
    margin: 0;
    font-size: 14px;
  }
  p {
    margin: 0;
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
  }
  .error {
    color: var(--out);
  }
  .ok {
    color: var(--ok);
    font-size: 13px;
    overflow-wrap: anywhere;
  }
  .caution {
    color: var(--caution);
    font-size: 13px;
  }
  .stages {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .stages li {
    display: flex;
    flex-direction: column;
    gap: 1px;
    padding: 6px 10px;
    border: 1px solid var(--border-soft);
    border-radius: 8px;
    font-size: 13px;
    min-width: 96px;
  }
  .stages li.now {
    border-color: var(--accent);
  }
  .stages li.clear .mark {
    color: var(--ok);
  }
  .stages li.later {
    color: var(--text-muted);
  }
  .split {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    align-items: flex-start;
  }
  .recs {
    flex: 3 1 460px;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  aside {
    flex: 2 1 280px;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .rec,
  .panel {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 14px 16px;
    background: var(--surface);
    border: 1px solid var(--border-soft);
    border-radius: 12px;
  }
  .rec.disputed {
    border-color: var(--caution);
  }
  .rec header {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  table {
    border-collapse: collapse;
    align-self: flex-start;
  }
  th,
  td {
    text-align: left;
    padding: 4px 16px 4px 0;
    font-weight: normal;
  }
  th {
    font-size: 12px;
    color: var(--text-muted);
  }
  .actions {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .brief {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .brief li {
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-size: 13px;
  }
  .panel ul:not(.brief) {
    margin: 0;
    padding-left: 18px;
    font-size: 13px;
  }
  .primary,
  .secondary {
    border-radius: 8px;
    padding: 7px 12px;
    font-weight: 600;
    cursor: pointer;
  }
  .primary {
    background: var(--accent);
    color: var(--accent-text);
    border: none;
  }
  .secondary {
    background: none;
    color: var(--text-2);
    border: 1px solid var(--border);
  }
  .primary:disabled,
  .secondary:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .link {
    background: none;
    border: none;
    padding: 0;
    color: var(--link);
    cursor: pointer;
    font-size: 13px;
  }
</style>
