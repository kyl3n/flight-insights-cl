<script lang="ts">
  // The flight's motor-out margin and payload in the Propulsion view (spec: Propulsion,
  // "Motor-out margin" and "Payload"; mock-up Propulsion): each motor stopped in turn and
  // what the busiest of the others would need, placed on the frame as ArduPilot's mixer
  // sets it out; the heaviest take-off that meets the target; and the payload band this
  // flight fell in, beside the airframe's others. The numbers are the analyzers'.
  import { app, t } from "../lib/app.svelte";
  import type { MessageKey } from "../lib/i18n";
  import {
    fill,
    flightMargin,
    isSkipped,
    lossLook,
    partners,
    side,
    stations,
    type MarginView,
    type MotorOut,
    type Payload,
  } from "../lib/margin";
  import { formatNumber } from "../lib/report";
  import { engineCall } from "../lib/shell";
  import { STATUS } from "../lib/status";
  import { groupName, type PayloadGroups } from "../lib/trends";
  import StatusBadge from "./StatusBadge.svelte";

  let { flightId }: { flightId: string } = $props();

  let view = $state<MarginView | null>(null);
  let bands = $state<PayloadGroups | null>(null);
  let error = $state("");

  $effect(() => {
    void flightId;
    view = null;
    bands = null;
    flightMargin(flightId)
      .then(async (v) => {
        view = v;
        if (v.airframe && v.payload && !isSkipped(v.payload)) {
          bands = await engineCall<PayloadGroups>("trend.payloads", { airframe_id: v.airframe });
        }
      })
      .catch((e: unknown) => (error = String(e)));
  });

  const num = (v: number | null | undefined, digits = 1) =>
    v === null || v === undefined || !Number.isFinite(v) ? "–" : formatNumber(Math.round(v * 10 ** digits) / 10 ** digits, app.locale);
  const pct = (v: number | null | undefined, digits = 1) => (v === null || v === undefined ? "–" : `${num(v, digits)}%`);
  const sideName = (m: { roll: number; pitch: number }) => t(`margin.side.${side(m)}` as MessageKey);
</script>

