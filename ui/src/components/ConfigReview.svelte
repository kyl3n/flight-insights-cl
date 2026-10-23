<script lang="ts">
  // The airframe's configuration review (spec: Configuration review; mock-up
  // ConfigReview): the parameters its latest flight flew with, checked against the
  // platform's answers. Errors first, then settings that are not the best this aircraft
  // could have, then minor points; each says why for this aircraft, the change it
  // proposes, and can join the plan.
  import { app, t } from "../lib/app.svelte";
  import { getRegistry, latestFlight } from "../lib/data";
  import { recFor, type Plan, type PlanRec } from "../lib/plan";
  import {
    findingAction,
    findingTitle,
    formatNumber,
    formatTakeoff,
    severityLook,
    sortFindings,
    stageKey,
    type Analysis,
    type FindingRow,
    type FlightRow,
    type Registry,
  } from "../lib/report";
  import { text, type AirframeView, type PlatformView, type SetupSchema } from "../lib/setup";
  import { engineCall } from "../lib/shell";
  import { STATUS } from "../lib/status";
  import AirframePicker from "./AirframePicker.svelte";
  import StatusBadge from "./StatusBadge.svelte";

  type Category = "errors" | "optimal" | "minor";

  let reg = $state<Registry | null>(null);
  let flight = $state<FlightRow | null>(null);
  let analysis = $state<Analysis | null>(null);
  let plan = $state<Plan | null>(null);
  let platform = $state<PlatformView | null>(null);
  let schema = $state<SetupSchema | null>(null);
  let error = $state("");

  // The analyzers whose findings are about the configuration itself.
  const REVIEW = ["params.", "option."];

  async function load(airframeId: string) {
    error = "";
    try {
      reg = await getRegistry();
      flight = await latestFlight(airframeId);
      const airframe = await engineCall<AirframeView>("airframe.get", { id: airframeId });
      [platform, schema] = await Promise.all([
        engineCall<PlatformView>("platform.get", { id: airframe.platform.id }),
        engineCall<SetupSchema>("setup.schema"),
      ]);
      if (flight) {
        analysis = await engineCall<Analysis>("analysis.run", { flight_id: flight.id });
        plan = await engineCall<Plan>("recommend.plan", { flight_id: flight.id });
      } else {
        analysis = plan = null;
      }
    } catch (e) {
      error = String(e);
    }
  }

  $effect(() => {
    const id = app.scope.airframe;
    if (id && app.shell.engine === "ready") load(id);
  });

  function category(f: FindingRow): Category {
    return f.severity === "safety" || f.severity === "out" ? "errors" : f.severity === "caution" ? "optimal" : "minor";
  }

  const findings = $derived(
    analysis ? sortFindings(analysis.findings.filter((f) => REVIEW.some((p) => f.code.startsWith(p)))) : [],
  );
  const groups = $derived(
    (["errors", "optimal", "minor"] as Category[]).map((c) => [c, findings.filter((f) => category(f) === c)] as const),
  );
  const answered = $derived.by(() => {
    if (!platform || !schema) return [];
    const spec = platform.spec;
    return schema.steps
      .flatMap((s) => s.fields)
      .filter((f) => spec[f.id] !== undefined)
      .map((f) => ({ id: f.id, label: text(f.label, app.locale), value: String(spec[f.id]?.value) }));
  });
  const accepted = $derived(plan?.recommendations.filter((r) => r.record_status === "accepted").length ?? 0);

  async function addToPlan(rec: PlanRec) {
    if (!rec.id || rec.record_version === undefined || !app.scope.airframe) return;
    try {
      await engineCall("recommend.set_status", { id: rec.id, status: "accepted", version: rec.record_version });
      await load(app.scope.airframe);
    } catch (e) {
      error = String(e);
    }
  }

  function value(v: number | null): string {
    return v === null ? "–" : formatNumber(v, app.locale);
  }
</script>

