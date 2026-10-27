<script lang="ts">
  // The inspector of the parameter history (mock-up History, right): one measured effect
  // and the changes the engine ranked behind it, each with the map's reason, its factors
  // and the fleet's votes; then what confounds it, and the revert test or credit that
  // follows it. A shift between flights shows its metric around the shift; an in-flight
  // A/B split shows its two sides.
  import { app, t } from "../lib/app.svelte";
  import {
    candidateName,
    candidateWhy,
    changeText,
    classText,
    effectText,
    factorsText,
    fleetLink,
    hardwareWith,
    intervalText,
    miniChart,
    paramValue,
    scoreShare,
    shiftHow,
    stepStatus,
    verdictLook,
    weatherRows,
    type Credit,
    type FleetLink,
    type Ranked,
    type RevertTest,
    type Shift,
    type Split,
    type SplitResult,
  } from "../lib/history";
  import type { MessageKey } from "../lib/i18n";
  import { formatNumber, formatValue, unitText } from "../lib/report";
  import { STATUS } from "../lib/status";
  import type { Series } from "../lib/trends";

  let {
    shift = null,
    ab = null,
    flight,
    test = null,
    credits = [],
    fleet,
    series = null,
    flightName,
    groupName,
    onopentrend,
    onopenflight,
  }: {
    shift?: Shift | null;
    ab?: { flight: string; split: Split; result: SplitResult } | null;
    flight: string; // where the changes first applied, or the flight split
    test?: RevertTest | null;
    credits?: Credit[];
    fleet: FleetLink[];
    series?: Series | null;
    flightName: (id: string) => string;
    groupName: (group: string) => string;
    onopentrend?: () => void;
    onopenflight: (id: string) => void;
  } = $props();

  type Candidate = Ranked & {
    kind?: string;
    name: string;
    before: number | string | null;
    after: number | string | null;
    origin: string | null;
    fleet?: { agree: number; against: number } | null;
  };

  const m = $derived(shift ?? ab?.result ?? null);
  const metricName = $derived(m ? m.name[app.locale] : "");
  const candidates = $derived<Candidate[]>(shift?.candidates ?? ab?.result.candidates ?? []);
  const scores = $derived(candidates.map((c) => c.score));
  const verdict = $derived(
    shift ? verdictLook(shift.verdict, shiftHow(shift), app.locale) : ab ? verdictLook(ab.result.verdict, ab.result.method, app.locale) : null,
  );
  const segment = (s: string) => t(`segment.${s}` as MessageKey);

  // The chart: the metric over this payload band's flights, the shift and its revert tests marked.
  const W = 340;
  const H = 100;
  const chart = $derived.by(() => {
    if (!shift || !series || series.points.length < 2) return null;
    const c = miniChart(series.points, series.baseline, W, H);
    const after = c.x(shift.flight);
    const last = shift.before.at(-1);
    const before = last ? c.x(last) : null;
    const at = after === null ? null : before === null ? after - 6 : (before + after) / 2;
    const reverts = (test?.steps ?? []).flatMap((s) => {
      const x = s.tested_on ? c.x(s.tested_on) : null;
      return x === null || !s.tested_on ? [] : [{ x: x - 6, flight: s.tested_on }];
    });
    return { ...c, at, reverts, after: new Set(shift.after) };
  });
  const tone = $derived(verdict ? STATUS[verdict.status].tone : "--text");
</script>

