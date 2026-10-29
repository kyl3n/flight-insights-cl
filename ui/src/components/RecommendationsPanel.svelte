<script lang="ts">
  // Recommendations on the Range and speed screen (spec: Power and efficiency analysis,
  // "Recommendations"; mock-up Performance, "Recommendations"): the latest flight's plan,
  // kept to what the power model and the speed build-up propose (cruise and home speeds,
  // BATT_LOW_MAH, pack capacity and ageing, the next speed step). Each opens in the plan,
  // which exports a parameter file for a person to load; nothing is written to the aircraft.
  import { app, t } from "../lib/app.svelte";
  import { latestFlight } from "../lib/data";
  import { rangeRecs, type Plan, type PlanRec } from "../lib/plan";
  import { formatNumber } from "../lib/report";
  import { engineCall } from "../lib/shell";
  import type { MessageKey } from "../lib/i18n";

  let { airframeId }: { airframeId: string } = $props();

  let recs = $state<PlanRec[]>([]);
  let error = $state("");
  let loaded = $state(false);

  $effect(() => {
    const id = airframeId;
    loaded = false;
    error = "";
    latestFlight(id)
      .then((f) => (f ? engineCall<Plan>("recommend.plan", { flight_id: f.id }) : null))
      .then((p) => (recs = p ? rangeRecs(p) : []))
      .catch((e: unknown) => {
        recs = [];
        error = String(e);
      })
      .finally(() => (loaded = true));
  });

  const value = (v: number | null) => (v === null ? "–" : formatNumber(v, app.locale));
  function open() {
    app.scope.airframe = airframeId;
    app.view = "plan";
  }
</script>

{#if loaded && (recs.length || error)}
  <section class="panel" aria-labelledby="rec-title">
    <div class="head">
      <h2 id="rec-title">{t("rangeRecs.title")}</h2>
      <span class="faint">{t("rangeRecs.about")}</span>
    </div>
    {#if error}<p class="error">{error}</p>{/if}
    <div class="cards">
      {#each recs as r, i (`${r.finding}|${r.param}|${i}`)}
        <article>
          <div class="top">
            {#if r.param}
              <span class="mono strong">{r.param}</span>
              <span class="mono">{value(r.current)} <span aria-hidden="true">→</span> {value(r.proposed)}</span>
            {:else}
              <span class="strong">{t("rangeRecs.action")}</span>
            {/if}
            <span class="tag" class:safety={r.kind === "safety"}>{t(`rangeRecs.kind.${r.kind}` as MessageKey)}</span>
            {#if r.record_status === "accepted" || r.record_status === "rejected" || r.record_status === "applied"}<span class="tag">{t(`plan.status.${r.record_status}` as MessageKey)}</span>{/if}
          </div>
          <p>{r.why[app.locale] ?? r.why.en}</p>
          {#if r.expected_effect && (r.expected_effect[app.locale] ?? r.expected_effect.en)}
            <p class="faint">{r.expected_effect[app.locale] ?? r.expected_effect.en}</p>
          {/if}
          <button type="button" class="link" onclick={open}>{t("rangeRecs.open")}</button>
        </article>
      {/each}
    </div>
  </section>
{/if}

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 14px 16px;
    background: var(--surface);
    border: 1px solid var(--border-soft);
    border-radius: 12px;
    min-width: 0;
  }
  .head {
    display: flex;
    gap: 6px 12px;
    align-items: baseline;
    flex-wrap: wrap;
  }
  h2 {
    margin: 0;
    font-size: 15px;
  }
  p {
    margin: 0;
    font-size: 13px;
    line-height: 1.4;
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
  }
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 10px;
  }
  article {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
    border: 1px solid var(--border-soft);
    border-radius: 10px;
    background: var(--surface-2);
    min-width: 0;
  }
  .top {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 10px;
    align-items: baseline;
    font-size: 13px;
  }
  .mono {
    font-family: var(--mono);
  }
  .strong {
    font-weight: 600;
  }
  .tag {
    display: inline-block;
    padding: 1px 7px;
    border-radius: 6px;
    font-size: 11px;
    background: var(--surface);
    color: var(--text-2);
    border: 1px solid var(--border-soft);
  }
  .tag.safety {
    color: var(--caution);
  }
  .link {
    align-self: flex-start;
    margin-top: auto;
    padding: 6px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: transparent;
    color: var(--link);
    font-size: 13px;
    cursor: pointer;
  }
  .error {
    color: var(--out);
  }
</style>