<main>
  <div class="top">
    <div>
      <h1>{flight ? t("review.title", { airframe: flight.airframe }) : t("nav.params")}</h1>
      {#if flight && platform}
        <p class="faint">
          {t("review.subtitle", {
            when: formatTakeoff(flight.utc_us, app.locale) ?? t("flights.noTime"),
            platform: platform.name,
          })}
        </p>
      {/if}
    </div>
    <div class="tools">
      <AirframePicker />
      {#if plan}
        <button type="button" class="link" onclick={() => (app.view = "plan")}>
          {t("review.openPlan", { count: accepted })}
        </button>
      {/if}
    </div>
  </div>
  {#if error}<p class="error">{error}</p>{/if}
  {#if app.scope.airframe && !flight && !error}<p class="faint">{t("plan.noFlights")}</p>{/if}

  {#if platform}
    <section class="panel" aria-labelledby="answers-title">
      <div class="row">
        <h2 id="answers-title">{t("review.answers", { platform: platform.name })}</h2>
        <button
          type="button"
          class="link"
          onclick={() => {
            app.setupPlatform = platform?.id ?? null;
            app.view = "fleet";
          }}
        >
          {t("review.answers.edit")}
        </button>
      </div>
      {#if answered.length === 0}
        <p class="faint">{t("review.answers.none")}</p>
      {:else}
        <dl>
          {#each answered as a (a.id)}
            <dt>{a.label}</dt>
            <dd>{a.value}</dd>
          {/each}
        </dl>
      {/if}
    </section>
  {/if}

  {#if analysis && reg}
    {@const registry = reg}
    <p class="summary">
      <strong>{t("review.summary", { total: findings.length })}</strong>
      {#each groups as [c, list] (c)}
        <span class="faint">· {t(`review.cat.${c}`)} {list.length}</span>
      {/each}
    </p>
    {#if findings.length === 0}<p class="faint">{t("review.empty")}</p>{/if}
    {#each groups as [c, list] (c)}
      {#if list.length}
        <section aria-labelledby="cat-{c}">
          <h2 id="cat-{c}">{t(`review.cat.${c}`)}</h2>
          <div class="cards">
            {#each list as f (f.id)}
              {@const look = severityLook(f.severity)}
              {@const rec = plan ? recFor(plan, f.id) : undefined}
              <article class="card">
                <header>
                  <span class="sev" style:color="var({STATUS[look.status].tone})">
                    {STATUS[look.status].glyph} {t(look.label)}
                  </span>
                  {#if f.stage}<span class="faint">{t(stageKey(f.stage))}</span>{/if}
                </header>
                <h3>{findingTitle(registry, f, app.locale)}</h3>
                <p>{findingAction(registry, f, app.locale)}</p>
                {#if f.changes.length}
                  <table>
                    <tbody>
                      {#each f.changes as ch (ch.param)}
                        <tr>
                          <th scope="row" class="mono">{ch.param}</th>
                          <td class="mono">{value(ch.current)}</td>
                          <td aria-hidden="true">→</td>
                          <td class="mono">{value(ch.proposed)}</td>
                        </tr>
                      {/each}
                    </tbody>
                  </table>
                {/if}
                {#if rec?.param}
                  <div class="actions">
                    {#if rec.record_status === "accepted"}
                      <StatusBadge status="ok" label={t("review.inPlan")} />
                    {:else if rec.status === "waiting"}
                      <span class="faint">{t("review.waits", { stage: t(stageKey(plan?.stage ?? rec.stage)) })}</span>
                    {:else if rec.status === "next" && rec.id}
                      <button type="button" class="secondary" onclick={() => addToPlan(rec)}>{t("review.add")}</button>
                    {/if}
                  </div>
                {/if}
              </article>
            {/each}
          </div>
        </section>
      {/if}
    {/each}
  {/if}
</main>

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
  .tools {
    display: flex;
    align-items: center;
    gap: 14px;
  }
  h1 {
    margin: 0 0 4px;
    font-size: 20px;
  }
  h2 {
    margin: 0 0 10px;
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
  .summary {
    display: flex;
    gap: 8px;
    align-items: baseline;
  }
  .panel,
  .card {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 14px 16px;
    background: var(--surface);
    border: 1px solid var(--border-soft);
    border-radius: 12px;
  }
  .row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
  }
  dl {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 6px 16px;
    margin: 0;
  }
  dt {
    font-size: 12px;
    color: var(--text-muted);
  }
  dd {
    margin: 0 0 4px;
    font-size: 13px;
  }
  .cards {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .card header {
    display: flex;
    gap: 10px;
    align-items: baseline;
  }
  .sev {
    font-size: 12px;
    font-weight: 600;
  }
  table {
    border-collapse: collapse;
    align-self: flex-start;
  }
  th,
  td {
    text-align: left;
    padding: 2px 10px 2px 0;
    font-weight: normal;
  }
  .actions {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .secondary {
    background: none;
    color: var(--text-2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 12px;
    font-weight: 600;
    cursor: pointer;
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
