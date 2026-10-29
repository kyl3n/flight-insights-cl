<script lang="ts">
  // Power and speed on the Range and speed screen (spec: Power and efficiency analysis,
  // "Range, endurance and speed"; mock-up Performance, "Power and speed"): the airframe's
  // electrical power against airspeed at each weight, with its 95% band and the flights'
  // steady points; the best-endurance and best-range speeds on it; and, at the weight
  // picked, the energy per km over the ground into each headwind and the flight time. The
  // curves run past the speeds flown, and are dashed and shaded there.
  import { app, t } from "../lib/app.svelte";
  import { at, band, least, plot, split, type Performance } from "../lib/range";
  import { formatNumber, unitText } from "../lib/report";

  let { view }: { view: Performance } = $props();

  const num = (v: number | null | undefined, digits = 1) =>
    v === null || v === undefined || !Number.isFinite(v) ? "–" : formatNumber(Math.round(v * 10 ** digits) / 10 ** digits, app.locale);
  const unit = (u: string) => unitText(u, app.locale);

  const P = { width: 900, height: 300, left: 56, right: 20, top: 28, bottom: 34 };
  const E = { width: 640, height: 220, left: 56, right: 20, top: 16, bottom: 30 };
  const HEADWIND_COLOURS = ["var(--text)", "var(--accent)", "#2F7FB8"];

  const c = $derived(view.curves!);
  const kg = $derived(view.inputs?.mass_kg ?? null);
  const chosen = $derived(c.power.find((p) => kg !== null && Math.abs(p.mass_kg - kg) < 0.05) ?? c.power[0]!);
  const power = $derived(
    plot(
      c.airspeed_ms,
      [...c.power.flatMap((p) => p.power_w.map((w) => w / 1000)), ...chosen.high_w.map((w) => w / 1000), ...c.points.map((p) => p.power_w / 1000)],
      P,
    ),
  );
  const headwinds = $derived(Object.keys(c.wh_per_km));
  const perKm = $derived(
    plot(
      c.airspeed_ms,
      headwinds.flatMap((h) => {
        const ys = c.wh_per_km[h]!.filter((y): y is number => y !== null);
        const lo = Math.min(...ys);
        return ys.filter((y) => y <= lo * 4); // the steep end near the headwind's own speed would flatten the rest
      }),
      E,
    ),
  );
  const time = $derived(plot(c.airspeed_ms, c.flight_s.map((s) => s / 60), E));
  const top = $derived(c.top_ms);
  const s = $derived(view.speeds);
  const still = $derived(c.wh_per_km[headwinds[0] ?? "0.0"] ?? []);
  const plus2 = $derived.by(() => {
    if (!s) return null;
    const a = at(c.airspeed_ms, still, s.range);
    const b = at(c.airspeed_ms, still, s.range + 2);
    return a !== null && b !== null ? (b / a - 1) * 100 : null;
  });
  const lines = $derived(
    [
      view.wp_spd_ms ? { v: view.wp_spd_ms, label: t("speed.line.wpspd", { v: num(view.wp_spd_ms) }), cls: "set" } : null,
      view.build_up?.verified_ms ? { v: view.build_up.verified_ms, label: t("speed.line.verified", { v: num(view.build_up.verified_ms) }), cls: "verified" } : null,
      view.top_speed && view.top_speed.airspeed <= c.airspeed_ms.at(-1)! ? { v: view.top_speed.airspeed, label: t("speed.line.max", { v: num(view.top_speed.airspeed) }), cls: "max" } : null,
    ].filter((l): l is { v: number; label: string; cls: string } => l !== null),
  );
  const kw = (w: number | null) => (w === null ? null : w / 1000);
  const minutes = $derived(c.flight_s.map((v) => v / 60));
  const timeLine = $derived(split(c.airspeed_ms, minutes, top, time));
  const timeBest = $derived(s ? at(c.airspeed_ms, minutes, s.endurance) : null);
</script>

