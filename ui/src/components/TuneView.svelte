<script lang="ts">
  // The Tune view of one flight (spec: User interface, "Tune"; mock-up Tune): attitude
  // tracking error, the gate's measure, and rate tracking error, its guard, against a
  // flight of the same payload band; the step and frequency responses of one axis;
  // the stick inputs counted and left out, with why; and whether the plan improved the
  // tune. Every verdict is the engine's.
  import { app, t } from "../lib/app.svelte";
  import { getRegistry } from "../lib/data";
  import type { MessageKey } from "../lib/i18n";
  import type { ProcedureStep } from "../lib/plan";
  import { formatNumber, formatTakeoff, unitText, type Registry } from "../lib/report";
  import { engineCall } from "../lib/shell";
  import { STATUS } from "../lib/status";
  import { groupName } from "../lib/trends";
  import {
    AXES,
    clock,
    counts,
    gateItems,
    leftOut,
    rowLook,
    scaleTop,
    type Axis,
    type Curve,
    type Family,
    type Measure,
    type Side,
    type TuneView,
  } from "../lib/tune";
  import ProcedureSteps from "./ProcedureSteps.svelte";
  import ResponsePlot from "./ResponsePlot.svelte";

  let { flightId }: { flightId: string } = $props();

  let view = $state<TuneView | null>(null);
  let reg = $state<Registry | null>(null);
  let error = $state("");
  let axis = $state<Axis>("roll");
  let group = $state<string | null>(null); // null: the flight's own band
  let compareId = $state<string | null>(null); // null: the engine's choice
  let showCard = $state(false);

  $effect(() => {
    void flightId;
    view = null;
    group = null;
    compareId = null;
    axis = "roll";
  });
  $effect(() => {
    const params = { flight_id: flightId, axis, group, compare_id: compareId };
    error = "";
    engineCall<TuneView>("flight.tune", params)
      .then((v) => (view = v))
      .catch((e: unknown) => (error = String(e)));
  });
  $effect(() => {
    getRegistry().then((r) => (reg = r)).catch(() => {});
  });

  function pickBand(g: string) {
    group = g;
    compareId = null; // a comparison belongs to its band
  }

  const groupLabel = (g: string) => {
    const found = view?.groups.find((x) => x.group === g);
    return found ? groupName(found, app.locale) : g;
  };
  const when = (utc: number | null | undefined) => formatTakeoff(utc ?? null, app.locale) ?? t("flights.noTime");
  const flightLabel = (id: string) => {
    const l = view?.labels[id];
    return l ? `${when(l.utc_us)} · ${groupLabel(l.group)}` : id.slice(0, 8);
  };
  const axisName = (a: Axis) => t(`tune.axis.${a}` as MessageKey);
  const num = (v: number | undefined | null, digits = 1) =>
    v === undefined || v === null ? "–" : formatNumber(Math.round(v * 10 ** digits) / 10 ** digits, app.locale);

  const bandName = $derived(view ? groupLabel(view.group) : "");
  const rows = (family: Family) => view?.rows.filter((r) => r.family === family) ?? [];
  const top = (family: Family) => scaleTop(rows(family).flatMap((r) => [r.this, r.compare]));
  const pos = (v: number, family: Family) => `${Math.min(100, Math.max(0, (v / top(family)) * 100))}%`;
  const span = (m: Measure, family: Family) =>
    m.low === undefined || m.high === undefined
      ? null
      : { left: pos(m.low, family), width: `${Math.max(0.5, ((m.high - m.low) / top(family)) * 100)}%` };
  const ticks = (family: Family) => [0, top(family) / 2, top(family)];

  // The response plots: the comparison dashed and faint, this flight with its interval.
  const stepCurves = $derived.by((): Curve[] => {
    const out: Curve[] = [];
    const before = view?.compare?.curves?.step;
    if (before) out.push({ label: flightLabel(view!.compare!.id), x: before.t, y: before.y, tone: "--text-faint", dash: [5, 4] });
    const now = view?.this?.curves?.step;
    if (now) out.push({ label: t("tune.this"), x: now.t, y: now.y, tone: "--accent", low: now.low, high: now.high });
    return out;
  });
  const freqCurves = $derived.by((): Curve[] => {
    const out: Curve[] = [];
    const before = view?.compare?.curves?.frequency;
    if (before) out.push({ label: flightLabel(view!.compare!.id), x: before.f, y: before.db, tone: "--text-faint", dash: [5, 4] });
    const now = view?.this?.curves?.frequency;
    if (now) out.push({ label: t("tune.this"), x: now.f, y: now.db, tone: "--accent" });
    return out;
  });
  const bandwidths = $derived.by(() => {
    const marks: { x: number; tone: string }[] = [];
    const before = view?.compare?.curves?.frequency?.bandwidth_hz;
    const now = view?.this?.curves?.frequency?.bandwidth_hz;
    if (before) marks.push({ x: before, tone: "--text-faint" });
    if (now) marks.push({ x: now, tone: "--accent" });
    return marks;
  });

  function figure(name: string): { label: string; before: string; after: string; range: string } | null {
    const f = view?.figures[name];
    if (!f || (!f.this && !f.compare)) return null;
    const unit = reg?.metrics[`tune.${name}_${axis}`]?.unit ?? "";
    const text = (m: Measure | null) =>
      m ? (unit === "%" ? `${num(m.value, 0)}%` : `${num(m.value, unit === "ms" ? 0 : 2)} ${unitText(unit, app.locale)}`.trim()) : "–";
    const range = f.this?.low !== undefined && f.this?.high !== undefined ? `${num(f.this.low, unit === "ms" ? 0 : 2)}–${num(f.this.high, unit === "ms" ? 0 : 2)}` : "";
    return { label: t(`tune.fig.${name}` as MessageKey), before: text(f.compare), after: text(f.this), range };
  }

  // The stick inputs timeline: each input as a mark along the armed time.
  const armed = $derived(view?.this?.inputs?.armed_s ?? 0);
  const x = (s: number) => (armed > 0 ? (s / armed) * 1000 : 0);
  const notes = $derived(view?.this?.inputs ? leftOut(view.this.inputs) : []);
  const reasonText = (r: string) => t(`tune.reason.${r.replace(/ /g, "_")}` as MessageKey);

  const items = $derived(view?.gate && reg ? gateItems(view.gate, reg, groupLabel, app.locale) : []);
  const met = $derived(items.filter((i) => i.status === "ok").length);
  const gateState = $derived.by(() => {
    const g = view?.gate;
    if (!g) return null;
    const key = g.pending ? "pending" : g.status.replace(/ /g, "_");
    const tone = g.status === "confirmed" ? "ok" : g.status === "regressed" ? "out" : "caution";
    return { text: t(`tune.gate.state.${key}` as MessageKey), tone } as const;
  });

  const cardSteps = $derived(
    (view?.card ?? []).map((s): ProcedureStep => ({ kind: s.kind, text: s.text, params: [], step: null, checks: [] })),
  );

  function sideCounts(s: Side | null) {
    return counts(s?.inputs);
  }
