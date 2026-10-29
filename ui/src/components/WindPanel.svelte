<script lang="ts">
  // Wind on the Range and speed screen (spec: Power and efficiency analysis, "Range,
  // endurance and speed"; mock-up Performance, "Wind"): the radius against wind speed, the
  // worst way round and across the wind; the radius in the wind picked by direction of
  // travel relative to the wind, never to a place; and the speed to fly into a headwind.
  import { app, t } from "../lib/app.svelte";
  import { plot, polar, split, type Performance } from "../lib/range";
  import { formatNumber, unitText } from "../lib/report";
  import type { MessageKey } from "../lib/i18n";

  let { view }: { view: Performance } = $props();

  const num = (v: number | null | undefined, digits = 1) =>
    v === null || v === undefined || !Number.isFinite(v) ? "–" : formatNumber(Math.round(v * 10 ** digits) / 10 ** digits, app.locale);
  const unit = (u: string) => unitText(u, app.locale);

  const F = { width: 640, height: 230, left: 50, right: 16, top: 16, bottom: 32 };
  const rw = $derived(view.radius_wind ?? []);
  const winds = $derived(rw.map((r) => r.wind_ms));
  const worstKm = $derived(rw.map((r) => r.worst_m / 1000));
  const acrossKm = $derived(rw.map((r) => (r.across_m === null ? null : r.across_m / 1000)));
  const p = $derived(plot(winds, [...worstKm, ...acrossKm.filter((v): v is number => v !== null)], F));
  const worstLine = $derived(split(winds, worstKm, Infinity, p).solid);
  const acrossLine = $derived(split(winds, acrossKm, Infinity, p).solid);
  const picked = $derived(view.radius);
  const most = $derived(picked ? Math.max(...picked.by_direction.map(([, m]) => m), 1) : 1);
  const trip = $derived(view.inputs?.trip ?? "out_and_back");
</script>