{#if m && verdict}
  <div class="top">
    <span class="eyebrow">{t(shift ? "history.inspector.eyebrow" : "history.inspector.eyebrowAb", { flight: flightName(flight) })}</span>
    <h2>{t("history.inspector.title", { metric: metricName, effect: effectText(m.effect, m.unit, app.locale) })}</h2>
    {#if shift}
      <p class="muted">{segment(shift.segment)} · {groupName(shift.group)}. {t("history.inspector.score")}</p>
    {:else if ab}
      <p class="muted">
        {segment(ab.result.segment)} ·
        {t("history.inspector.sides", { before: formatValue(ab.result.before, ab.result.unit, app.locale), after: formatValue(ab.result.after, ab.result.unit, app.locale) })}.
        {#if ab.split.before_s && ab.split.after_s}
          {t("history.ab.sides", {
            b0: formatNumber(ab.split.before_s[0], app.locale), b1: formatNumber(ab.split.before_s[1], app.locale),
            a0: formatNumber(ab.split.after_s[0], app.locale), a1: formatNumber(ab.split.after_s[1], app.locale),
          })}
        {/if}
      </p>
    {/if}
    <p class="verdict">
      <span style:color="var({tone})"><span aria-hidden="true">{STATUS[verdict.status].glyph}</span> {verdict.text}</span>
      <span class="mono faint">{intervalText(m.low, m.high, app.locale)}</span>
    </p>
  </div>

  {#if chart && shift && series}
    <figure>
      <figcaption>{metricName}, {unitText(series.unit, app.locale)} · ┅ {t("trends.baseline")}</figcaption>
      <svg viewBox="0 0 {W} {H + 18}" width="100%" role="img" aria-label={t("history.inspector.chart", { metric: metricName })}>
        {#if chart.band}
          <rect x="0" y={chart.band.y} width={W} height={chart.band.h} fill="none" stroke="var(--text-muted)" stroke-opacity="0.6" stroke-dasharray="5 4" />
        {/if}
        <path d={chart.path} fill="none" stroke="var(--border)" stroke-width="1.2" />
        {#each chart.dots as d (d.flight)}
          <circle cx={d.x} cy={d.y} r="3.2" fill={chart.after.has(d.flight) ? `var(${tone})` : "var(--text)"} />
        {/each}
        {#if chart.at !== null}
          <line x1={chart.at} x2={chart.at} y1="2" y2={H} stroke="var(--accent)" stroke-width="1.4" stroke-dasharray="4 3" />
          <text x={chart.at + 4} y="12" fill="var(--link)" font-size="11">{t("history.chart.shift", { flight: flightName(shift.flight) })}</text>
        {/if}
        {#each chart.reverts as r (r.flight)}
          <line x1={r.x} x2={r.x} y1="2" y2={H} stroke="var(--ok)" stroke-width="1.4" stroke-dasharray="4 3" />
          <text x={r.x + 4} y="26" fill="var(--ok)" font-size="11">{t("history.chart.revert", { flight: flightName(r.flight) })}</text>
        {/each}
        <text x="0" y={H + 15} fill="var(--text-faint)" font-size="10" font-family="var(--mono)">{flightName(series.points[0]?.flight ?? "")}</text>
        <text x={W} y={H + 15} fill="var(--text-faint)" font-size="10" text-anchor="end" font-family="var(--mono)">{flightName(series.points.at(-1)?.flight ?? "")}</text>
      </svg>
    </figure>
  {/if}

  <section>
    <h3>{t("history.inspector.ranked", { confidence: t(`history.confidence.${m.confidence}`) })}</h3>
    {#if candidates.length === 0}
      <p class="faint">{t("history.inspector.noCandidates")}</p>
    {:else}
      <ol class="cands">
        {#each candidates as c, i (i)}
          {@const confirmed = shift ? fleetLink(fleet, c, shift.metric, shift.effect) : null}
          <li class:top={i === 0}>
            <div class="row">
              <span class="rank mono">{i + 1}</span>
              <span class="mono name">{candidateName(c, app.locale)}</span>
              {#if (c.kind ?? "parameter") === "parameter"}
                <span class="mono faint">{paramValue(c.before, app.locale)} → {paramValue(c.after, app.locale)}</span>
              {/if}
            </div>
            <p>{candidateWhy(c, metricName, app.locale)}</p>
            <div class="score">
              <span class="mono">{formatNumber(c.score, app.locale)}</span>
              <span class="bar" aria-hidden="true"><span style:width="{scoreShare(c.score, scores) * 100}%"></span></span>
            </div>
            <span class="faint">
              {[factorsText(c.factors, app.locale), c.origin === "tuning knob" ? t("history.origin.tuning_knob") : "", c.fleet ? t("history.fleet.votes", c.fleet) : ""]
                .filter(Boolean)
                .join(" · ")}
            </span>
            {#if confirmed}
              <span class="ok small"><span aria-hidden="true">●</span> {t(confirmed.in_map ? "history.fleet.confirmed" : "history.fleet.confirmedLearned", { count: confirmed.airframes })}</span>
            {/if}
          </li>
        {/each}
      </ol>
    {/if}
  </section>

  {#if shift}
    {@const hardware = hardwareWith(shift)}
    {@const weather = weatherRows(shift.weather, app.locale)}
    <section class="list">
      <h3>{t("history.confounders")}</h3>
      {#each hardware as h, i (i)}
        <span><span class="caution" aria-hidden="true">▲</span> {t("history.confounder.hardware", { change: candidateName(h, app.locale) })}</span>
      {:else}
        <span><span class="ok" aria-hidden="true">●</span> {t("history.confounder.noHardware")}</span>
      {/each}
      {#each weather as w (w.name)}
        <span>
          <span class={w.differs ? "caution" : "info"} aria-hidden="true">{w.differs ? "▲" : "◆"}</span>
          {t(w.differs ? "history.confounder.differs" : "history.confounder.same", { name: w.name, before: w.before, after: w.after })}
        </span>
      {/each}
      {#if shift.context.length}
        <span class="faint">
          {t("history.context", {
            list: shift.context.map((c) => `${changeText(c, app.locale)} (${classText(c.class, app.locale)})`).join(", "),
          })}
        </span>
      {/if}
    </section>

    {#if test}
      {@const settled = test.steps.find((s) => s.state === "explained")}
      {@const next = test.steps.find((s) => s.order === test.next_step)}
      <section class="list" class:settled={!!settled}>
        <h3>
          {#if settled?.tested_on}
            <span class="ok"><span aria-hidden="true">●</span> {t("history.revert.settled", { flight: flightName(settled.tested_on) })}</span>
          {:else}
            {t("history.revert.title")}
          {/if}
        </h3>
        <ol class="steps">
          {#each test.steps as s (s.order)}
            {@const look = STATUS[stepStatus(s.state)]}
            <li>
              <span class="mono">{s.name}</span>
              <span class="mono faint">{paramValue(s.after, app.locale)} → {paramValue(s.before, app.locale)}</span>
              <span style:color="var({look.tone})"><span aria-hidden="true">{look.glyph}</span> {t(`history.state.${s.state.replaceAll(" ", "_")}` as MessageKey)}</span>
              {#if s.tested_on && s.value !== null}
                <span class="faint">
                  {t("history.revert.step", {
                    param: s.name,
                    value: formatValue(s.value, test.unit, app.locale),
                    flight: flightName(s.tested_on),
                    before: formatValue(test.before, test.unit, app.locale),
                    after: formatValue(test.after, test.unit, app.locale),
                  })}
                </span>
              {/if}
            </li>
          {/each}
        </ol>
        {#if next}
          <p class="caution small">
            <span aria-hidden="true">▲</span>
            {t("history.revert.next", { param: next.name, value: paramValue(next.before, app.locale), segment: segment(shift.segment), group: groupName(shift.group) })}
          </p>
        {/if}
        {#if test.by_hand.length}
          <p class="faint">{t("history.revert.byHand", { list: test.by_hand.map((c) => candidateName(c, app.locale)).join(", ") })}</p>
        {/if}
      </section>
    {:else if shift.verdict === "worse"}
      <p class="faint">{t("history.revert.none")}</p>
    {/if}

    {#each credits as c (c.param)}
      <p class="small" class:ok={c.kept} class:caution={!c.kept}>
        <span aria-hidden="true">{c.kept ? "●" : "▲"}</span>
        {c.kept
          ? t("history.credit.kept", { param: c.param, metric: c.name[app.locale] })
          : t("history.credit.undone", { param: c.param, metric: c.name[app.locale], flight: c.undone_on ? flightName(c.undone_on) : "–" })}
      </p>
    {/each}
  {/if}

  <div class="actions">
    {#if shift && onopentrend}
      <button type="button" class="primary" onclick={onopentrend}>{t("history.openTrend")}</button>
    {/if}
    <button type="button" class="secondary" onclick={() => onopenflight(shift ? shift.flight : flight)}>{t("trends.open")}</button>
  </div>
{/if}

<style>
  .top {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .eyebrow {
    font-size: 12px;
    font-weight: 600;
    color: var(--link);
  }
  h2 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    line-height: 1.3;
    overflow-wrap: anywhere;
  }
  h3 {
    margin: 0 0 6px;
    font-size: 13px;
    font-weight: 600;
  }
  p {
    margin: 0;
  }
  .muted {
    font-size: 13px;
    color: var(--text-muted);
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
  }
  .small {
    font-size: 12px;
  }
  .ok {
    color: var(--ok);
  }
  .caution {
    color: var(--caution);
  }
  .info {
    color: var(--info);
  }
  .verdict {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 10px;
    font-size: 13px;
    font-weight: 600;
  }
  figure {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  figcaption {
    font-size: 12px;
    color: var(--text-muted);
  }
  svg {
    display: block;
    max-width: 100%;
  }
  .cands {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .cands li {
    display: flex;
    flex-direction: column;
    gap: 5px;
    padding: 10px 12px;
    border: 1px solid var(--border-soft);
    border-radius: 10px;
    background: var(--surface-2);
  }
  .cands li.top {
    border-color: var(--accent);
  }
  .cands p {
    font-size: 13px;
    color: var(--text-2);
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 10px;
    align-items: baseline;
  }
  .rank {
    color: var(--text-faint);
  }
  .name {
    font-weight: 500;
    overflow-wrap: anywhere;
  }
  .row .faint {
    margin-left: auto;
  }
  .score {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 12px;
  }
  .bar {
    flex: 1;
    height: 4px;
    border-radius: 2px;
    background: var(--raised);
    overflow: hidden;
  }
  .bar span {
    display: block;
    height: 100%;
    background: var(--accent);
  }
  .list {
    display: flex;
    flex-direction: column;
    gap: 5px;
    font-size: 13px;
  }
  .list.settled {
    padding: 10px 12px;
    border: 1px solid color-mix(in srgb, var(--ok) 45%, transparent);
    border-radius: 10px;
    background: color-mix(in srgb, var(--ok) 7%, transparent);
  }
  .steps {
    margin: 0;
    padding-left: 18px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .steps li {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 8px;
    align-items: baseline;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .primary {
    padding: 8px 14px;
    border: 0;
    border-radius: 8px;
    background: var(--accent);
    color: var(--accent-text);
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
  }
  .secondary {
    padding: 8px 14px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: transparent;
    color: var(--text);
    font-size: 13px;
    cursor: pointer;
  }
</style>