{#if error}<p class="error">{error}</p>{/if}

{#if view?.motor_out}
  {@const mo = view.motor_out}
  <section class="panel" aria-labelledby="mo-title">
    <div class="head">
      <h2 id="mo-title">{t("margin.title")}</h2>
      <span class="faint">{t("margin.about")}</span>
    </div>
    {#if isSkipped(mo)}
      <p class="faint">{t("margin.skipped", { why: mo.skipped ?? "" })}</p>
    {:else}
      {@render motorOut(mo)}
    {/if}
  </section>
{/if}

{#if view?.payload}
  {@const p = view.payload}
  <section class="panel" aria-labelledby="pl-title">
    <div class="head">
      <h2 id="pl-title">{t("payload.title")}</h2>
      <span class="faint">{t("payload.about")}</span>
    </div>
    {#if isSkipped(p)}
      <p class="faint">{t("payload.skipped", { why: p.skipped ?? "" })}</p>
    {:else}
      {@render payload(p)}
    {/if}
  </section>
{/if}

{#snippet motorOut(mo: MotorOut)}
  {@const worst = mo.motors.find((m) => m.motor === mo.worst_motor)}
  {@const short = mo.finding !== null}
  <p class="faint">
    {t(mo.hold_yaw ? "margin.target.yaw" : "margin.target", { frame: mo.frame, target: num(mo.target_pct, 0) })}
  </p>
  <div class="split">
    <div class="cards">
      <div class="callout" class:short>
        <p class="kicker" style:color="var({STATUS[short ? "caution" : "ok"].tone})">
          {STATUS[short ? "caution" : "ok"].glyph}
          {t("margin.worst", { motor: mo.worst_motor })}
        </p>
        {#if mo.worst_output_pct !== undefined && mo.busiest_motor !== undefined}
          <p>
            {t("margin.worst.text", {
              motor: mo.worst_motor,
              busiest: mo.busiest_motor,
              needed: num(mo.worst_output_pct),
              target: num(mo.target_pct, 0),
            })}
            {#if worst && partners(mo.motors, mo.worst_motor).includes(mo.busiest_motor)}{t("margin.sameArm")}{/if}
          </p>
          {#if mo.best_split_output_pct != null}
            <p class="faint">{t("margin.best", { best: num(mo.best_split_output_pct) })}</p>
          {/if}
        {:else}
          <p>{t("margin.noBalance", { motor: mo.worst_motor })}</p>
        {/if}
      </div>
      <div class="card">
        <p class="faint">{t("margin.heaviest")}</p>
        {#if mo.heaviest_kg !== null}
          <p class="big mono">{num(mo.heaviest_kg)} kg</p>
          <p class="faint">{t(mo.weight_from === "weighed" ? "margin.heaviest.weighed" : "margin.heaviest.estimated", { ratio: num(mo.weight_ratio, 2) })}</p>
        {:else if mo.weight_ratio !== null}
          <p class="big mono">×{num(mo.weight_ratio, 2)}</p>
          <p class="faint">{t("margin.heaviest.ratio")}</p>
        {:else}
          <p class="faint">–</p>
        {/if}
      </div>
    </div>
    {@render frame(mo)}
  </div>
  <div class="scroll">
    <table>
      <thead>
        <tr>
          <th scope="col">{t("margin.col.stopped")}</th>
          <th scope="col">{t("margin.col.position")}</th>
          <th scope="col">{t("margin.col.busiest")}</th>
          <th scope="col"><span class="sr">{t("margin.col.verdict")}</span></th>
        </tr>
      </thead>
      <tbody>
        {#each mo.motors as m (m.motor)}
          {@const look = lossLook(m.needed_pct, mo.target_pct)}
          {@const pair = partners(mo.motors, m.motor)}
          <tr class:worst={m.motor === mo.worst_motor}>
            <td class="mono">{t("prop.motor", { n: m.motor })}</td>
            <td>
              {sideName(m)}
              {#if pair.length}<span class="faint"> · {t("margin.pair", { motors: pair.join(", ") })}</span>{/if}
            </td>
            <td>
              <span class="bar" aria-hidden="true">
                <span class="fill" style:width="{fill(m.needed_pct)}%" style:background="var({STATUS[look.status].tone})"></span>
                <span class="target" style:left="{fill(mo.target_pct)}%"></span>
              </span>
              <span class="mono">{pct(m.needed_pct)}</span>
            </td>
            <td class="end"><StatusBadge status={look.status} label={t(look.label)} /></td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
  <p class="faint">{t("margin.note", { hover: num(mo.hover_thrust_pct) })}</p>
{/snippet}

{#snippet frame(mo: MotorOut)}
  {@const arms = stations(mo.motors)}
  {@const worstArm = arms.find((s) => s.motors.includes(mo.worst_motor))}
  <figure class="frame">
    <svg viewBox="-130 -130 260 260" role="img" aria-label={t("margin.frame", { frame: mo.frame })}>
      <path d="M 0 -26 L 9 -10 L -9 -10 Z" class="nose" />
      {#each arms as s (s.bearing)}
        <line x1="0" y1="0" x2={s.x * 82} y2={s.y * 82} class="arm" />
      {/each}
      {#each arms as s (s.bearing)}
        {@const isWorst = s === worstArm}
        <g transform="translate({s.x * 95} {s.y * 95})">
          <circle r="27" class:worst={isWorst} class="rotor" />
          <text text-anchor="middle" dominant-baseline="central" class:worst={isWorst}>{s.motors.join(" · ")}</text>
        </g>
      {/each}
    </svg>
    <figcaption class="faint">
      {t("margin.frame.caption")}
      {#if worstArm}<span class="worst-text">{t("margin.frame.worst", { motor: mo.worst_motor })}</span>{/if}
    </figcaption>
  </figure>
{/snippet}

{#snippet payload(p: Payload)}
  <div class="cards">
    <div class="card">
      <p class="faint">{t("payload.hover")}</p>
      <p class="big mono">{pct(p.hover_thrust_pct)}</p>
      <p class="faint">{t("payload.hover.about", { seconds: num(p.seconds, 0) })}</p>
    </div>
    <div class="card">
      <p class="faint">{t(p.takeoff_kg !== null ? "payload.weighed" : "payload.estimated")}</p>
      <p class="big mono">{p.takeoff_kg !== null ? num(p.takeoff_kg) : num(p.estimated_kg)} kg</p>
      <p class="faint">
        {#if p.takeoff_kg !== null && p.estimated_kg !== null}{t("payload.estimate.beside", { kg: num(p.estimated_kg) })}
        {:else if p.takeoff_kg === null && p.estimated_kg === null}{t("payload.weighNone")}
        {:else if p.takeoff_kg === null}{t("payload.fromWeighed")}{/if}
      </p>
    </div>
    <div class="card">
      <p class="faint">{t("payload.density")}</p>
      <p class="big mono">{num(p.density_ratio, 3)}</p>
      <p class="faint">{t("payload.density.about")}</p>
    </div>
  </div>
  {#if bands && bands.groups.length}
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th scope="col">{t("payload.col.band")}</th>
            <th scope="col">{t("payload.col.thrust")}</th>
            <th scope="col">{t("payload.col.kg")}</th>
            <th scope="col">{t("payload.col.flights")}</th>
            <th scope="col">{t("payload.col.labels")}</th>
          </tr>
        </thead>
        <tbody>
          {#each bands.groups as g (g.group)}
            <tr class:mine={g.group === p.group}>
              <td>
                {groupName(g, app.locale)}{#if g.group === p.group}<span class="this"> · {t("payload.thisFlight")}</span>{/if}
              </td>
              <td class="mono">{"band" in g ? `${num(g.low)}–${num(g.high)}%` : "–"}</td>
              <td class="mono">{"band" in g && g.kg ? `${num(g.kg[0])}–${num(g.kg[1])} kg` : "–"}</td>
              <td class="mono">{g.flights}</td>
              <td>{g.labels.join(", ") || "–"}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#if bands.check.length}
      {@const worstError = Math.max(...bands.check.map((c) => Math.abs(c.error_pct)))}
      <p class="check">
        <StatusBadge status="ok" label={t("payload.check")} />
        <span class="faint">{t("payload.check.text", { n: bands.check.length, pct: num(worstError) })}</span>
      </p>
    {/if}
  {/if}
{/snippet}

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
    gap: 10px;
    align-items: baseline;
    flex-wrap: wrap;
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
  .error {
    color: var(--out);
  }
  .split {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    align-items: flex-start;
  }
  .cards {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    min-width: 0;
  }
  .split > .cards {
    flex: 1 1 340px;
  }
  .card,
  .callout {
    flex: 1 1 200px;
    padding: 12px 14px;
    border: 1px solid var(--border-soft);
    border-radius: 10px;
    background: var(--surface-2);
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 13px;
  }
  .callout.short {
    border-color: color-mix(in srgb, var(--caution) 55%, transparent);
    background: color-mix(in srgb, var(--caution) 8%, var(--surface-2));
  }
  .kicker {
    font-weight: 600;
    font-size: 12px;
  }
  .big {
    font-size: 20px;
  }
  .frame {
    margin: 0;
    flex: 0 1 240px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    align-items: center;
  }
  .frame svg {
    width: 200px;
    max-width: 100%;
    height: auto;
  }
  .arm {
    stroke: var(--text-muted);
    stroke-width: 6;
    stroke-linecap: round;
  }
  .nose {
    fill: var(--accent);
  }
  .rotor {
    fill: var(--surface-2);
    stroke: var(--border);
    stroke-width: 2;
  }
  .rotor.worst {
    stroke: var(--out);
    stroke-dasharray: 5 4;
  }
  svg text {
    fill: var(--text);
    font-family: var(--mono);
    font-size: 14px;
  }
  svg text.worst {
    fill: var(--out);
  }
  .worst-text {
    color: var(--out);
    margin-left: 4px;
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
  td.end {
    text-align: right;
  }
  tr.worst td,
  tr.mine td {
    background: color-mix(in srgb, var(--accent) 6%, transparent);
  }
  .this {
    color: var(--accent);
  }
  .bar {
    position: relative;
    display: inline-block;
    width: 120px;
    height: 6px;
    border-radius: 3px;
    background: var(--border-soft);
    margin-right: 8px;
    vertical-align: middle;
  }
  .fill {
    position: absolute;
    inset: 0 auto 0 0;
    border-radius: 3px;
  }
  .target {
    position: absolute;
    top: -3px;
    bottom: -3px;
    width: 2px;
    background: var(--text-muted);
  }
  .check {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }
  .sr {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
  }
  @media (max-width: 520px) {
    .bar {
      width: 60px;
    }
  }
</style>
