<script lang="ts">
  // Over time on the Range and speed screen (spec: Power and efficiency analysis, "Range,
  // endurance and speed"; mock-up Performance, "Over time"): one point per flight for hover
  // grams per watt, hover figure of merit and energy per km, all at the weight picked in
  // standard air, so flights flown heavier, in thinner air or at other speeds line up; the
  // changes between flights on the same axis; and the flights that drew too much named.
  import { app, t } from "../lib/app.svelte";
  import { axis } from "../lib/energy";
  import type { MessageKey } from "../lib/i18n";
  import { overTimeSeries, type OverTime, type OverTimeChange, type OverTimeKey, type Performance } from "../lib/range";
  import { formatNumber, unitText } from "../lib/report";

  let { view }: { view: Performance } = $props();

  const num = (v: number | null | undefined, digits = 1) =>
    v === null || v === undefined || !Number.isFinite(v) ? "–" : formatNumber(Math.round(v * 10 ** digits) / 10 ** digits, app.locale);
  const unit = (u: string) => unitText(u, app.locale);

  const ot = $derived(view.over_time as OverTime);
  const W = 900, LEFT = 96, RIGHT = 56, LANE = 74, GAP = 22, TOP = 14;
  const LANES: { key: OverTimeKey; label: MessageKey; digits: number }[] = [
    { key: "grams_per_watt", label: "overtime.gw", digits: 1 },
    { key: "figure_of_merit", label: "overtime.fm", digits: 2 },
    { key: "wh_per_km", label: "overtime.wh", digits: 0 },
  ];
  const lanes = $derived(
    LANES.map((l) => ({ ...l, points: overTimeSeries(ot, l.key) }))
      .filter((l) => l.points.length > 0)
      .map((l, i) => {
        const y0 = TOP + i * (LANE + GAP);
        const a = axis(
          l.points.map((p) => p.v),
          2,
        );
        const py = (v: number) => y0 + LANE - ((v - a.lo) / (a.hi - a.lo)) * LANE;
        // as many decimals as the tick step needs, so close ticks never read the same
        const step = a.ticks.length > 1 ? a.ticks[1]! - a.ticks[0]! : 1;
        const tickDigits = Math.min(3, Math.max(0, Math.ceil(-Math.log10(step))));
        return { ...l, y0, a, py, tickDigits };
      }),
  );
  const changesY = $derived(TOP + lanes.length * (LANE + GAP) + 6);
  const H = $derived(changesY + 40);
  const px = (n: number) => LEFT + ((n - 0.5) / Math.max(ot.count, 1)) * (W - LEFT - RIGHT);
  // a change sits just before the flight it first applies to
  const at = (n: number) => px(n - 0.5);
  const xTicks = $derived.by(() => {
    const step = Math.max(1, Math.ceil(ot.count / 10));
    const out: number[] = [];
    for (let n = 1; n <= ot.count; n += step) out.push(n);
    return out;
  });
  const line = (pts: { n: number; v: number }[], py: (v: number) => number) => pts.map((p, i) => `${i ? "L" : "M"}${px(p.n).toFixed(1)} ${py(p.v).toFixed(1)}`).join(" ");

  function changeText(c: OverTimeChange): string {
    switch (c.kind) {
      case "build":
        return t("trends.event.build", { number: c.number ?? "", reason: c.reason ?? "" });
      case "firmware":
        return t("trends.event.firmware", { from: c.from ?? "", to: c.to ?? "" });
      case "ground station change":
        return t("trends.event.station");
      default:
        return t("trends.event.parameter", { origin: c.origin ?? "", params: (c.params ?? []).join(", ") });
    }
  }
  const builds = $derived(ot.changes.filter((c) => c.kind === "build"));
  const firmware = $derived(ot.changes.filter((c) => c.kind === "firmware"));
  const others = $derived(ot.changes.filter((c) => c.kind !== "build" && c.kind !== "firmware"));
  const flagged = $derived(
    ot.flights.flatMap((f) =>
      f.flagged.map((phase) => ({ n: f.n, phase, pct: (((phase === "hover" ? f.hover : f.level) ?? 1) - 1) * 100 })),
    ),
  );
  const threshold = $derived(Math.max(ot.thresholds.hover ?? 0, ot.thresholds.level ?? 0));
</script>