</script>

{#if error}<p class="error">{error}</p>{/if}
{#if !view && !error}<p class="faint">{t("report.analysing")}</p>{/if}

{#if view}
  {@const v = view}
  <p class="faint about">{t("tune.about")}</p>
  <div class="layout">
    <aside class="panel side" aria-labelledby="tune-comparison">
      <h2 id="tune-comparison">{t("tune.comparison")}</h2>

      <label class="field">
        <span class="faint">{t("tune.comparedWith")}</span>
        {#if v.candidates.length}
          <select value={v.compare?.id ?? ""} onchange={(e) => (compareId = (e.currentTarget as HTMLSelectElement).value || null)}>
            {#each v.candidates as c (c.id)}
              <option value={c.id}>
                {t(c.same_tune ? "tune.candidate.same" : c.before ? "tune.candidate.other" : "tune.candidate.later", { when: when(c.utc_us) })}
              </option>
            {/each}
          </select>
          {#if v.compare?.why}<span class="faint">{t(`tune.why.${v.compare.why}` as MessageKey)}</span>{/if}
        {:else}
          <span class="faint">{t("tune.noCompare")}</span>
        {/if}
      </label>

      <div class="field">
        <span class="faint">{t("tune.band")}</span>
        <div role="group" aria-label={t("tune.band")} class="toggle">
          {#each v.groups as g (g.group)}
            <button type="button" aria-pressed={v.group === g.group} onclick={() => pickBand(g.group)}>{groupName(g, app.locale)}</button>
          {/each}
        </div>
      </div>

      <div class="field">
        <span class="faint">{t("tune.axisPick")}</span>
        <div role="group" aria-label={t("tune.axisPick")} class="toggle">
          {#each AXES as a (a)}
            <button type="button" aria-pressed={axis === a} onclick={() => (axis = a)}>{axisName(a)}</button>
          {/each}
        </div>
      </div>

      {#if v.this}
        {@const mine = sideCounts(v.this)}
        {@const theirs = sideCounts(v.compare)}
        <table>
          <thead>
            <tr><th></th><th>{t("tune.this")}</th>{#if v.compare}<th>{t("tune.compared")}</th>{/if}</tr>
          </thead>
          <tbody>
            <tr>
              <td>{t("tune.ctx.rate")}</td>
              <td>{v.this.inputs?.rate_hz ? t("tune.ctx.rateValue", { hz: num(v.this.inputs.rate_hz, 0) }) : "–"}</td>
              {#if v.compare}<td>{v.compare.inputs?.rate_hz ? t("tune.ctx.rateValue", { hz: num(v.compare.inputs.rate_hz, 0) }) : "–"}</td>{/if}
            </tr>
            {#each AXES.filter((a) => mine[a] || theirs[a]) as a (a)}
              <tr>
                <td>{t("tune.ctx.inputs", { axis: axisName(a).toLocaleLowerCase() })}</td>
                <td>{mine[a] ? t("tune.ctx.inputsValue", mine[a]) : "–"}</td>
                {#if v.compare}<td>{theirs[a] ? t("tune.ctx.inputsValue", theirs[a]) : "–"}</td>{/if}
              </tr>
            {/each}
            <tr>
              <td>{t("tune.ctx.stick")}</td>
              <td>{v.this.inputs?.stick_median != null ? `${num(v.this.inputs.stick_median * 100, 0)}%` : "–"}</td>
              {#if v.compare}<td>{v.compare.inputs?.stick_median != null ? `${num(v.compare.inputs.stick_median * 100, 0)}%` : "–"}</td>{/if}
            </tr>
          </tbody>
        </table>
        <p class="faint">{t("tune.ctx.note")}</p>
      {/if}

      <button type="button" class="primary" onclick={() => (showCard = !showCard)} aria-expanded={showCard}>{t("tune.card")}</button>
      {#if showCard}
        <div class="card" role="region" aria-label={t("tune.card")}>
          <p class="faint">{t("tune.card.about")}</p>
          <ProcedureSteps steps={cardSteps} />
          <button type="button" class="secondary" onclick={() => (showCard = false)}>{t("tune.close")}</button>
        </div>
      {/if}
    </aside>

    <div class="main">
      {#if !v.this}
        <section class="panel"><p class="notice">▲ {t("tune.noThis")}</p></section>
      {:else if !v.rows.some((r) => r.this)}
        <section class="panel"><p class="notice">▲ {t("tune.notMeasured")}</p></section>
      {/if}

      {#each [["attitude_error", "tune.attitude.title", "tune.attitude.about"], ["tracking_error", "tune.rate.title", "tune.rate.about"]] as const as [family, title, about] (family)}
        {#if rows(family).length}
          <section class="panel">
            <div class="row">
              <h2>{t(title, { band: bandName })}</h2>
              <span class="legend">
                {#if v.compare}<span class="key faint">━●━ {t("tune.legend.compare", { flight: when(v.compare.utc_us) })}</span>{/if}
                <span class="key accent">━●━ {t("tune.this")}</span>
              </span>
            </div>
            <div class="rows">
              {#each rows(family) as r (r.metric)}
                {@const look = rowLook(r, v.needs[family]?.by, app.locale)}
                <div class="axis-row">
                  <span class="axis-name">{axisName(r.axis)}</span>
                  <div class="track" aria-label={`${axisName(r.axis)}: ${num(r.compare?.value)} → ${num(r.this?.value)}`}>
                    {#each [["compare", r.compare], ["this", r.this]] as const as [which, m] (which)}
                      {#if m}
                        {@const s = span(m, family)}
                        <div class="lane {which}">
                          {#if s}<span class="interval" style:left={s.left} style:width={s.width}></span>{/if}
                          <span class="dot" style:left={pos(m.value, family)}></span>
                        </div>
                      {/if}
                    {/each}
                  </div>
                  <div class="values">
                    <span class="mono">
                      {#if r.compare}<span class="faint">{num(r.compare.value)}%</span>{" → "}{/if}{r.this ? `${num(r.this.value)}%` : "–"}
                    </span>
                    {#if look}
                      <span class="verdict" style:color="var({STATUS[look.status].tone})">{STATUS[look.status].glyph} {look.text}</span>
                    {/if}
                  </div>
                </div>
              {/each}
              <div class="axis-row scale-row" aria-hidden="true">
                <span></span>
                <div class="scale">
                  {#each ticks(family) as tick, i (i)}<span style:left={pos(tick, family)}>{num(tick, 0)}%</span>{/each}
                </div>
                <span></span>
              </div>
            </div>
            <p class="faint">{t(about)}</p>
          </section>
        {/if}
      {/each}

      {#if v.this}
        <div class="pair">
          <section class="panel">
            <h2>{t("tune.step.title", { axis: axisName(axis).toLocaleLowerCase() })}</h2>
            <span class="legend">
              {#if v.compare?.curves?.step}<span class="key faint">┅ {when(v.compare.utc_us)}</span>{/if}
              <span class="key accent">━ {t("tune.legend.thisInterval")}</span>
            </span>
            {#if stepCurves.length}
              <ResponsePlot curves={stepCurves} xUnit={unitText("s", app.locale)} level={1} label={t("tune.step.title", { axis: axisName(axis) })} />
            {:else}
              <p class="faint">{v.this.curves?.why ? t("tune.curves.none") : t("tune.step.none")}</p>
            {/if}
            <div class="figures">
              {#each ["rise_time", "overshoot", "settling"] as name (name)}
                {@const f = figure(name)}
                {#if f}
                  <div><span class="faint">{f.label}</span><span class="mono">{f.before}{" → "}{f.after}</span>{#if f.range}<span class="faint mono">{f.range}</span>{/if}</div>
                {/if}
              {/each}
            </div>
          </section>

          <section class="panel">
            <h2>{t("tune.freq.title", { axis: axisName(axis).toLocaleLowerCase() })}</h2>
            <span class="legend">
              {#if v.compare?.curves?.frequency}<span class="key faint">┅ {when(v.compare.utc_us)}</span>{/if}
              <span class="key accent">━ {t("tune.this")}</span>
              <span class="key faint">▮ {t("tune.legend.unmeasured")}</span>
            </span>
            {#if freqCurves.length}
              <ResponsePlot
                curves={freqCurves}
                logX
                xUnit={unitText("Hz", app.locale)}
                yUnit={unitText("dB", app.locale)}
                level={-3}
                marks={bandwidths}
                unmeasuredFrom={v.this.curves?.frequency?.measured_to_hz ?? null}
                label={t("tune.freq.title", { axis: axisName(axis) })}
              />
            {:else}
              <p class="faint">{t("tune.curves.none")}</p>
            {/if}
            <div class="figures">
              {#each ["bandwidth", "sensitivity_peak"] as name (name)}
                {@const f = figure(name)}
                {#if f}
                  <div><span class="faint">{f.label}</span><span class="mono">{f.before}{" → "}{f.after}</span>{#if f.range}<span class="faint mono">{f.range}</span>{/if}</div>
                {/if}
              {/each}
            </div>
          </section>
        </div>

        <section class="panel">
          <div class="row">
            <h2>{t("tune.inputs.title")}</h2>
            <span class="legend">
              <span class="key accent">▮ {t("tune.inputs.counted")}</span>
              <span class="key caution">▯ {t("tune.inputs.leftOut")}</span>
            </span>
          </div>
          {#if v.this.inputs?.axes && armed > 0}
            {@const inputs = v.this.inputs}
            <svg viewBox="0 0 1080 {AXES.length * 30 + 22}" class="timeline" role="img" aria-label={t("tune.inputs.title")}>
              {#each AXES as a, i (a)}
                <text x="0" y={i * 30 + 19} class="lane-label">{axisName(a)}</text>
                {#each inputs.axes?.[a] ?? [] as e, k (k)}
                  <rect
                    x={80 + x(e.start_s)}
                    y={i * 30 + 6}
                    width={Math.max(x(e.end_s) - x(e.start_s), 2)}
                    height="18"
                    rx="2"
                    class={e.counted ? "counted" : "left"}
                  ><title>{clock(e.start_s)}{e.reason ? ` · ${reasonText(e.reason)}` : ""}</title></rect>
                {/each}
              {/each}
              {#each [0, 0.25, 0.5, 0.75, 1] as share (share)}
                <text x={80 + share * 1000} y={AXES.length * 30 + 18} class="tick" text-anchor={share === 0 ? "start" : share === 1 ? "end" : "middle"}>{clock(share * armed)}</text>
              {/each}
            </svg>
            {#if notes.length}
              {#each notes as n (n.reason)}
                <p class="notice">▲ {t("tune.inputs.left", { time: clock(n.first_s), axes: n.axes.map(axisName).join(", ").toLocaleLowerCase(), count: n.count, reason: reasonText(n.reason) })}</p>
              {/each}
            {:else}
              <p class="faint">{t("tune.inputs.every")}</p>
            {/if}
          {:else}
            <p class="faint">{t("tune.inputs.none")}</p>
          {/if}
        </section>
      {/if}

      <section class="panel">
        <div class="row">
          <h2>{t("tune.gate.title")}</h2>
          {#if gateState}
            <span style:color="var({STATUS[gateState.tone].tone})">
              {STATUS[gateState.tone].glyph} {gateState.text}{#if items.length} · {t("tune.gate.summary", { met, total: items.length })}{/if}
            </span>
          {/if}
        </div>
        {#if !v.gate}
          <p class="faint">{t("tune.gate.none")}</p>
        {:else}
          {#if v.gate.graded_flight}<p class="faint">{t("tune.gate.graded", { flight: flightLabel(v.gate.graded_flight) })}</p>{/if}
          <ul class="gate">
            {#each items as item, i (i)}
              <li>
                <span class="glyph" style:color="var({STATUS[item.status].tone})">{STATUS[item.status].glyph}</span>
                <span class="item">
                  <strong>{item.title}</strong>
                  {#if item.detail}<span class="faint">{item.detail}</span>{/if}
                </span>
              </li>
            {/each}
          </ul>
        {/if}
      </section>
    </div>
  </div>
{/if}

<style>
  .about {
    margin: 0 0 10px;
  }
  .layout {
    display: grid;
    grid-template-columns: minmax(240px, 300px) minmax(0, 1fr);
    gap: 12px;
    align-items: start;
  }
  @media (max-width: 900px) {
    .layout {
      grid-template-columns: minmax(0, 1fr);
    }
  }
  .main {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
  }
  .pair {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 12px;
  }
  .panel {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 14px 16px;
    background: var(--surface);
    border: 1px solid var(--border-soft);
    border-radius: 12px;
    min-width: 0;
  }
  .side {
    gap: 12px;
  }
  h2 {
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
  .notice {
    color: var(--caution);
    font-size: 13px;
  }
  .error {
    color: var(--out);
  }
  .mono {
    font-family: var(--mono, ui-monospace, monospace);
    font-size: 12px;
  }
  .row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  select {
    background: var(--surface-2, var(--surface));
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 8px;
  }
  .toggle {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
  }
  .toggle button {
    background: none;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text-2);
    padding: 4px 12px;
    cursor: pointer;
    font-size: 12px;
  }
  .toggle button[aria-pressed="true"] {
    border-color: var(--accent);
    color: var(--text);
  }
  table {
    border-collapse: collapse;
    font-size: 12px;
  }
  th {
    text-align: left;
    font-weight: 500;
    color: var(--text-muted);
    padding: 2px 10px 2px 0;
  }
  td {
    padding: 3px 10px 3px 0;
    border-top: 1px solid var(--border-soft);
  }
  .card {
    display: flex;
    flex-direction: column;
    gap: 8px;
    border-top: 1px solid var(--border-soft);
    padding-top: 10px;
  }
  .legend {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    font-size: 12px;
  }
  .accent {
    color: var(--accent);
  }
  .caution {
    color: var(--caution);
  }
  .rows {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .axis-row {
    display: grid;
    grid-template-columns: 56px minmax(0, 1fr) minmax(150px, 220px);
    align-items: center;
    gap: 10px;
    padding: 4px 0;
    border-bottom: 1px solid var(--border-soft);
  }
  .axis-name {
    font-size: 13px;
  }
  .track {
    position: relative;
    height: 30px;
  }
  .lane {
    position: absolute;
    left: 0;
    right: 0;
    height: 10px;
  }
  .lane.compare {
    top: 2px;
    color: var(--text-faint);
  }
  .lane.this {
    top: 17px;
    color: var(--accent);
  }
  .interval {
    position: absolute;
    top: 4px;
    height: 2px;
    background: currentColor;
  }
  .dot {
    position: absolute;
    top: 0;
    width: 10px;
    height: 10px;
    margin-left: -5px;
    border-radius: 50%;
    background: var(--surface);
    border: 2px solid currentColor;
    box-sizing: border-box;
  }
  .lane.this .dot {
    background: currentColor;
  }
  .values {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 2px;
    text-align: right;
  }
  .verdict {
    font-size: 12px;
  }
  .scale-row {
    border-bottom: none;
    padding: 0;
  }
  .scale {
    position: relative;
    height: 14px;
    font-size: 11px;
    color: var(--text-faint);
  }
  .scale span {
    position: absolute;
    transform: translateX(-50%);
  }
  .scale span:first-child {
    transform: none;
  }
  .scale span:last-child {
    transform: translateX(-100%);
  }
  .figures {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
    gap: 8px;
  }
  .figures div {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .timeline {
    width: 100%;
    height: auto;
  }
  .timeline .lane-label,
  .timeline .tick {
    fill: var(--text-faint);
    font-size: 13px;
  }
  .timeline .counted {
    fill: var(--accent);
  }
  .timeline .left {
    fill: none;
    stroke: var(--caution);
    stroke-width: 2;
  }
  .gate {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 13px;
  }
  .gate li {
    display: flex;
    gap: 8px;
  }
  .glyph {
    flex: none;
  }
  strong {
    font-weight: 500;
  }
  .item {
    display: flex;
    flex-direction: column;
    gap: 1px;
  }
  .primary {
    background: var(--accent);
    color: var(--accent-text);
    border: none;
    border-radius: 8px;
    padding: 8px 14px;
    font-weight: 600;
    cursor: pointer;
  }
  .secondary {
    align-self: flex-start;
    background: none;
    color: var(--text-2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 12px;
    cursor: pointer;
  }
</style>
