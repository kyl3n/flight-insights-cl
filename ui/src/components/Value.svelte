<script lang="ts">
  // The one way a measured value appears anywhere in the app (spec: Explanations for
  // every metric and data point). It reads value, unit and zone at a glance; hovering
  // or focusing it shows what it is and where it sits, and clicking pins the full
  // explanation (mock-ups MetricHover and MetricExplain).
  import { app, t } from "../lib/app.svelte";
  import { formatValue, zoneLook, type Baseline, type MetricInfo, type Zone } from "../lib/report";
  import { STATUS } from "../lib/status";
  import MetricExplain from "./MetricExplain.svelte";
  import ZoneBar from "./ZoneBar.svelte";

  let {
    id,
    metric,
    value,
    zone,
    context,
    loadBaseline,
  }: {
    id: string;
    metric: MetricInfo;
    value: number;
    zone: Zone | null;
    context: string;
    loadBaseline?: () => Promise<Baseline | null>;
  } = $props();

  const uid = $props.id();
  let hovering = $state(false);
  let pinned = $state(false);
  let baseline = $state<Baseline | null | undefined>(undefined);
  const look = $derived(zoneLook(zone));

  function show() {
    hovering = true;
    if (baseline === undefined && loadBaseline) {
      baseline = null; // asked once
      loadBaseline().then((b) => (baseline = b)).catch(() => (baseline = null));
    }
  }
</script>

<span class="wrap">
  <button
    type="button"
    class="value"
    aria-describedby={hovering ? `${uid}-card` : undefined}
    onmouseenter={show}
    onmouseleave={() => (hovering = false)}
    onfocus={show}
    onblur={() => (hovering = false)}
    onclick={() => {
      hovering = false;
      pinned = true;
    }}
  >
    <span class="name">
      <span class="glyph" style:color="var({STATUS[look.status].tone})" aria-label={t(look.label)}>
        {STATUS[look.status].glyph}
      </span>
      {metric.name[app.locale]}
    </span>
    <span class="number mono">{formatValue(value, metric.unit, app.locale)}</span>
  </button>
  {#if hovering}
    <span class="card" role="tooltip" id="{uid}-card">
      <span class="title">{metric.name[app.locale]}</span>
      <span class="line">
        <strong class="mono">{formatValue(value, metric.unit, app.locale)}</strong>
        <span style:color="var({STATUS[look.status].tone})">{STATUS[look.status].glyph} {t(look.label)}</span>
      </span>
      <span class="faint">{context}</span>
      <span class="definition">{metric.definition[app.locale]}</span>
      <ZoneBar {metric} {value} />
      {#if baseline}
        <span class="faint">
          {t("metric.usual", {
            low: formatValue(baseline.low, metric.unit, app.locale),
            high: formatValue(baseline.high, metric.unit, app.locale),
          })}
        </span>
      {/if}
      <span class="hint">{t("metric.click")}</span>
    </span>
  {/if}
</span>

{#if pinned}
  <MetricExplain {id} {metric} {value} {zone} {context} {baseline} onclose={() => (pinned = false)} />
{/if}

<style>
  .wrap {
    position: relative;
    display: inline-flex;
  }
  .value {
    display: inline-flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    min-width: 120px;
    padding: 8px 12px;
    background: var(--surface-2);
    border: 1px solid var(--border-soft);
    border-radius: 10px;
    color: var(--text);
    text-align: left;
    cursor: pointer;
  }
  .value:hover {
    border-color: var(--border);
  }
  .name {
    font-size: 12px;
    color: var(--text-muted);
  }
  .number {
    font-size: 17px;
  }
  .glyph {
    font-size: 10px;
    margin-right: 3px;
  }
  .card {
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    z-index: 15;
    width: 300px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 12px 14px;
    background: var(--raised);
    border: 1px solid var(--border);
    border-radius: 10px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
    font-size: 13px;
  }
  .title {
    font-weight: 600;
  }
  .line {
    display: flex;
    gap: 10px;
    align-items: baseline;
  }
  .line strong {
    font-size: 18px;
  }
  .definition {
    color: var(--text-2);
  }
  .faint,
  .hint {
    font-size: 12px;
    color: var(--text-faint);
  }
</style>