{#if ot.flights.length}
  <section class="panel" aria-labelledby="c7-title">
    <div class="head">
      <h2 id="c7-title">{t("overtime.title")}</h2>
      <span class="faint">{t("overtime.about", { kg: num(ot.mass_kg) })}</span>
    </div>
    <figure>
      <figcaption>
        <span><span aria-hidden="true" class="dot">●</span> {t("overtime.key.flight")}</span>
        <span><span aria-hidden="true" class="dot warn">●</span> {t("overtime.key.flagged")}</span>
        <span><span aria-hidden="true" class="key-build">┊</span> {t("overtime.key.build")}</span>
        <span><span aria-hidden="true" class="dot change">●</span> {t("overtime.key.params")}</span>
        <span><span aria-hidden="true" class="dot change">◆</span> {t("overtime.key.firmware")}</span>
      </figcaption>
      <div class="scroll">
        <svg viewBox="0 0 {W} {H}" role="img" aria-label={t("overtime.aria", { n: ot.flights.length, flagged: flagged.length })}>
          {#each lanes as l (l.key)}
            <text class="label" x="0" y={l.y0 + 12}>{t(l.label)}</text>
            {#each l.a.ticks as v (v)}
              <line class="grid" x1={LEFT} x2={W - RIGHT} y1={l.py(v)} y2={l.py(v)} />
              <text class="tick" x={W - RIGHT + 8} y={l.py(v) + 4}>{num(v, l.tickDigits)}</text>
            {/each}
            <path class="line" d={line(l.points, l.py)} />
            {#each l.points as p (p.n)}
              <circle class="pt" class:warn={p.flagged} cx={px(p.n)} cy={l.py(p.v)} r="3.5">
                <title>{t("overtime.flight", { n: p.n })}: {num(p.v, l.digits)}</title>
              </circle>
            {/each}
          {/each}
          {#each builds as b (b.n)}
            <line class="build" x1={at(b.n)} x2={at(b.n)} y1={TOP - 4} y2={changesY} />
            <rect class="build-mark" x={at(b.n) - 6} y={changesY - 6} width="12" height="12" rx="2"><title>{changeText(b)}</title></rect>
          {/each}
          <line class="axis" x1={LEFT} x2={W - RIGHT} y1={changesY} y2={changesY} />
          <text class="label" x="0" y={changesY + 4}>{t("overtime.changes")}</text>
          {#each others as c, i (i)}
            <circle class="change" cx={at(c.n)} cy={changesY} r="5"><title>{changeText(c)}</title></circle>
          {/each}
          {#each firmware as c (c.n)}
            {@const x = at(c.n)}
            <path class="change" d="M{x} {changesY - 7} L{x + 7} {changesY} L{x} {changesY + 7} L{x - 7} {changesY} Z"><title>{changeText(c)}</title></path>
          {/each}
          {#each xTicks as n (n)}
            <text class="tick" x={px(n)} y={H - 6} text-anchor="middle">{n}</text>
          {/each}
        </svg>
      </div>
    </figure>
    {#each flagged as f (`${f.n}-${f.phase}`)}
      <p class="notice">
        ▲ {t(`overtime.flagged.${f.phase}` as MessageKey, { n: f.n, pct: num(f.pct, 0) })}
        <span class="faint">{t(`overtime.why.${f.phase}` as MessageKey)}</span>
      </p>
    {/each}
    <p class="faint">
      {t("overtime.note", { threshold: num(threshold * 100, 0) })}
      {#if ot.airspeed_ms !== null}{t("overtime.speed", { v: num(ot.airspeed_ms), unit: unit("m/s") })}{/if}
    </p>
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
  .notice {
    font-size: 13px;
    color: var(--caution);
  }
  figure {
    margin: 0;
    min-width: 0;
  }
  figcaption {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 14px;
    font-size: 12px;
    color: var(--text-muted);
  }
  .scroll {
    position: relative;
    overflow-x: auto;
  }
  svg {
    display: block;
    width: 100%;
    min-width: 560px;
    height: auto;
  }
  .dot {
    color: var(--text);
  }
  .dot.warn {
    color: var(--caution);
  }
  .dot.change {
    color: var(--text-faint);
  }
  .key-build {
    color: var(--accent);
  }
  .grid {
    stroke: var(--border-soft);
  }
  .axis {
    stroke: var(--border);
  }
  .label {
    fill: var(--text-muted);
    font-size: 11px;
  }
  .tick {
    fill: var(--text-faint);
    font-size: 10px;
    font-family: var(--mono);
  }
  .line {
    fill: none;
    stroke: var(--text-faint);
    stroke-width: 1.2;
  }
  .pt {
    fill: var(--text);
  }
  .pt.warn {
    fill: var(--caution);
  }
  .build {
    stroke: var(--accent);
    stroke-width: 1.4;
    stroke-dasharray: 4 3;
  }
  .build-mark {
    fill: var(--accent);
  }
  .change {
    fill: var(--text-faint);
  }
</style>