<section class="panel" aria-labelledby="c2-title">
  <div class="head">
    <h2 id="c2-title">{t("speed.title")}</h2>
    <span class="faint">{t("speed.about", { kg: num(kg), top: num(top) })}</span>
  </div>

  <figure>
    <figcaption>
      <span class="cap">{t("speed.power")}</span>
      <span><span aria-hidden="true" class="key solid">━</span> {t("speed.power.model")}</span>
      <span><span aria-hidden="true" class="key dashed">┅</span> {t("speed.power.beyond")}</span>
      <span><span aria-hidden="true" class="key pt">●</span> {t("speed.power.points")}</span>
    </figcaption>
    <svg viewBox="0 0 {P.width} {P.height}" role="img" aria-label={t("speed.power.aria", { range: num(s?.range), endurance: num(s?.endurance) })}>
      {#if power.px(top) < P.width - P.right}
        <rect class="outside" x={power.px(top)} y={P.top} width={P.width - P.right - power.px(top)} height={P.height - P.top - P.bottom} />
      {/if}
      {#each power.y.ticks as v (v)}
        <line class="grid" x1={P.left} x2={P.width - P.right} y1={power.py(v)} y2={power.py(v)} />
        <text class="tick" x={P.left - 8} y={power.py(v) + 4} text-anchor="end">{num(v, 1)}</text>
      {/each}
      {#each power.x.ticks as v (v)}
        <text class="tick" x={power.px(v)} y={P.height - P.bottom + 18} text-anchor="middle">{num(v, 0)}</text>
      {/each}
      <text class="tick" x={4} y={12}>{unit("kW")}</text>
      <text class="tick" x={P.width - P.right} y={P.height - 2} text-anchor="end">{unit("m/s")}</text>
      {#each lines as l (l.cls)}
        <line class="vline {l.cls}" x1={power.px(l.v)} x2={power.px(l.v)} y1={P.top} y2={P.height - P.bottom} />
        <text class="tick" x={power.px(l.v)} y={P.top - 6} text-anchor="middle">{l.label}</text>
      {/each}
      <path class="band" d={band(c.airspeed_ms, chosen.low_w.map((w) => w / 1000), chosen.high_w.map((w) => w / 1000), power)} />
      {#each c.power as curve (curve.mass_kg)}
        {@const ln = split(c.airspeed_ms, curve.power_w.map(kw), top, power)}
        {@const mine = curve === chosen}
        <path class="curve" class:mine d={ln.solid} />
        <path class="curve beyond" class:mine d={ln.beyond} />
        <text class="tick" x={P.left + 4} y={power.py(curve.power_w[0]! / 1000) - 5}>{num(curve.mass_kg)} {unit("kg")}</text>
      {/each}
      {#each c.points as q, i (i)}
        <circle class="pt" class:mine={kg !== null && Math.abs(q.mass_kg - kg) < 0.05} cx={power.px(q.airspeed_ms)} cy={power.py(q.power_w / 1000)} r="2.6">
          <title>{t(`energy.part.${q.phase === "level" ? "level" : "hover"}`)}: {num(q.airspeed_ms)} {unit("m/s")}, {num(q.power_w / 1000, 2)} {unit("kW")}, {num(q.mass_kg)} {unit("kg")}</title>
        </circle>
      {/each}
      {#if s}
        {#each [{ v: s.endurance, label: "V_mp" }, { v: s.range, label: "V_mr" }] as m (m.label)}
          {@const y = at(c.airspeed_ms, chosen.power_w.map(kw), m.v)}
          {#if y !== null}
            <circle class="opt" cx={power.px(m.v)} cy={power.py(y)} r="5" />
            <text class="tick strong" x={power.px(m.v)} y={power.py(y) - 10} text-anchor="middle">{m.label} {num(m.v)}</text>
          {/if}
        {/each}
      {/if}
    </svg>
  </figure>

  <div class="split">
    <div class="charts">
      <figure>
        <figcaption>
          <span class="cap">{t("speed.perKm", { kg: num(kg) })}</span>
          {#each headwinds as h, i (h)}
            <span><span aria-hidden="true" style:color={HEADWIND_COLOURS[i]}>━</span> {Number(h) === 0 ? t("speed.still") : t("speed.headwind", { h: num(Number(h), 0) })}</span>
          {/each}
        </figcaption>
        <svg viewBox="0 0 {E.width} {E.height}" role="img" aria-label={t("speed.perKm.aria")}>
          {#each perKm.y.ticks as v (v)}
            <line class="grid" x1={E.left} x2={E.width - E.right} y1={perKm.py(v)} y2={perKm.py(v)} />
            <text class="tick" x={E.left - 8} y={perKm.py(v) + 4} text-anchor="end">{num(v, 0)}</text>
          {/each}
          {#each perKm.x.ticks as v (v)}
            <text class="tick" x={perKm.px(v)} y={E.height - E.bottom + 16} text-anchor="middle">{num(v, 0)}</text>
          {/each}
          <text class="tick" x={4} y={10}>{unit("W.h/km")}</text>
          <defs><clipPath id="sp-perkm"><rect x={E.left} y={E.top} width={E.width - E.left - E.right} height={E.height - E.top - E.bottom} /></clipPath></defs>
          <g clip-path="url(#sp-perkm)">
            {#each headwinds as h, i (h)}
              {@const ln = split(c.airspeed_ms, c.wh_per_km[h]!, top, perKm)}
              {@const lo = least(c.airspeed_ms, c.wh_per_km[h]!, top)}
              <path class="curve" style:stroke={HEADWIND_COLOURS[i]} d={ln.solid} />
              <path class="curve beyond" style:stroke={HEADWIND_COLOURS[i]} d={ln.beyond} />
              {#if lo}
                <circle class="opt" cx={perKm.px(lo.v)} cy={perKm.py(lo.y)} r="4" />
                <text class="tick" x={perKm.px(lo.v) + 8} y={perKm.py(lo.y) < E.top + 14 ? perKm.py(lo.y) + 14 : perKm.py(lo.y) - 6}>{num(lo.v)}</text>
              {/if}
            {/each}
          </g>
        </svg>
      </figure>
      <figure>
        <figcaption><span class="cap">{t("speed.time", { kg: num(kg) })}</span></figcaption>
        <svg viewBox="0 0 {E.width} {E.height}" role="img" aria-label={t("speed.time.aria")}>
          {#each time.y.ticks as v (v)}
            <line class="grid" x1={E.left} x2={E.width - E.right} y1={time.py(v)} y2={time.py(v)} />
            <text class="tick" x={E.left - 8} y={time.py(v) + 4} text-anchor="end">{num(v, 1)}</text>
          {/each}
          {#each time.x.ticks as v (v)}
            <text class="tick" x={time.px(v)} y={E.height - E.bottom + 16} text-anchor="middle">{num(v, 0)}</text>
          {/each}
          <text class="tick" x={4} y={10}>{unit("min")}</text>
          <text class="tick" x={E.width - E.right} y={E.height - 2} text-anchor="end">{unit("m/s")}</text>
          <path class="curve mine" d={timeLine.solid} />
          <path class="curve beyond mine" d={timeLine.beyond} />
          {#if s && timeBest !== null}
            <circle class="opt" cx={time.px(s.endurance)} cy={time.py(timeBest)} r="4" />
            <text class="tick" x={time.px(s.endurance)} y={time.py(timeBest) - 9} text-anchor="middle">{t("speed.time.best", { min: num(timeBest), v: num(s.endurance) })}</text>
          {/if}
        </svg>
      </figure>
    </div>
    <div class="facts">
      <p>{t("speed.note")}</p>
      {#if s && plus2 !== null}<p class="faint">{t(plus2 >= 0 ? "speed.plus2.more" : "speed.plus2.less", { pct: num(Math.abs(plus2), 0) })}</p>{/if}
      {#each headwinds.slice(1) as h (h)}
        {@const lo = least(c.airspeed_ms, c.wh_per_km[h]!, top)}
        {#if lo}<p class="faint">{t("speed.into", { h: num(Number(h), 0), v: num(lo.v) })}</p>{/if}
      {/each}
      <p class="faint">{t("speed.beyondNote", { top: num(top) })}</p>
    </div>
  </div>
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
  p {
    margin: 0;
    font-size: 13px;
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
  .key.solid,
  .key.dashed {
    color: var(--accent);
  }
  .key.pt {
    color: var(--caution);
  }
  svg {
    width: 100%;
    max-width: 1000px;
    height: auto;
    display: block;
  }
  .split {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    align-items: flex-start;
  }
  .charts svg {
    max-width: 720px;
  }
  .charts {
    flex: 999 1 420px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
  }
  .facts {
    flex: 1 1 220px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .grid {
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
  .outside {
    fill: var(--text-faint);
    fill-opacity: 0.07;
  }
  .vline {
    stroke: var(--text-faint);
    stroke-dasharray: 4 4;
  }
  .vline.verified {
    stroke: var(--accent);
    stroke-dasharray: 2 3;
  }
  .band {
    fill: var(--accent);
    fill-opacity: 0.14;
  }
  .curve {
    fill: none;
    stroke: var(--text-faint);
    stroke-width: 1.5;
  }
  .curve.mine {
    stroke: var(--accent);
    stroke-width: 2.2;
  }
  .curve.beyond {
    stroke-dasharray: 5 4;
  }
  .pt {
    fill: color-mix(in srgb, var(--caution) 45%, transparent);
  }
  .pt.mine {
    fill: var(--caution);
  }
  .opt {
    fill: var(--surface);
    stroke: var(--text);
    stroke-width: 1.8;
  }
</style>
