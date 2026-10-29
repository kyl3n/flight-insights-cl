<script lang="ts">
  // The flight's energy in the Propulsion view (spec: Propulsion and power, "Energy";
  // mock-up Propulsion): the energy used and how far it went, where the energy went, the
  // packs' power against time with the phases shaded, the flight's steady points on its
  // airframe's power curve, and what was watched at speed. The numbers are the engine's.
  import { app, t } from "../lib/app.svelte";
  import { getRegistry } from "../lib/data";
  import {
    bar,
    clock,
    curveChart,
    flightEnergy,
    partKey,
    shaded,
    standing,
    watched,
    PART_COLOURS,
    type EnergyView,
  } from "../lib/energy";
  import type { MessageKey } from "../lib/i18n";
  import { findingAction, findingTitle, formatNumber, formatValue, unitText, zoneLook, type Registry } from "../lib/report";
  import { STATUS } from "../lib/status";
  import StatusBadge from "./StatusBadge.svelte";
  import TimePlot from "./TimePlot.svelte";
  import ZoneBar from "./ZoneBar.svelte";

  let { flightId }: { flightId: string } = $props();

  let view = $state<EnergyView | null>(null);
  let reg = $state<Registry | null>(null);
  let error = $state("");
  let open = $state(true);

  $effect(() => {
    void flightId;
    view = null;
    error = "";
    flightEnergy(flightId)
      .then((v) => (view = v))
      .catch((e: unknown) => (error = String(e)));
    getRegistry()
      .then((r) => (reg = r))
      .catch(() => (reg = null));
  });

  const num = (v: number | null | undefined, digits = 1) =>
    v === null || v === undefined || !Number.isFinite(v) ? "–" : formatNumber(Math.round(v * 10 ** digits) / 10 ** digits, app.locale);
  const signed = (v: number | null | undefined) => (v === null || v === undefined ? "–" : `${v > 0 ? "+" : ""}${num(v)}%`);
  const unit = (u: string) => unitText(u, app.locale);
  const kw = (w: number | null | undefined) => (w === null || w === undefined ? "–" : num(w / 1000, 2));

  const FRAME = { width: 640, height: 240, left: 56, right: 16, top: 24, bottom: 34 };
  const chart = $derived(view ? curveChart(view.on_curve, FRAME) : null);
  const parts = $derived(view ? bar(view.breakdown) : []);
  const status = $derived(view ? standing(view.on_curve.against, view.findings) : null);
  const legend = $derived(view ? [...new Set(view.phases.map((p) => p.phase))] : []);
  const trace = $derived(view ? [{ label: t("energy.power"), env: view.power, colour: "#9FB2C6" }] : []);
</script>

