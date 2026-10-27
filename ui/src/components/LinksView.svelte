<script lang="ts">
  // The links of one flight (spec: Links, ground station and whole-system faults, "The
  // Links tab"; mock-up Links): what the link analyzer measured, the findings on the
  // links, the failsafe settings for the link with what losing it does in each mode,
  // then the downlink, the uplink and the joystick, the radio and the receiver over time,
  // and what each kind of radio reports. Built from ArduPilot's and the ground station's
  // records: the radios' own logs come later. Every verdict and every worded line is the
  // engine's.
  import { app, t } from "../lib/app.svelte";
  import type { MessageKey } from "../lib/i18n";
  import {
    delayStation,
    linkFindings,
    linkMetrics,
    longestSpan,
    metricNote,
    radioRows,
    settingKey,
    stationColour,
    valueNote,
    verdictNote,
    worstLoss,
    type LinksView,
    type Note,
    type Span,
  } from "../lib/links";
  import { findingAction, findingTitle, formatNumber, severityLook, unitText, type Analysis, type Registry } from "../lib/report";
  import { engineCall } from "../lib/shell";
  import { STATUS } from "../lib/status";
  import StatusBadge from "./StatusBadge.svelte";
  import TimePlot from "./TimePlot.svelte";
  import Value from "./Value.svelte";

  let {
    flightId,
    analysis,
    registry,
    airframeId,
  }: { flightId: string; analysis: Analysis | null; registry: Registry | null; airframeId: string | null } = $props();

  let view = $state<LinksView | null>(null);
  let error = $state("");

  $effect(() => {
    const id = flightId;
    view = null;
    error = "";
    engineCall<LinksView>("flight.links", { flight_id: id })
      .then((v) => (view = v))
      .catch((e: unknown) => (error = String(e)));
  });

  const num = (v: number) => formatNumber(v, app.locale);
  const unit = (v: number, u: string) => `${num(v)} ${unitText(u, app.locale)}`;
  const say = (n: Note) => t(n.key, n.vars);
  const spans = (list: Span[]) => list.map((s) => t("links.span", { from: num(s.from_s), to: num(s.to_s) })).join(", ");

  const metrics = $derived(analysis ? linkMetrics(analysis) : []);
  const findings = $derived(analysis ? linkFindings(analysis) : []);
  const evidence = $derived(new Map((analysis?.evidence ?? []).map((e) => [e.id, e])));
  const armed = $derived<[number, number]>(view ? [0, view.armed_s] : [0, 1]);
  const delay = $derived(view?.downlink ? delayStation(view.downlink.stations) : null);
  const lapse = $derived(view?.joystick ? longestSpan(view.joystick.lapses) : null);
  const sources = $derived.by(() => {
    if (!view) return "";
    const parts = [view.tlogs ? t("links.source.tlogs", { count: view.tlogs }) : t("links.source.noTlog")];
    if (view.uplink) parts.push("MAV");
    if (view.radio) parts.push(view.radio.reported_by === "aircraft" ? "RAD" : "RADIO_STATUS");
    if (view.receiver.rssi || view.receiver.lq) parts.push("RSSI");
    if (view.joystick || view.receiver.failsafes) parts.push("RCI2");
    return parts.join(" · ");
  });
</script>

