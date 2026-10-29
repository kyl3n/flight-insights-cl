<script lang="ts">
  // Density altitude on the Range and speed screen (spec: Power and efficiency analysis,
  // "Range, endurance and speed"; mock-up Performance, "Density altitude"): range and hover
  // endurance against density altitude, at the weight picked. Worked out and kept on this
  // laptop: the density altitudes flown are near the field's elevation, so never sent.
  import { app, t } from "../lib/app.svelte";
  import { againstSeaLevel, plot, type Performance } from "../lib/range";
  import { formatNumber, unitText } from "../lib/report";

  let { view }: { view: Performance } = $props();

  const num = (v: number | null | undefined, digits = 1) =>
    v === null || v === undefined || !Number.isFinite(v) ? "–" : formatNumber(Math.round(v * 10 ** digits) / 10 ** digits, app.locale);
  const pct = (v: number | null | undefined) => (v === null || v === undefined ? "–" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${num(Math.abs(v * 100), 0)}%`);
  const unit = (u: string) => unitText(u, app.locale);

  const F = { width: 640, height: 220, left: 54, right: 16, top: 16, bottom: 32 };
  const d = $derived(view.density);
  const s = $derived(d ? againstSeaLevel(d) : null);
  const p = $derived(s ? plot(s.altitude, [...s.range, ...s.hover, 0].filter((v): v is number => v !== null).map((v) => v * 100), F, null) : null);

  // solid inside the density altitudes flown, dashed outside
  function path(ys: (number | null)[], inside: boolean): string {
    if (!s || !p || !d) return "";
    const [lo, hi] = d.flown_m ?? [Infinity, -Infinity];
    let out = "";
    let pen = false;
    s.altitude.forEach((x, i) => {
      const y = ys[i];
      const within = x >= lo - 125 && x <= hi + 125;
      if (y === null || y === undefined || (inside && !within)) {
        pen = false;
        return;
      }
      out += `${pen ? "L" : "M"}${p.px(x).toFixed(1)} ${p.py(y * 100).toFixed(1)} `;
      pen = true;
    });
    return out;
  }
  const at = (h: number) => s?.altitude.indexOf(h) ?? -1;
  const at2000 = $derived(at(2000));
  // the airframe's usual air, read between the grid's altitudes
  const usual = $derived.by(() => {
    if (!s || !d || d.usual_m === null) return null;
    const x = d.usual_m;
    for (let i = 1; i < s.altitude.length; i++) {
      const a = s.altitude[i - 1]!;
      const b = s.altitude[i]!;
      const ya = s.range[i - 1];
      const yb = s.range[i];
      if (x >= a && x <= b && ya !== null && ya !== undefined && yb !== null && yb !== undefined) return { x, y: ya + ((x - a) / (b - a)) * (yb - ya) };
    }
    return null;
  });
</script>

{#if d && s && p}
  <section class="panel" aria-labelledby="c9-title">
    <div class="head">
      <h2 id="c9-title">{t("density.title", { kg: num(view.inputs?.mass_kg) })}</h2>
      <span class="faint">{t("density.about")}</span>
    </div>
    <figure>
      <figcaption>
        <span class="cap">{t("density.cap")}</span>
        <span><span aria-hidden="true" class="range">━</span> {t("density.range")}</span>
        <span><span aria-hidden="true" class="hover">━</span> {t("density.hover")}</span>
        {#if d.flown_m}
          <span><span aria-hidden="true" class="faint">┅</span> {t("density.beyond", { lo: num(d.flown_m[0], 0), hi: num(d.flown_m[1], 0) })}</span>
        {/if}
      </figcaption>
      <svg viewBox="0 0 {F.width} {F.height}" role="img" aria-label={t("density.aria", { r: pct(s.range[at2000]), h: pct(s.hover[at2000]) })}>
        {#if d.flown_m}
          <rect class="flown" x={p.px(Math.max(d.flown_m[0], p.x.lo))} y={F.top} width={Math.max(2, p.px(Math.min(d.flown_m[1], p.x.hi)) - p.px(Math.max(d.flown_m[0], p.x.lo)))} height={F.height - F.top - F.bottom} />
        {/if}
        {#each p.y.ticks as v (v)}
          <line class="grid" class:zero={v === 0} x1={F.left} x2={F.width - F.right} y1={p.py(v)} y2={p.py(v)} />
          <text class="tick" x={F.left - 8} y={p.py(v) + 4} text-anchor="end">{v > 0 ? "+" : ""}{num(v, 0)}%</text>
        {/each}
        {#each p.x.ticks as v (v)}
          <text class="tick" x={p.px(v)} y={F.height - F.bottom + 16} text-anchor="middle">{num(v, 0)}</text>
        {/each}
        <text class="tick" x={F.width - F.right} y={F.height - 2} text-anchor="end">{unit("m")}</text>
        <path class="line range dash" d={path(s.range, false)} />
        <path class="line hover dash" d={path(s.hover, false)} />
        <path class="line range" d={path(s.range, true)} />
        <path class="line hover" d={path(s.hover, true)} />
        {#if usual}
          <circle class="opt range" cx={p.px(usual.x)} cy={p.py(usual.y * 100)} r="4.5" />
          <text class="tick strong" x={p.px(usual.x) + 10} y={p.py(usual.y * 100) - 10}>{t("density.usual", { m: num(usual.x, 0) })}</text>
        {/if}
      </svg>
    </figure>
    {#if at2000 >= 0}
      <p class="faint">
        {t("density.note", {
          h: num((d.rows[at2000]?.hover_s ?? 0) / 60),
          dh: pct(s.hover[at2000]),
          r: num((d.rows[at2000]?.range_m ?? 0) / 1000),
          dr: pct(s.range[at2000]),
        })}
      </p>
    {/if}
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
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
  }
  figure {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
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
    max-width: 900px;
    height: auto;
    display: block;
  }
  .grid {
    stroke: var(--border-soft);
  }
  .grid.zero {
    stroke: var(--border);
  }
  .flown {
    fill: color-mix(in srgb, var(--accent) 8%, transparent);
  }
  .tick {
    fill: var(--text-faint);
    font-family: var(--mono);
    font-size: 11px;
  }
  .tick.strong {
    fill: var(--text-2);
    font-family: var(--font);
  }
  .line {
    fill: none;
    stroke-width: 2.2;
  }
  .line.dash {
    stroke-width: 1.6;
    stroke-dasharray: 5 4;
  }
  .line.range,
  .range {
    stroke: var(--accent);
    color: var(--accent);
  }
  .line.hover,
  .hover {
    stroke: var(--link);
    color: var(--link);
  }
  .line.hover {
    stroke: var(--ai);
  }
  .hover {
    color: var(--ai);
  }
  .opt {
    fill: var(--surface);
    stroke-width: 2;
  }
</style>
