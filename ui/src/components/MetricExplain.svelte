<script lang="ts">
  // A metric's full explanation, opened by clicking a value (spec: Explanations for
  // every metric and data point; mock-up MetricExplain). Every word comes from the
  // knowledge pack's registry, so the app explains exactly what the engine computes.
  import { onMount } from "svelte";
  import { app, t } from "../lib/app.svelte";
  import type { MessageKey } from "../lib/i18n";
  import {
    formatValue,
    intervalText,
    unitText,
    zoneLook,
    type Baseline,
    type Interval,
    type MetricInfo,
    type Zone,
  } from "../lib/report";
  import StatusBadge from "./StatusBadge.svelte";
  import ZoneBar from "./ZoneBar.svelte";

  let {
    id,
    metric,
    value,
    zone,
    context,
    baseline,
    onclose,
  }: {
    id: string;
    metric: MetricInfo;
    value: number;
    zone: Zone | null;
    context: string;
    baseline: Baseline | null | undefined;
    onclose: () => void;
  } = $props();

  let closeButton: HTMLButtonElement | undefined = $state();
  onMount(() => {
    const opener = document.activeElement as HTMLElement | null;
    closeButton?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onclose();
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      opener?.focus(); // back where the person was
    };
  });

  const words = $derived({ below: t("metric.below"), above: t("metric.above") });
  const zones = $derived(
    (["target", "caution", "out"] as Zone[])
      .filter((z) => metric.zones[z]?.length)
      .map((z) => [z, metric.zones[z] as Interval[]] as const),
  );
  const look = $derived(zoneLook(zone));
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="backdrop" onclick={(e) => e.target === e.currentTarget && onclose()}>
  <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="metric-title">
    <header>
      <div>
        <p class="eyebrow">{t("metric.explain")}</p>
        <h1 id="metric-title">{metric.name[app.locale]}</h1>
        <p class="faint mono">{id}{metric.unit ? ` · ${unitText(metric.unit, app.locale)}` : ""}</p>
      </div>
      <button type="button" class="close" bind:this={closeButton} onclick={onclose}>
        {t("metric.close")}
      </button>
    </header>

    <div class="value">
      <strong class="mono">{formatValue(value, metric.unit, app.locale)}</strong>
      <StatusBadge status={look.status} label={t(look.label)} />
      <span class="faint">{context}</span>
    </div>
    <ZoneBar {metric} {value} />

    <section>
      <h2>{t("metric.what")}</h2>
      <p>{metric.definition[app.locale]}</p>
      <p class="faint">{t(`metric.direction.${metric.direction}` as MessageKey)}</p>
    </section>

    <section>
      <h2>{t("metric.how")}</h2>
      <p>{metric.method[app.locale]}</p>
      {#if metric.messages.length}
        <p class="faint">
          {t("metric.messages")}: <span class="mono">{metric.messages.join(", ")}</span>
        </p>
      {/if}
    </section>

    {#if zones.length}
      <section>
        <h2>{t("metric.meaning")}</h2>
        <table>
          <tbody>
            {#each zones as [z, intervals] (z)}
              <tr>
                <th scope="row"><StatusBadge status={zoneLook(z).status} label={t(zoneLook(z).label)} /></th>
                <td class="mono">
                  {intervals.map((i) => intervalText(i, metric.unit, app.locale, words)).join("; ")}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
        <p class="faint">
          {#if metric.source.url}
            <a href={metric.source.url} target="_blank" rel="noreferrer">{t("metric.source", { title: metric.source.title })}</a>
          {:else}
            {t("metric.source", { title: metric.source.title })}
          {/if}
        </p>
      </section>
    {/if}

    <section>
      <h2>{t("metric.this")}</h2>
      <ul>
        <li>{t("metric.inZone", { zone: t(look.label) })}</li>
        <li>
          {#if baseline}
            {t("metric.baseline", {
              low: formatValue(baseline.low, metric.unit, app.locale),
              high: formatValue(baseline.high, metric.unit, app.locale),
              flights: baseline.flights,
            })}
          {:else}
            {t("metric.noBaseline")}
          {/if}
        </li>
      </ul>
    </section>
  </div>
</div>

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    background: rgba(6, 10, 14, 0.6);
    display: grid;
    place-items: start center;
    padding: 40px 16px;
    overflow-y: auto;
    z-index: 20;
  }
  .dialog {
    width: min(620px, 100%);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 20px 22px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    box-sizing: border-box;
  }
  header {
    display: flex;
    justify-content: space-between;
    gap: 12px;
  }
  h1 {
    margin: 2px 0;
    font-size: 19px;
  }
  h2 {
    margin: 0 0 6px;
    font-size: 13px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  p,
  ul {
    margin: 0;
  }
  ul {
    padding-left: 18px;
  }
  .eyebrow,
  .faint {
    font-size: 12px;
    color: var(--text-faint);
  }
  .value {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .value strong {
    font-size: 24px;
  }
  table {
    border-collapse: collapse;
    margin-bottom: 6px;
  }
  th,
  td {
    text-align: left;
    padding: 4px 12px 4px 0;
    font-weight: normal;
  }
  a {
    color: var(--link);
  }
  .close {
    align-self: flex-start;
    background: none;
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text-2);
    padding: 6px 12px;
    cursor: pointer;
  }
</style>