{#if error}<p class="error">{error}</p>{/if}

{#if view}
  {@const f = view.figures}
  {@const c = view.on_curve}
  {@const a = c.against}
  <section class="panel" aria-labelledby="en-title">
    <div class="head">
      <h2 id="en-title">
        <button type="button" class="fold" aria-expanded={open} aria-controls="en-body" onclick={() => (open = !open)}>
          <span aria-hidden="true" class="chev" class:shut={!open}>▾</span>{t("energy.title")}
        </button>
      </h2>
      <span class="faint">
        {t("energy.about")}{#if c.mass_kg !== null}{` · ${num(c.mass_kg)} ${unit("kg")}`}{/if}{#if c.density_ratio !== null}{` · ${t("energy.air", { ratio: num(c.density_ratio, 2) })}`}{/if}
      </span>
      {#if status && a}
        <span class="end">
          <StatusBadge
            {status}
            label={status === "caution"
              ? t("energy.over", { pct: signed(a.excess_pct) })
              : t("energy.within", { pct: num(a.threshold_pct, 0) })}
          />
        </span>
      {/if}
    </div>

    {#if !open}
      <p class="faint">
        {t("energy.closed", { wh: num(f.used_wh, 0), km: num(f.flown_m / 1000, 2) })}
      </p>
    {:else}
      <div id="en-body" class="body">
        {#if view.simulated}<p class="notice">{t("energy.simulated")}</p>{/if}

        {#if reg}
          {#each view.findings as finding (finding.code)}
            <div class="callout caution">
              <p class="kicker" style:color="var({STATUS.caution.tone})">{STATUS.caution.glyph} {t("energy.anomaly")}</p>
              <p>{findingTitle(reg, finding, app.locale)}</p>
              <p class="faint">{findingAction(reg, finding, app.locale)}</p>
            </div>
          {/each}
        {/if}

        <div class="tiles">
          <div class="card">
            <p class="faint">{t("energy.used")}</p>
            <p class="big mono">{formatValue(f.used_wh, "W.h", app.locale)}</p>
            <p class="faint">
              <span class="mono">{formatValue(f.used_mah, "mA.h", app.locale)}</span>{#if f.share_pct !== null}, {t(
                  f.capacity_from === "measured" ? "energy.share.measured" : "energy.share",
                  { pct: num(f.share_pct, 0) },
                )}{/if}
            </p>
          </div>
          <div class="card">
            <p class="faint">{t("energy.distance")}</p>
            <p class="big mono">{num(f.flown_m / 1000, 2)} {unit("km")}</p>
            <p class="faint">{t("energy.distance.note", { time: clock(f.seconds) })}</p>
          </div>
          <div class="card">
            <p class="faint">{t("energy.perKm")}</p>
            <p class="big mono">{f.wh_per_km === null ? "–" : formatValue(f.wh_per_km, "W.h/km", app.locale)}</p>
            <p class="faint">{t("energy.perKm.note")}</p>
          </div>
          <div class="card">
            <p class="faint">{t("energy.cruise")}</p>
            <p class="big mono">{f.cruise_wh_per_km === null ? "–" : formatValue(f.cruise_wh_per_km, "W.h/km", app.locale)}</p>
            <p class="faint">
              {#if f.cruise_airspeed_ms !== null}{t("energy.cruise.at", { speed: num(f.cruise_airspeed_ms) })}{:else}{t("energy.cruise.none")}{/if}
            </p>
          </div>
          <div class="card">
            <p class="faint">{t("energy.climb")}</p>
            {#if f.climb}
              <p class="big mono">{num(f.climb.wh_per_m, 2)} {unit("W.h/m")}</p>
              <p class="faint">
                {t("energy.climb.at", { rate: num(f.climb.rate_ms) })}{#if f.climb.above_hover_wh_per_m !== null}; {t("energy.climb.above", {
                    x: num(f.climb.above_hover_wh_per_m, 2),
                  })}{/if}
              </p>
            {:else}
              <p class="big mono">–</p>
              <p class="faint">{t("energy.climb.none")}</p>
            {/if}
          </div>
          <div class="card" class:warn={(a?.by_phase_pct.hover ?? 0) > (a?.threshold_pct ?? Infinity)}>
            <p class="faint">{t("energy.hover")}</p>
            {#if c.hover}
              <p class="big mono">{kw(c.hover.standard_w)} {unit("kW")}</p>
              <p class="faint">
                {#if a && a.by_phase_pct.hover !== null && a.by_phase_pct.hover !== undefined}<span class="mono">{signed(a.by_phase_pct.hover)}</span>{" · "}{/if}{t("energy.hover.model", { kg: num(c.mass_kg), kw: kw(c.hover.model_standard_w) })}
              </p>
            {:else}
              <p class="big mono">–</p>
              <p class="faint">{t("energy.hover.none")}</p>
            {/if}
          </div>
        </div>

        <figure>
          <figcaption>
            <span class="cap">
              {t("energy.where", { wh: num(view.breakdown.cells_wh ?? view.breakdown.terminal_wh, 0) })}
            </span>
            {#if view.breakdown.cells_wh !== null}
              <span>{t("energy.where.terminals", { wh: num(view.breakdown.terminal_wh, 0) })}</span>
            {/if}
          </figcaption>
          <div class="stack" role="img" aria-label={parts.map((p) => `${t(partKey(p.part))} ${num(p.wh, 0)} Wh`).join(", ")}>
            {#each parts as p (p.part)}
              <span style:left="{p.from * 100}%" style:width="{Math.max(0, p.share * 100 - 0.25)}%" style:background={p.colour}></span>
            {/each}
          </div>
          <div class="legend">
            {#each parts as p (p.part)}
              <div class="key">
                <span class="swatch" style:background={p.colour} aria-hidden="true"></span>
                <span class="name">{t(partKey(p.part))}</span>
                <span class="mono">{formatValue(p.wh, "W.h", app.locale)}</span>
                <span class="mono faint share">{num(p.share * 100, p.share < 0.01 ? 1 : 0)}%</span>
              </div>
            {/each}
          </div>
          <p class="faint">
            {t(view.breakdown.split ? "energy.where.split" : "energy.where.nosplit")}
            {#if view.breakdown.split_why}{t("energy.where.why", { why: view.breakdown.split_why })}{/if}
            {#if view.breakdown.heat_why}{t("energy.where.noheat", { why: view.breakdown.heat_why })}{/if}
          </p>
        </figure>

        <figure>
          <figcaption>
            <span class="cap">{t("energy.power")}</span>
            {#each legend as phase (phase)}
              <span><span aria-hidden="true" style:color={PART_COLOURS[phase]}>▮</span> {t(partKey(phase))}</span>
            {/each}
            {#if view.fast}
              <span><span aria-hidden="true" class="dashed">▯</span> {t("energy.fast.box", { speed: num(view.fast.speed_ms) })}</span>
            {/if}
          </figcaption>
          <TimePlot
            traces={trace}
            unit="W"
            range={view.window_s}
            armed={view.armed_s}
            spans={shaded(view.phases)}
            outlined={view.fast?.spans_s ?? []}
            label={t("energy.power.aria", { wh: num(f.used_wh, 0) })}
            height={200}
          />
        </figure>

        <div class="split">
          <figure class="curve">
            <figcaption>
              <span class="cap">{t("energy.curve")}</span>
              {#if chart}
                <span><span aria-hidden="true" class="model">━</span> {t("energy.curve.model", { kg: num(c.mass_kg) })}</span>
                <span><span aria-hidden="true" class="mine">●</span> {t("energy.curve.mine")}</span>
              {/if}
            </figcaption>
            {#if chart}
              <svg viewBox="0 0 {FRAME.width} {FRAME.height}" role="img" aria-label={t("energy.curve.aria", { pct: signed(a?.excess_pct) })}>
                {#each chart.y.ticks as v (v)}
                  <line class="grid" x1={FRAME.left} x2={FRAME.width - FRAME.right} y1={chart.py(v)} y2={chart.py(v)} />
                  <text class="tick" x={FRAME.left - 8} y={chart.py(v) + 4} text-anchor="end">{num(v / 1000, 1)}</text>
                {/each}
                {#each chart.x.ticks as v (v)}
                  <text class="tick" x={chart.px(v)} y={FRAME.height - FRAME.bottom + 18} text-anchor="middle">{num(v, 0)}</text>
                {/each}
                <text class="tick" x={FRAME.left - 8} y={12} text-anchor="end">{unit("kW")}</text>
                <defs>
                  <clipPath id="en-plot"><rect x={FRAME.left} y={FRAME.top - 4} width={FRAME.width - FRAME.left - FRAME.right} height={FRAME.height - FRAME.top - FRAME.bottom + 4} /></clipPath>
                </defs>
                {#if chart.flown !== null && chart.flown < FRAME.width - FRAME.right - 40}
                  <rect class="beyond" x={chart.flown} y={FRAME.top} width={FRAME.width - FRAME.right - chart.flown} height={FRAME.height - FRAME.top - FRAME.bottom} />
                  <text class="tick" x={chart.flown + 6} y={FRAME.top + 12}>{t("energy.curve.beyond")}</text>
                {/if}
                <text class="tick" x={FRAME.width - FRAME.right} y={FRAME.height - 2} text-anchor="end">{unit("m/s")}</text>
                <g clip-path="url(#en-plot)">
                  {#if chart.band}<path class="band" d={chart.band} />{/if}
                  <path class="line" d={chart.line} />
                </g>
                {#each chart.points as q, i (i)}
                  <circle class="point" class:hover={q.phase === "hover"} cx={q.x} cy={q.y} r="3.5">
                    <title>{t(partKey(q.phase))}: {num(q.p.airspeed_ms)} {unit("m/s")}, {kw(q.p.power_w)} {unit("kW")}, {num(q.p.seconds, 0)} {unit("s")}</title>
                  </circle>
                {/each}
              </svg>
            {:else}
              <p class="faint">{t("energy.curve.none", { why: c.why ?? "" })}</p>
            {/if}
          </figure>
          <div class="facts">
            {#if a}
              <p class="fact">
                <span class="faint">{t("energy.against")}</span>
                <span>
                  <span class="mono">{signed(a.excess_pct)}</span>
                  <span class="faint">{t("energy.against.interval", { lo: signed(a.interval_pct[0]), hi: signed(a.interval_pct[1]) })}</span>
                </span>
              </p>
              <p class="fact">
                <span class="faint">{t("energy.against.phases")}</span>
                <span class="mono">{t("energy.against.byPhase", { hover: signed(a.by_phase_pct.hover), level: signed(a.by_phase_pct.level) })}</span>
              </p>
              <p class="fact">
                <span class="faint">{t("energy.against.usual")}</span>
                <span>{t("energy.against.threshold", { pct: num(a.threshold_pct, 0), n: a.others })}</span>
              </p>
            {:else if chart}
              <p class="faint">{t("energy.against.none")}</p>
            {/if}
            {#if c.points.length}
              <p class="fact">
                <span class="faint">{t("energy.points")}</span>
                <span>
                  {(["hover", "level", "climb", "descent"] as const)
                    .map((ph) => [ph, c.points.filter((p) => p.phase === ph).reduce((s, p) => s + p.tiles, 0)] as const)
                    .filter(([, n]) => n > 0)
                    .map(([ph, n]) => `${t(partKey(ph))} ${n}`)
                    .join(", ")}
                </span>
              </p>
            {/if}
            {#if c.mass_kg !== null}
              <p class="fact">
                <span class="faint">{t("energy.weightAir")}</span>
                <span>{t("energy.weightAir.value", { kg: num(c.mass_kg), from: c.mass_from ?? "", ratio: num(c.density_ratio, 2) })}</span>
              </p>
            {/if}
            {#if c.wind_from}
              <p class="fact">
                <span class="faint">{t("energy.wind")}</span>
                <span>{t(`energy.wind.${c.wind_from}` as MessageKey)}</span>
              </p>
            {/if}
            {#if c.level}<p class="faint">{t(`energy.level.${c.level}` as MessageKey)}</p>{/if}
            <p class="faint">{t("energy.curve.note")}</p>
          </div>
        </div>

        <div class="speed">
          <div class="head">
            <h3>{t("energy.fast")}</h3>
            {#if view.fast}
              <span class="faint">
                {t(view.fast.speed_from === "air" ? "energy.fast.about.air" : "energy.fast.about.ground", {
                  speed: num(view.fast.speed_ms),
                  s: num(view.fast.seconds, 0),
                  top: num(view.fast.top_ms),
                })}
              </span>
            {/if}
          </div>
          {#if !view.fast}
            <p class="faint">{t("energy.fast.none", { why: view.fast_why ?? "" })}</p>
          {:else if view.watch && reg}
            <div class="scroll">
              <table>
                <thead>
                  <tr>
                    <th scope="col">{t("energy.fast.col.watched")}</th>
                    <th scope="col">{t("energy.fast.col.run")}</th>
                    <th scope="col">{t("energy.fast.col.zones")}</th>
                    <th scope="col"><span class="sr">{t("energy.fast.col.status")}</span></th>
                  </tr>
                </thead>
                <tbody>
                  {#each watched(view.watch.metrics) as w (w.metric)}
                    {@const info = reg.metrics[w.metric]}
                    {@const look = zoneLook(w.zone)}
                    <tr>
                      <td>{info?.name[app.locale] ?? w.metric}</td>
                      <td class="mono">{formatValue(w.value, info?.unit ?? "", app.locale)}</td>
                      <td class="zones">{#if info && w.zone}<ZoneBar metric={info} value={w.value} />{:else}<span class="faint">{t("energy.fast.nozones")}</span>{/if}</td>
                      <td class="end">{#if w.zone}<StatusBadge status={look.status} label={t(look.label)} />{/if}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}
        </div>
      </div>
    {/if}
  </section>
{/if}

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
  .end {
    margin-left: auto;
  }
  h2,
  h3 {
    margin: 0;
    font-size: 14px;
  }
  h3 {
    font-size: 13px;
  }
  .fold {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--text);
    font: inherit;
    font-weight: 600;
    cursor: pointer;
  }
  .chev {
    color: var(--text-faint);
    transition: transform 0.15s;
  }
  .chev.shut {
    transform: rotate(-90deg);
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
  .error {
    color: var(--out);
  }
  .notice {
    font-size: 12px;
    color: var(--text-muted);
  }
  .body {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .callout,
  .card {
    padding: 12px 14px;
    border: 1px solid var(--border-soft);
    border-radius: 10px;
    background: var(--surface-2);
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 13px;
  }
  .callout.caution,
  .card.warn {
    border-color: color-mix(in srgb, var(--caution) 55%, transparent);
  }
  .callout.caution {
    background: color-mix(in srgb, var(--caution) 8%, var(--surface-2));
  }
  .kicker {
    font-weight: 600;
    font-size: 12px;
  }
  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    gap: 10px;
  }
  .big {
    font-size: 18px;
    font-weight: 500;
  }
  figure {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
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
  .stack {
    position: relative;
    height: 22px;
    border-radius: 4px;
    background: var(--surface-2);
    overflow: hidden;
  }
  .stack span {
    position: absolute;
    top: 0;
    bottom: 0;
  }
  .legend {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
    gap: 4px 16px;
    font-size: 12px;
  }
  .key {
    display: flex;
    align-items: baseline;
    gap: 6px;
  }
  .swatch {
    flex: 0 0 10px;
    width: 10px;
    height: 10px;
    border-radius: 2px;
  }
  .name {
    flex: 1 1 auto;
    color: var(--text-2);
  }
  .share {
    flex: 0 0 38px;
    text-align: right;
  }
  .dashed {
    color: var(--accent);
  }
  .split {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    align-items: flex-start;
  }
  .curve {
    flex: 999 1 380px;
  }
  .curve svg {
    width: 100%;
    max-width: 760px;
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
  .beyond {
    fill: var(--text-faint);
    fill-opacity: 0.06;
  }
  .band {
    fill: var(--accent);
    fill-opacity: 0.14;
  }
  .line {
    fill: none;
    stroke: var(--accent);
    stroke-width: 2.2;
  }
  .point {
    fill: var(--caution);
    stroke: var(--surface);
    stroke-width: 1.2;
  }
  .point.hover {
    fill: color-mix(in srgb, var(--caution) 65%, var(--surface));
  }
  .model {
    color: var(--accent);
  }
  .mine {
    color: var(--caution);
  }
  .facts {
    flex: 1 1 220px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .fact {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 10px;
    font-size: 12px;
    text-align: right;
  }
  .fact > .faint:first-child {
    text-align: left;
  }
  .speed {
    display: flex;
    flex-direction: column;
    gap: 8px;
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
    white-space: nowrap;
  }
  td {
    padding: 6px 12px 6px 0;
    border-top: 1px solid var(--border-soft);
  }
  td.zones {
    min-width: 120px;
  }
  td.end {
    text-align: right;
  }
  .sr {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
  }
</style>