<section class="panel" aria-labelledby="c4-title">
  <div class="head">
    <h2 id="c4-title">{t("wind.title")}</h2>
    <span class="faint">{t("wind.about")}</span>
  </div>
  <div class="split">
    {#if rw.length > 1}
      <figure class="main">
        <figcaption>
          <span class="cap">{t("wind.radius", { trip: t(`range.trip.${trip}` as MessageKey) })}</span>
          <span><span aria-hidden="true" class="worst">━</span> {t("wind.worst")}</span>
          <span><span aria-hidden="true" class="across">┅</span> {t("wind.across")}</span>
        </figcaption>
        <svg viewBox="0 0 {F.width} {F.height}" role="img" aria-label={t("wind.radius.aria")}>
          {#each p.y.ticks as v (v)}
            <line class="grid" x1={F.left} x2={F.width - F.right} y1={p.py(v)} y2={p.py(v)} />
            <text class="tick" x={F.left - 8} y={p.py(v) + 4} text-anchor="end">{num(v, 1)}</text>
          {/each}
          {#each p.x.ticks as v (v)}
            <text class="tick" x={p.px(v)} y={F.height - F.bottom + 16} text-anchor="middle">{num(v, 0)}</text>
          {/each}
          <text class="tick" x={4} y={10}>{unit("km")}</text>
          <text class="tick" x={F.width - F.right} y={F.height - 2} text-anchor="end">{unit("m/s")}</text>
          <path class="line across" d={acrossLine} />
          <path class="line worst" d={worstLine} />
          {#if picked}
            <circle class="opt" cx={p.px(picked.wind_ms)} cy={p.py(picked.worst_m / 1000)} r="4.5" />
            <text class="tick strong" x={p.px(picked.wind_ms) + 8} y={p.py(picked.worst_m / 1000) + 18}>{num(picked.worst_m / 1000)} {unit("km")}</text>
          {/if}
        </svg>
      </figure>
    {/if}
    {#if picked && picked.by_direction.length}
      <figure class="polar">
        <figcaption><span class="cap">{t("wind.polar", { wind: num(picked.wind_ms, 0) })}</span></figcaption>
        <svg viewBox="0 0 320 240" role="img" aria-label={t("wind.polar.aria", { km: num(picked.worst_m / 1000) })}>
          <circle class="ring" cx="160" cy="120" r="90" />
          <circle class="ring" cx="160" cy="120" r="45" />
          <line class="grid" x1="160" y1="22" x2="160" y2="218" />
          <line class="grid" x1="62" y1="120" x2="258" y2="120" />
          <text class="tick" x="160" y="14" text-anchor="middle">{t("wind.polar.into")}</text>
          <text class="tick" x="160" y="236" text-anchor="middle">{t("wind.polar.with")}</text>
          <text class="tick" x="2" y="124">{t("wind.polar.across")}</text>
          <text class="tick" x="318" y="124" text-anchor="end">{t("wind.polar.across")}</text>
          <text class="tick" x="164" y="31">{num(most / 1000)} {unit("km")}</text>
          <path class="shape" d={polar(picked.by_direction, 160, 120, 90, most)} />
        </svg>
        <p class="faint">{t("wind.polar.note")}</p>
      </figure>
    {/if}
  </div>
  {#if view.headwind?.length}
    <div class="scroll">
      <h3>{t("wind.table", { kg: num(view.inputs?.mass_kg) })}</h3>
      <table>
        <thead>
          <tr>
            <th scope="col">{t("wind.col.headwind")}</th>
            <th scope="col">{t("wind.col.air")}</th>
            <th scope="col">{t("wind.col.ground")}</th>
            <th scope="col">{t("wind.col.energy")}</th>
          </tr>
        </thead>
        <tbody>
          {#each view.headwind as h (h.wind_ms)}
            <tr>
              <td class="mono">{num(h.wind_ms, 0)} {unit("m/s")}</td>
              {#if h.airspeed === null}
                <td colspan="3" class="faint">{t("wind.noWay")}</td>
              {:else}
                <td class="mono">{num(h.airspeed)} {unit("m/s")}</td>
                <td class="mono">{num(h.ground)} {unit("m/s")}</td>
                <td class="mono">{num(h.wh_per_km, 0)} {unit("W.h/km")}</td>
              {/if}
            </tr>
          {/each}
        </tbody>
      </table>
      <p class="faint">{t("wind.table.note")}</p>
    </div>
  {/if}
</section>

<style>
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
  h3 {
    margin: 0 0 6px;
    font-size: 13px;
  }
  p {
    margin: 0;
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
  }
  .mono {
    font-family: var(--mono);
  }
  .split {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    align-items: flex-start;
  }
  figure {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
  }
  .main {
    flex: 999 1 400px;
  }
  .main svg {
    max-width: 720px;
  }
  .polar {
    flex: 1 1 300px;
    max-width: 380px;
  }
  figcaption {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 14px;
    font-size: 12px;
    color: var(--text-muted);
  }
  .cap {
    color: var(--text);
    font-weight: 500;
  }
  svg {
    width: 100%;
    height: auto;
    display: block;
  }
  .grid {
    stroke: var(--border-soft);
  }
  .ring {
    fill: none;
    stroke: var(--border-soft);
  }
  .tick {
    fill: var(--text-faint);
    font-family: var(--mono);
    font-size: 11px;
  }
  .tick.strong {
    fill: var(--text);
  }
  .line {
    fill: none;
    stroke-width: 2;
  }
  .line.worst,
  .worst {
    stroke: var(--accent);
    color: var(--accent);
  }
  .line.across {
    stroke: var(--text-faint);
    stroke-dasharray: 5 4;
  }
  .across {
    color: var(--text-faint);
  }
  .opt {
    fill: var(--surface);
    stroke: var(--text);
    stroke-width: 1.8;
  }
  .shape {
    fill: color-mix(in srgb, var(--accent) 18%, transparent);
    stroke: var(--accent);
    stroke-width: 2;
  }
  .scroll {
    overflow-x: auto;
  }
  table {
    border-collapse: collapse;
    font-size: 13px;
    width: 100%;
  }
  th {
    text-align: left;
    font-weight: 500;
    color: var(--text-muted);
    padding: 6px 12px 6px 0;
  }
  td.mono {
    white-space: nowrap;
  }
  td {
    padding: 6px 12px 6px 0;
    border-top: 1px solid var(--border-soft);
  }
</style>