{#if error}<p class="error">{error}</p>{/if}
{#if !view && !error}<p class="faint">{t("report.analysing")}</p>{/if}

{#if view}
  {@const fs = view.failsafes}
  <div class="stack">
    <p class="muted">{t(view.control === "station" ? "links.intro.station" : "links.intro.receiver")}</p>
    <ul class="facts" aria-label={t("links.fact.sources")}>
      <li><span class="faint">{t("links.fact.control")}</span><span>{t(`links.control.${view.control}`)}</span></li>
      <li><span class="faint">{t("links.fact.radio")}</span><span>{view.radio_kind ?? t("links.radio.unset")}</span></li>
      <li><span class="faint">{t("links.fact.sources")}</span><span class="mono">{sources}</span></li>
    </ul>

    <section class="panel" aria-labelledby="links-measures">
      <h2 id="links-measures">{t("links.measures")}</h2>
      {#if metrics.length === 0 && !delay && !view.joystick}
        <p class="faint">{t("links.measures.none")}</p>
      {/if}
      <div class="kpis">
        {#if registry}
          {#each metrics as m (m.metric)}
            {@const info = registry.metrics[m.metric]}
            {@const note = metricNote(m.metric, evidence.get(m.evidence), num)}
            {#if info}
              <div class="kpi">
                <Value id={m.metric} metric={info} value={m.value} zone={m.zone} context={t("segment.flight")} />
                {#if note}<span class="sub">{say(note)}</span>{/if}
              </div>
            {/if}
          {/each}
        {/if}
        {#if delay && delay.delay_ms}
          <div class="kpi stat">
            <span class="name">{t("links.kpi.delay")}</span>
            <span class="number mono">{unit(delay.delay_ms[0], "ms")}</span>
            <span class="sub">{t("links.kpi.delay.note", { p95: num(delay.delay_ms[1]), n: delay.station })}</span>
          </div>
        {/if}
        {#if view.joystick}
          <div class="kpi stat">
            <span class="name">{t("links.kpi.joystick")}</span>
            <span class="number mono">{num(view.joystick.held_pct)}%</span>
            <span class="sub">{t("links.kpi.joystick.note", { s: lapse === null ? "0" : num(lapse) })}</span>
          </div>
        {/if}
      </div>
    </section>

    {#if analysis && registry}
      {@const reg = registry}
      <section class="panel" aria-labelledby="links-findings">
        <h2 id="links-findings">{t("links.findings")}</h2>
        {#if findings.length === 0}
          <p class="faint">{t("links.findings.none")}</p>
        {:else}
          <ul class="findings">
            {#each findings as f (f.id)}
              {@const look = severityLook(f.severity)}
              <li>
                <span class="sev" style:color="var({STATUS[look.status].tone})">{STATUS[look.status].glyph} {t(look.label)}</span>
                <span class="title">{findingTitle(reg, f, app.locale)}</span>
                <span class="faint">{findingAction(reg, f, app.locale)}</span>
              </li>
            {/each}
          </ul>
        {/if}
      </section>
    {/if}

    <section class="panel" aria-labelledby="links-failsafes">
      <div class="head">
        <h2 id="links-failsafes">{t("links.failsafes")}</h2>
        {#if "control" in fs}<span class="faint">{t(`links.failsafes.${fs.control}`)}</span>{/if}
      </div>
      {#if "skipped" in fs}
        <p class="faint">{t("links.failsafes.copterOnly")}</p>
      {:else}
        <div class="scroll">
          <table class="rows" aria-label={t("links.failsafes")}>
            <thead>
              <tr><th scope="col">{t("links.col.setting")}</th><th scope="col">{t("links.col.value")}</th><th scope="col" class="right">{t("links.col.status")}</th></tr>
            </thead>
            <tbody>
              {#each fs.settings as row (row.param)}
                <tr>
                  <td><span class="mono param">{row.param}</span> · {t(settingKey(row, fs.control))}</td>
                  <td>{say(valueNote(row, num))}</td>
                  <td class="right"><StatusBadge status={row.status} label={say(verdictNote(row, num))} /></td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
        <div class="head">
          <h3>{t("links.losses")}</h3>
          <StatusBadge status={worstLoss(fs.losses)} />
        </div>
        <div class="losses">
          {#each fs.losses as loss (loss.id)}
            <div class="loss" class:alert={loss.status === "out"}>
              <h4>{loss.title[app.locale]}</h4>
              <ul>
                {#each loss.lines as line, i (i)}<li>{line[app.locale]}</li>{/each}
              </ul>
              {#if loss.alone.length}
                <p class="out"><span aria-hidden="true">{STATUS.out.glyph}</span> {t("links.losses.alone", { modes: loss.alone_names.join(", ") })}</p>
              {/if}
            </div>
          {/each}
        </div>
        <p class="faint">
          {t("links.losses.about")}
          {#if airframeId}
            <button type="button" class="link" onclick={() => (app.recoveryCard = airframeId)}>{t("links.recovery")}</button>
          {/if}
        </p>
      {/if}
    </section>

    <section class="panel" aria-labelledby="links-down">
      <h2 id="links-down">{t("links.down")}</h2>
      {#if view.tlogs_missing}<p class="notice">{t("links.down.missing", { count: view.tlogs_missing })}</p>{/if}
      {#if !view.downlink}
        <p class="faint">{t("links.down.none")}</p>
      {:else}
        {@const down = view.downlink}
        <TimePlot
          traces={down.stations.map((s) => ({ label: t("links.station", { n: s.station }), env: s.silence, colour: stationColour(s.station) }))}
          unit="s"
          range={view.window_s}
          {armed}
          modes={view.modes}
          errors={down.stations.flatMap((s) => s.outage_list.map((o) => o.from_s))}
          level={down.outage_s}
          label={t("links.down.plot")}
          height={150}
        />
        <p class="faint">{t("links.down.plot")}. {t("links.down.level", { s: num(down.outage_s) })}</p>
        <div class="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">{t("links.col.station")}</th>
                <th scope="col">{t("links.col.received")}</th>
                <th scope="col">{t("links.col.lost")}</th>
                <th scope="col">{t("links.col.loss")}</th>
                <th scope="col">{t("links.col.outOfOrder")}</th>
                <th scope="col">{t("links.col.longest")}</th>
                <th scope="col">{t("links.col.clock")}</th>
                <th scope="col">{t("links.col.delay")}</th>
              </tr>
            </thead>
            <tbody>
              {#each down.stations as s (s.station)}
                <tr>
                  <td><span class="swatch" style:background={stationColour(s.station)}></span>{t("links.station", { n: s.station })}</td>
                  <td class="mono">{num(s.received)}</td>
                  <td class="mono">{num(s.lost)}</td>
                  <td class="mono">{num(s.loss_pct)}%</td>
                  <td class="mono">{num(s.out_of_order)}</td>
                  <td class="mono">{unit(s.longest_s, "s")}</td>
                  <td>{t(`links.clock.${s.clock}` as MessageKey)}</td>
                  <td class="mono">{s.delay_ms ? t("links.delay", { median: num(s.delay_ms[0]), p95: num(s.delay_ms[1]) }) : "–"}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
        {@const outages = down.stations.flatMap((s) => s.outage_list)}
        <p>{outages.length ? t("links.down.outages", { list: spans(outages) }) : t("links.down.noOutage")}</p>
      {/if}
    </section>

    <section class="panel" aria-labelledby="links-up">
      <h2 id="links-up">{t("links.up")}</h2>
      {#if !view.uplink}
        <p class="faint">{t("links.up.none")}</p>
      {:else}
        {@const up = view.uplink}
        {#if up.unheard && up.timeout_s !== undefined}
          <TimePlot
            traces={[{ label: t("links.up.plot"), env: up.unheard, colour: "#4A9FD8" }]}
            unit="s"
            range={view.window_s}
            {armed}
            modes={view.modes}
            level={up.timeout_s}
            label={t("links.up.plot")}
            height={150}
          />
          <p class="faint">{t("links.up.plot")}. {t("links.up.level", { s: num(up.timeout_s) })}</p>
          {#if up.longest_s !== undefined && up.from_s !== undefined}
            <p>{t("links.up.longest", { s: num(up.longest_s), from: num(up.from_s) })}</p>
          {/if}
        {:else}
          <p class="faint">{t("links.up.noGcs")}</p>
        {/if}
        {#if up.links.length}
          <div class="scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("links.col.link")}</th>
                  <th scope="col">{t("links.col.received")}</th>
                  <th scope="col">{t("links.col.missed")}</th>
                  <th scope="col">{t("links.col.loss")}</th>
                </tr>
              </thead>
              <tbody>
                {#each up.links as l (l.chan)}
                  <tr>
                    <td>{t("links.link", { n: l.chan })}</td>
                    <td class="mono">{num(l.received)}</td>
                    <td class="mono">{num(l.missed)}</td>
                    <td class="mono">{num(l.loss_pct)}%</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {:else if up.unheard}
          <p class="faint">{t("links.up.few")}</p>
        {/if}
      {/if}
      {#if view.joystick}
        <h3>{t("links.joy")}</h3>
        <p>{t("links.joy.held", { pct: num(view.joystick.held_pct) })}</p>
        <p>
          {view.joystick.lapses.length
            ? t("links.joy.lapses", { list: spans(view.joystick.lapses), s: num(view.joystick.override_time_s) })
            : t("links.joy.noLapse")}
        </p>
      {:else if view.control === "station"}
        <h3>{t("links.joy")}</h3>
        <p class="faint">{t("links.joy.none")}</p>
      {/if}
    </section>

    <section class="panel" aria-labelledby="links-radio">
      <h2 id="links-radio">{t("links.radio")}</h2>
      {#if !view.radio}
        <p class="faint">{t("links.radio.none")}</p>
      {:else}
        {@const radio = view.radio}
        <p class="faint">{t(radio.reported_by === "aircraft" ? "links.radio.aircraft" : "links.radio.ground")}</p>
        <h3>{t("links.radio.txbuf")}</h3>
        <TimePlot
          traces={[{ label: t("links.radio.txbuf"), env: radio.txbuf, colour: "#4A9FD8" }]}
          unit="%"
          range={view.window_s}
          {armed}
          modes={view.modes}
          level={radio.slows_streams}
          label={t("links.radio.txbuf")}
          height={140}
        />
        <p class="faint">{t("links.radio.txbufLevel", { pct: num(radio.slows_streams) })}</p>
        {#if radio.fade_db}
          {@const ends = (["aircraft", "ground"] as const).filter((e) => radio.fade_db?.[e])}
          <h3>{t("links.radio.fade")}</h3>
          <TimePlot
            traces={ends.map((e, i) => ({ label: t(`links.end.${e}`), env: radio.fade_db![e]!, colour: i ? "#E0952B" : "#4A9FD8" }))}
            unit="dB"
            range={view.window_s}
            {armed}
            modes={view.modes}
            level={radio.target_db ?? null}
            label={t("links.radio.fade")}
            height={140}
          />
          <p class="faint">
            {#each ends as e, i (e)}<span class="swatch" style:background={i ? "#E0952B" : "#4A9FD8"}></span>{t(`links.end.${e}`)}{" "}{/each}
            {radio.target_db != null ? t("links.radio.fadeLevel", { db: num(radio.target_db) }) : ""}
          </p>
        {:else}
          <p class="faint">{t("links.radio.notSik")}</p>
        {/if}
      {/if}
    </section>

    <section class="panel" aria-labelledby="links-rc">
      <h2 id="links-rc">{t("links.rc")}</h2>
      {#if view.receiver.ignored}
        <p class="faint">{t("links.rc.ignored")}</p>
      {:else}
        {@const rx = view.receiver}
        {#if rx.rssi || rx.lq}
          <TimePlot
            traces={[
              ...(rx.rssi ? [{ label: t("links.rc.rssi"), env: rx.rssi, colour: "#4A9FD8" }] : []),
              ...(rx.lq ? [{ label: t("links.rc.lq"), env: rx.lq, colour: "#E0952B" }] : []),
            ]}
            unit="%"
            range={view.window_s}
            {armed}
            modes={view.modes}
            errors={(rx.failsafes ?? []).map((s) => s.from_s)}
            label={t("links.rc.plot")}
            height={140}
          />
          <p class="faint">
            {#if rx.rssi}<span class="swatch" style:background="#4A9FD8"></span>{t("links.rc.rssi")}{" "}{/if}
            {#if rx.lq}<span class="swatch" style:background="#E0952B"></span>{t("links.rc.lq")}{/if}
          </p>
        {:else}
          <p class="faint">{t("links.rc.noSignal")}</p>
        {/if}
        {#if rx.failsafes}
          <p>{rx.failsafes.length ? t("links.rc.failsafes", { list: spans(rx.failsafes) }) : t("links.rc.noFailsafes")}</p>
        {/if}
      {/if}
    </section>

    <section class="panel" aria-labelledby="links-quality">
      <div class="head">
        <h2 id="links-quality">{t("links.quality")}</h2>
        <span class="faint">{t("links.quality.sub")}</span>
      </div>
      <div class="scroll">
        <table class="rows">
          <thead>
            <tr><th scope="col">{t("links.col.radio")}</th><th scope="col">{t("links.col.reads")}</th><th scope="col" class="right">{t("links.col.flight")}</th></tr>
          </thead>
          <tbody>
            {#each radioRows(view) as r (r.id)}
              <tr>
                <td>
                  {t(`links.quality.${r.id}`)}
                  {#if r.mine}<span class="faint"> · {t("links.quality.mine")}</span>{/if}
                </td>
                <td>{t(`links.quality.${r.id}.reads`)}</td>
                <td class="right"><StatusBadge status={r.status} label={t(`links.reading.${r.reading}`)} /></td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <p class="faint">{t("links.quality.later")}</p>
    </section>

    <p class="faint">{t("links.legend")}</p>
  </div>
{/if}

<style>
  .stack {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
  }
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
    flex-wrap: wrap;
    align-items: baseline;
    gap: 6px 12px;
  }
  h2 {
    margin: 0;
    font-size: 15px;
  }
  h3 {
    margin: 4px 0 0;
    font-size: 13px;
  }
  h4 {
    margin: 0 0 4px;
    font-size: 13px;
    font-weight: 600;
  }
  p {
    margin: 0;
  }
  .muted {
    color: var(--text-muted);
    font-size: 13px;
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
  }
  .error,
  .out {
    color: var(--out);
  }
  .notice {
    color: var(--caution);
    font-size: 13px;
  }
  .facts {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .facts li {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding: 4px 10px;
    border-radius: 7px;
    background: var(--surface);
    border: 1px solid var(--border-soft);
    font-size: 12px;
    min-width: 0;
  }
  .kpis {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
    gap: 10px;
  }
  .kpi {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }
  .kpi :global(.wrap),
  .kpi :global(.value) {
    width: 100%;
    box-sizing: border-box;
  }
  .stat {
    padding: 8px 12px;
    background: var(--surface-2);
    border: 1px solid var(--border-soft);
    border-radius: 10px;
  }
  .stat .name {
    font-size: 12px;
    color: var(--text-muted);
  }
  .stat .number {
    font-size: 17px;
  }
  .sub {
    font-size: 12px;
    color: var(--text-faint);
    padding: 0 2px;
  }
  .findings {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .findings li {
    display: grid;
    grid-template-columns: minmax(90px, 120px) 1fr;
    gap: 2px 12px;
    padding: 10px 12px;
    background: var(--surface-2);
    border: 1px solid var(--border-soft);
    border-radius: 10px;
  }
  .findings .faint {
    grid-column: 2;
  }
  .sev {
    font-size: 12px;
    font-weight: 600;
  }
  .scroll {
    overflow-x: auto;
    max-width: 100%;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  th {
    text-align: left;
    font-weight: 500;
    color: var(--text-muted);
    padding: 6px 10px 6px 0;
    border-bottom: 1px solid var(--border);
  }
  td {
    padding: 7px 10px 7px 0;
    border-bottom: 1px solid var(--border-soft);
    vertical-align: top;
  }
  .right {
    text-align: right;
  }
  td.right,
  table:not(.rows) th,
  table:not(.rows) td {
    white-space: nowrap; /* a wide table scrolls inside its own box */
  }
  /* Narrow: each setting and each radio reads as one block, with no sideways scroll. */
  @media (max-width: 640px) {
    .rows thead {
      display: none;
    }
    .rows tr {
      display: flex;
      flex-direction: column;
      gap: 4px;
      padding: 8px 0;
      border-bottom: 1px solid var(--border-soft);
    }
    .rows td {
      padding: 0;
      border: none;
    }
    .rows td.right {
      text-align: left;
      white-space: normal;
    }
    .findings li {
      grid-template-columns: 1fr;
    }
    .findings .faint {
      grid-column: 1;
    }
  }
  .param {
    font-size: 12px;
    color: var(--text-2);
  }
  .losses {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 10px;
  }
  .loss {
    padding: 10px 12px;
    background: var(--surface-2);
    border: 1px solid var(--border-soft);
    border-radius: 10px;
    font-size: 13px;
  }
  .loss.alert {
    border-color: color-mix(in srgb, var(--out) 60%, var(--border));
  }
  .loss ul {
    margin: 0 0 4px;
    padding-left: 18px;
  }
  .link {
    background: none;
    border: none;
    padding: 0;
    color: var(--link);
    cursor: pointer;
    font-size: 12px;
  }
  .swatch {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 2px;
    margin-right: 5px;
    vertical-align: -1px;
  }
</style>
