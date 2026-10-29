<script lang="ts">
  // An airframe's range, flight time and speeds (spec: Power and efficiency analysis,
  // "Range, endurance and speed"; mock-up Performance): pick the take-off weight, the air,
  // the trip and the wind, and see the figures with their 95% intervals, and how range and
  // flight time fall as the weight rises. The engine works them out on this laptop from
  // the airframe's own flights; nothing on this screen is sent anywhere.
  import { app, t } from "../lib/app.svelte";
  import type { MessageKey } from "../lib/i18n";
  import {
    airframePerformance,
    earlyEnds,
    flownSpan,
    interval,
    measuredAt,
    TRIPS,
    weightChart,
    weights,
    WINDS,
    type Air,
    type Figures,
    type Performance,
    type Trip,
  } from "../lib/range";
  import { formatNumber, unitText } from "../lib/report";
  import BatteryPanel from "./BatteryPanel.svelte";
  import BuildsPanel from "./BuildsPanel.svelte";
  import BuildUpPanel from "./BuildUpPanel.svelte";
  import DataPanel from "./DataPanel.svelte";
  import DensityPanel from "./DensityPanel.svelte";
  import OverTimePanel from "./OverTimePanel.svelte";
  import RecommendationsPanel from "./RecommendationsPanel.svelte";
  import SpeedPanel from "./SpeedPanel.svelte";
  import WhatIfPanel from "./WhatIfPanel.svelte";
  import WindPanel from "./WindPanel.svelte";

  let { airframeId }: { airframeId: string } = $props();

  let massKg = $state<number | null>(null);
  let air = $state<Air>("usual");
  let trip = $state<Trip>("out_and_back");
  let windMs = $state(5);
  let view = $state<Performance | null>(null);
  let error = $state("");
  let busy = $state(false);
  let reloads = $state(0); // a setup answer changed: work the figures out again

  $effect(() => {
    void airframeId;
    massKg = null;
  });

  $effect(() => {
    const asked = { mass_kg: massKg, air, trip, wind_ms: windMs };
    void reloads;
    busy = true;
    error = "";
    airframePerformance(airframeId, asked)
      .then((v) => (view = v))
      .catch((e: unknown) => (error = String(e)))
      .finally(() => (busy = false));
  });

  const num = (v: number | null | undefined, digits = 1) =>
    v === null || v === undefined || !Number.isFinite(v) ? "–" : formatNumber(Math.round(v * 10 ** digits) / 10 ** digits, app.locale);
  const unit = (u: string) => unitText(u, app.locale);
  const km = (m: number) => num(m / 1000, 1);
  const min = (s: number) => num(s / 60, 1);

  const inputs = $derived(view?.inputs);
  const span = $derived(flownSpan(inputs));
  const choices = $derived(weights(inputs));
  const f = $derived(view?.figures ?? null);
  const kg = $derived(inputs?.mass_kg ?? null);
  const inside = $derived(measuredAt(kg, span));
  const early = $derived(earlyEnds(view?.ends));
  const ends = (which: "hover" | "endurance" | "range") => t(`range.ends.${view?.ends?.[which] ?? "reserve"}` as MessageKey);

  const FRAME = { width: 900, height: 220, left: 56, right: 20, top: 16, bottom: 34 };
  const rangeChart = $derived(view?.against_weight ? weightChart(view.against_weight, (r: Figures) => r.range_m / 1000, FRAME) : null);
  const timeChart = $derived(view?.against_weight ? weightChart(view.against_weight, (r: Figures) => r.endurance_s / 60, FRAME) : null);
  const markers = $derived(
    [
      inputs?.empty_kg != null ? { kg: inputs.empty_kg, label: t("range.weight.empty") } : null,
      inputs?.max_kg != null ? { kg: inputs.max_kg, label: t("range.weight.max") } : null,
    ].filter((m): m is { kg: number; label: string } => m !== null),
  );
</script>

<div class="stack">
  <section class="panel controls" aria-label={t("range.controls")}>
    <div class="control">
      <span class="label">{t("range.weight")}</span>
      <div class="chips" role="group" aria-label={t("range.weight")}>
        {#each choices as c (c.role + (c.kg ?? "auto"))}
          <button type="button" aria-pressed={massKg === c.kg} onclick={() => (massKg = c.kg)}>
            {#if c.kg === null}{t("range.weight.typical", { kg: num(kg) })}{:else}{t(`range.weight.${c.role}.kg` as MessageKey, { kg: num(c.kg) })}{/if}
          </button>
        {/each}
      </div>
    </div>
    <div class="control">
      <span class="label">{t("range.air")}</span>
      <div class="chips" role="group" aria-label={t("range.air")}>
        {#each ["usual", "standard"] as const as a (a)}
          <button type="button" aria-pressed={air === a} onclick={() => (air = a)}>{t(`range.air.${a}`)}</button>
        {/each}
      </div>
    </div>
    <div class="control">
      <span class="label">{t("range.trip")}</span>
      <div class="chips" role="group" aria-label={t("range.trip")}>
        {#each TRIPS as tr (tr)}
          <button type="button" aria-pressed={trip === tr} onclick={() => (trip = tr)}>{t(`range.trip.${tr}` as MessageKey)}</button>
        {/each}
      </div>
    </div>
    <div class="control">
      <span class="label">{t("range.wind")}</span>
      <div class="chips" role="group" aria-label={t("range.wind")}>
        {#each WINDS as w (w)}
          <button type="button" aria-pressed={windMs === w} onclick={() => (windMs = w)}>{num(w, 0)} {unit("m/s")}</button>
        {/each}
      </div>
    </div>
    <p class="faint foot">
      <span>{inputs ? t("range.inputs", { kg: num(kg), from: inputs.mass_from, ratio: num(inputs.density / 1.225, 2) }) : ""}</span>
      <span>{t("range.private")}</span>
    </p>
  </section>

  {#if error}<p class="error">{error}</p>{/if}
  {#if busy && !view}<p class="faint">{t("range.working")}</p>{/if}

  {#if view}
    {#if view.level && view.level !== "A"}
      <div class="notice">
        <p class="kicker">◆ {t("range.estimates")}</p>
        <p>{t(`range.level.${view.level}` as MessageKey)}</p>
      </div>
    {/if}

    {#if view.why || Object.keys(view.missing).length}
      <section class="panel" aria-labelledby="need-title">
        <h2 id="need-title">{t("range.needs")}</h2>
        <ul>
          {#if view.why}<li>{view.why}</li>{/if}
          {#each Object.entries(view.missing) as [k, why] (k)}<li>{why}</li>{/each}
        </ul>
      </section>
    {/if}

    <RecommendationsPanel {airframeId} />

    {#if view.hover && !f}
      <section class="panel" aria-labelledby="kpi-title">
        <h2 id="kpi-title">{t("range.kpi", { kg: num(view.hover.mass_kg) })}</h2>
        <div class="tiles">
          {@render tile(t("range.tile.hover"), min(view.hover.hover_s.median), unit("min"), interval(view.hover.hover_s, min), "", view.hover.measured)}
        </div>
        <p class="faint">{t("range.hoverOnly")}</p>
      </section>
    {/if}

    {#if f}
      {@const sp = view.spread ?? {}}
      {@const ts = view.top_speed}
      <section class="panel" aria-labelledby="kpi-title">
        <div class="head">
          <h2 id="kpi-title">{t("range.kpi", { kg: num(f.mass_kg) })}</h2>
          <span class="faint">{t(inside ? "range.kpi.inside" : "range.kpi.outside")}</span>
        </div>
        <div class="tiles">
          {@render tile(t("range.tile.hover"), min(f.hover_s), unit("min"), interval(sp.hover_s, min), ends("hover"), inside)}
          {@render tile(t("range.tile.endurance"), min(f.endurance_s), unit("min"), interval(sp.endurance_s, min), `${t("range.at", { speed: num(f.endurance_ms) })} · ${ends("endurance")}`, inside)}
          {@render tile(t("range.tile.range"), km(f.range_m), unit("km"), interval(sp.range_m, km), `${t("range.tile.range.note", { speed: num(f.range_ms) })} · ${ends("range")}`, inside)}
          {#if view.radius}
            {@render tile(
              t("range.tile.radius"),
              km(view.radius.worst_m),
              unit("km"),
              null,
              `${t("range.tile.radius.note", { trip: t(`range.trip.${trip}` as MessageKey), wind: num(view.radius.wind_ms, 0) })}${
                f.radius_m[trip] !== undefined ? ` · ${t("range.tile.radius.still", { km: km(f.radius_m[trip]!) })}` : ""
              }`,
              inside,
            )}
          {/if}
          {#if ts}
            {@render tile(t("range.tile.top"), num(ts.airspeed), unit("m/s"), null, t(`range.tile.top.by.${ts.by}` as MessageKey), !ts.extrapolated)}
          {/if}
          {#if view.speeds}
            {@render tile(t("range.tile.rangeSpeed"), num(view.speeds.range), unit("m/s"), view.speeds.range_band.map((v) => num(v)).join("–"), t("range.tile.rangeSpeed.note"), inside)}
            {@render tile(
              t("range.tile.enduranceSpeed"),
              num(view.speeds.endurance),
              unit("m/s"),
              view.speeds.endurance_band.map((v) => num(v)).join("–"),
              t("range.tile.enduranceSpeed.note", { kw: num(view.speeds.endurance_w / 1000, 2) }),
              inside,
            )}
          {/if}
          {@render tile(t("range.tile.fm"), num(f.efficiency.figure_of_merit, 2), "FM", null, t("range.tile.fm.note", { gw: num(f.efficiency.grams_per_watt) }), inside)}
          {@render tile(t("range.tile.perKm"), num(f.efficiency.wh_per_km, 0), unit("W.h/km"), null, t("range.at.still", { speed: num(f.range_ms) }), inside)}
          {@render tile(t("range.tile.perKgKm"), num(f.efficiency.wh_per_kg_km, 2), unit("W.h/(kg.km)"), null, t("range.at", { speed: num(f.range_ms) }), inside)}
        </div>
        {#if early.length && view.failsafe}
          {@const fs = view.failsafe}
          <div class="callout">
            <p class="kicker">▲ {t("range.early")}</p>
            {#if early.includes("capacity_failsafe") && fs.capacity_mah !== null}
              <p>{t("range.early.capacity", { cap: num(fs.capacity_mah, 0), low: num(fs.low_mah ?? 0, 0), left: num((fs.capacity_mah - (fs.low_mah ?? 0)) / 1000, 2) })}</p>
            {/if}
            {#if early.includes("voltage_failsafe") && fs.low_volt !== null}
              <p>{t("range.early.voltage", { volt: num(fs.low_volt, 1), timer: num(fs.timer_s, 0) })}</p>
            {/if}
            <p class="faint">{t("range.early.fix")}</p>
          </div>
        {/if}
        <div class="reserve">
          <p class="cap">{t("range.reserve")}</p>
          <p>{t("range.reserve.text", { pct: num((inputs?.reserve_share ?? 0) * 100, 0), source: view.sources.reserve ?? "" })}</p>
          {#if view.sources.pack}<p class="faint">{t("range.reserve.pack", { pack: view.sources.pack })}</p>{/if}
        </div>
      </section>
    {/if}

    {#if view.curves && f}
      <SpeedPanel {view} />
    {/if}

    {#if f}
      <BuildUpPanel {view} {airframeId} onchange={() => (reloads += 1)} />
      <WindPanel {view} />
      <BatteryPanel {view} />
      <WhatIfPanel {view} />
      {#if view.builds}<BuildsPanel {view} onchange={() => (reloads += 1)} />{/if}
      <DensityPanel {view} />
      {#if view.over_time}<OverTimePanel {view} />{/if}
    {/if}

    {#if rangeChart && timeChart}
      <section class="panel" aria-labelledby="c1-title">
        <div class="head">
          <h2 id="c1-title">{t("range.weightChart")}</h2>
          <span class="faint">{t("range.weightChart.about")}</span>
        </div>
        {@render chart(rangeChart, t("range.weightChart.range"), unit("km"), t("range.weightChart.range.aria"))}
        {@render chart(timeChart, t("range.weightChart.time"), unit("min"), t("range.weightChart.time.aria"))}
        <p class="faint">
          {#if span}{t("range.weightChart.flown", { lo: num(span[0]), hi: num(span[1]) })}{/if}
          {t("range.weightChart.note")}
        </p>
      </section>
    {/if}
    <DataPanel {view} />
  {/if}
</div>

{#snippet tile(label: string, value: string, u: string, band: string | null, note: string, measured: boolean)}
  <div class="tile">
    <div class="tile-head">
      <span class="faint">{label}</span>
      <span class="badge" class:measured>{t(measured ? "range.measured" : "range.estimate")}</span>
    </div>
    <p><span class="big mono">{value}</span> <span class="faint">{u}</span></p>
    {#if band}<p class="faint mono">{t("range.band", { band })}</p>{/if}
    {#if note}<p class="faint">{note}</p>{/if}
  </div>
{/snippet}

{#snippet chart(c: NonNullable<typeof rangeChart>, title: string, u: string, aria: string)}
  <figure>
    <figcaption><span class="cap">{title}</span></figcaption>
    <svg viewBox="0 0 {FRAME.width} {FRAME.height}" role="img" aria-label={aria}>
      {#if span}
        {@const a = Math.max(c.px(span[0]), FRAME.left)}
        {@const b = Math.min(c.px(span[1]), FRAME.width - FRAME.right)}
        {#if a > FRAME.left}<rect class="outside" x={FRAME.left} y={FRAME.top} width={a - FRAME.left} height={FRAME.height - FRAME.top - FRAME.bottom} />{/if}
        {#if b < FRAME.width - FRAME.right}<rect class="outside" x={b} y={FRAME.top} width={FRAME.width - FRAME.right - b} height={FRAME.height - FRAME.top - FRAME.bottom} />{/if}
      {/if}
      {#each c.y.ticks as v (v)}
        <line class="grid" x1={FRAME.left} x2={FRAME.width - FRAME.right} y1={c.py(v)} y2={c.py(v)} />
        <text class="tick" x={FRAME.left - 8} y={c.py(v) + 4} text-anchor="end">{num(v, 1)}</text>
      {/each}
      {#each c.x.ticks as v (v)}
        <text class="tick" x={c.px(v)} y={FRAME.height - FRAME.bottom + 18} text-anchor="middle">{num(v, 1)}</text>
      {/each}
      <text class="tick" x={FRAME.left - 8} y={10} text-anchor="end">{u}</text>
      <text class="tick" x={FRAME.width - FRAME.right} y={FRAME.height - 2} text-anchor="end">{unit("kg")}</text>
      {#each markers as m (m.label)}
        {#if m.kg >= c.x.lo && m.kg <= c.x.hi}
          <line class="marker" x1={c.px(m.kg)} x2={c.px(m.kg)} y1={FRAME.top} y2={FRAME.height - FRAME.bottom} />
          {@const right = c.px(m.kg) > FRAME.width - FRAME.right - 160}
          <text class="tick" x={c.px(m.kg) + (right ? -4 : 4)} y={FRAME.top + 10} text-anchor={right ? "end" : "start"}>{m.label} · {num(m.kg)}</text>
        {/if}
      {/each}
      <path class="line" d={c.line} />
      {#each c.points as p (p.kg)}
        <circle class="point" class:chosen={kg !== null && Math.abs(p.kg - kg) < 0.05} cx={p.x} cy={p.y} r="3.5">
          <title>{num(p.kg)} {unit("kg")}</title>
        </circle>
      {/each}
    </svg>
  </figure>
{/snippet}

<style>
  .stack {
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-width: 0;
  }
  .panel {
    display: flex;
    flex-direction: column;
    gap: 12px;
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
  }
  ul {
    margin: 0;
    padding-left: 18px;
    font-size: 13px;
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
  }
  .mono {
    font-family: var(--mono);
  }
  .error {
    color: var(--out);
  }
  .controls {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 480px), 1fr));
    gap: 10px 24px;
  }
  .control {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
  }
  .label {
    flex: 0 0 90px;
    font-size: 13px;
    color: var(--text-muted);
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .chips button {
    padding: 5px 10px;
    border-radius: 7px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text-muted);
    font: inherit;
    font-size: 13px;
    cursor: pointer;
  }
  .chips button[aria-pressed="true"] {
    border-color: color-mix(in srgb, var(--accent) 50%, var(--border));
    background: color-mix(in srgb, var(--accent) 14%, transparent);
    color: var(--text);
  }
  .foot {
    grid-column: 1 / -1;
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 6px 16px;
    border-top: 1px solid var(--border-soft);
    padding-top: 10px;
  }
  .notice {
    padding: 12px 14px;
    border-radius: 10px;
    border: 1px solid var(--border);
    background: color-mix(in srgb, var(--info) 8%, var(--surface));
    font-size: 13px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .kicker {
    font-weight: 600;
    font-size: 12px;
  }
  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 10px;
  }
  .tile {
    padding: 12px 14px;
    border: 1px solid var(--border-soft);
    border-radius: 10px;
    background: var(--surface-2);
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 13px;
  }
  .tile-head {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    align-items: baseline;
  }
  .badge {
    font-size: 11px;
    padding: 1px 7px;
    border-radius: 999px;
    color: var(--text-muted);
    background: var(--raised);
    white-space: nowrap;
  }
  .badge.measured {
    color: var(--ok);
    background: color-mix(in srgb, var(--ok) 14%, transparent);
  }
  .big {
    font-size: 20px;
    font-weight: 500;
  }
  .callout {
    padding: 12px 14px;
    border: 1px solid color-mix(in srgb, var(--caution) 55%, transparent);
    background: color-mix(in srgb, var(--caution) 8%, var(--surface-2));
    border-radius: 10px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 13px;
  }
  .callout .kicker {
    color: var(--caution);
  }
  .reserve {
    padding: 12px 14px;
    border: 1px solid var(--border-soft);
    border-radius: 10px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 13px;
  }
  .cap {
    font-weight: 600;
    font-size: 13px;
  }
  figure {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
  }
  figcaption {
    font-size: 12px;
  }
  svg {
    width: 100%;
    max-width: 1000px;
    height: auto;
    display: block;
  }
  .grid {
    stroke: var(--border-soft);
  }
  .tick {
    fill: var(--text-faint);
    font-family: var(--mono);
    font-size: 11px;
  }
  .outside {
    fill: var(--text-faint);
    fill-opacity: 0.07;
  }
  .marker {
    stroke: var(--text-faint);
    stroke-dasharray: 3 3;
  }
  .line {
    fill: none;
    stroke: var(--accent);
    stroke-width: 2;
  }
  .point {
    fill: var(--surface);
    stroke: var(--accent);
    stroke-width: 1.5;
  }
  .point.chosen {
    fill: var(--accent);
  }
</style>
