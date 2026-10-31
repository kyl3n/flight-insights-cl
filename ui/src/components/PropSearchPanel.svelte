<script lang="ts">
  // For the Priority (spec: Power and efficiency analysis, "Propeller and motor choice",
  // "Advice"; mock-up Propellers): the propellers of the flown family and blades the
  // search tried (`airframe.prop_search`), each predicted against the one flown: one line
  // per diameter against pitch over diameter, then ranked, with the checks each fails.
  // The suggestion card shows only when the engine advises one; until the predictions
  // are validated it never does, and the status strip says so.
  import { app, t } from "../lib/app.svelte";
  import { getRegistry } from "../lib/data";
  import {
    CHECKS,
    failedKey,
    fixed,
    gainText,
    optionState,
    priorityKey,
    propName,
    REASONS,
    reasonKey,
    sameProp,
    searchChart,
    searchSpan,
    shownOptions,
    type Prop,
    type Propellers,
    type PropSearch,
  } from "../lib/props";
  import { findingAction, findingTitle, formatNumber, unitText, type Registry } from "../lib/report";
  import TheoryPill from "./TheoryPill.svelte";

  let {
    view,
    found,
    searching,
    error,
    tag,
    familyName,
  }: {
    view: Propellers;
    found: PropSearch | null;
    searching: boolean;
    error: string;
    tag: (id: string) => string;
    familyName: (id: string) => string;
  } = $props();

  const LIMIT = 8; // options shown before "show all"
  const F = { width: 1000, height: 280, left: 64, right: 44, top: 18, bottom: 46 };

  let all = $state(false);
  const s = $derived(found?.search ?? null);
  const flown = $derived<Prop | null>(found?.flown_prop ?? view.prop);
  const kg = $derived(found?.inputs?.mass_kg ?? null);
  const objective = $derived(s?.objective ?? "hover_power");
  const priority = $derived(t(priorityKey(s ? s.priority : view.priority)));
  const chart = $derived(s && flown ? searchChart(s.options, flown, s.best, F) : null);
  const rows = $derived(s && flown ? shownOptions(s.options, flown, all ? null : LIMIT) : []);
  const span = $derived(s ? searchSpan(s.options) : null);
  const times = $derived(formatNumber(view.advice.gain_over_error, app.locale));

  const n = (v: number | null | undefined, digits: number) => fixed(v, digits, app.locale);
  const name = (p: Prop) => propName(p, app.locale);
  const yTick = (v: number) => (objective === "hover_power" ? `${n(v, 0)} ${unitText("W", app.locale)}` : n(v, 1));
  // the diameters other than the one flown, from faint (smallest) to light (largest)
  const tone = (rank: number) => `color-mix(in srgb, var(--link) ${Math.round(25 + 60 * rank)}%, var(--text-faint))`;
  const diamond = (p: { x: number; y: number }) => `M${p.x} ${p.y - 7} L${p.x + 7} ${p.y} L${p.x} ${p.y + 7} L${p.x - 7} ${p.y} Z`;
  const near = (x: number) => x > F.width - F.right - 280; // a label near the right edge reads leftwards
  // the flown's label above its mark and the best's below, unless that runs off the plot
  const above = (y: number) => (y - 9 > F.top + 8 ? y - 9 : y + 18);
  const below = (y: number) => (y + 18 < F.height - F.bottom - 4 ? y + 18 : y - 10);

  const aria = $derived.by(() => {
    if (!s || !span || !flown) return "";
    const best = s.best
      ? t("props.chart.aria.best", { best: name(s.best), gain: n(s.best.gain * 100, 1), flown: name(flown) })
      : t("props.chart.aria.none");
    const lo = span.diameters[0] ?? 0;
    const hi = span.diameters[span.diameters.length - 1] ?? 0;
    return t(`props.chart.aria.${objective}`, { lo: formatNumber(lo, app.locale), hi: formatNumber(hi, app.locale), kg: n(kg, 1), best });
  });

  // ---- the suggestion: worded as the plan's finding (performance.prop_choice) ----
  let registry = $state<Registry | null>(null);
  $effect(() => {
    if (!s?.advised) return;
    getRegistry().then(
      (r) => (registry = r),
      () => (registry = null),
    );
  });
  const advised = $derived(s?.advised && s.best && flown && kg !== null ? { best: s.best, flown, kg } : null);
  const finding = $derived.by(() => {
    if (!advised || !registry) return null;
    const params = {
      flown: name(advised.flown),
      best: name(advised.best),
      gain_pct: Math.round(advised.best.gain * 1000) / 10,
      mass_kg: Math.round(advised.kg * 10) / 10,
      objective,
    };
    const f = { code: "performance.prop_choice", params };
    return { title: findingTitle(registry, f, app.locale), action: findingAction(registry, f, app.locale) };
  });
  const rests = $derived.by(() => {
    if (!advised) return "";
    const anchors = view.measured.flights.map((f) => tag(f.flight_id));
    const family = view.families.find((f) => f.id === advised.best.family);
    const error = view.advice.validated_error;
    return t("props.suggest.rests", {
      flights: anchors.length ? t("props.suggest.flights", { list: anchors.join(", ") }) : t("props.suggest.noFlights"),
      tables: t("props.suggest.tables", { family: familyName(advised.best.family), count: family?.tables ?? 0 }),
      pairs: view.advice.pairs,
      motors: view.advice.motors,
      error: n(error === null ? null : error * 100, 1),
      threshold: n(error === null ? null : error * view.advice.gain_over_error * 100, 1),
    });
  });
</script>

<section class="panel" aria-labelledby="props-search-title">
  <div class="head">
    <h2 id="props-search-title">{t("props.search.title", { priority })}</h2>
    <span class="faint">{t("props.search.about")}</span>
  </div>

  {#if error}
    <p class="error">{error}</p>
  {:else if searching && !found}
    <p class="faint">{t("props.search.working")}</p>
  {:else if found && !s}
    <ul>
      {#each Object.entries(found.missing) as [k, why] (k)}<li>{k === "prop" ? t("props.missing.prop") : why}</li>{/each}
    </ul>
  {:else if s && flown}
    {#if chart}
      <figure>
        <figcaption>
          <span class="cap">{t(`props.chart.${objective}`, { kg: n(kg, 1) })}</span>
          <span><span aria-hidden="true" class="key-flown">●</span> {t("props.key.flown")}</span>
          {#if s.best}<span><span aria-hidden="true" class="key-best">◆</span> {t("props.key.best")}</span>{/if}
          {#if s.options.some((o) => !o.ok)}<span><span aria-hidden="true">┅</span> {t("props.key.fails")}</span>{/if}
          <TheoryPill />
        </figcaption>
        <svg viewBox="0 0 {F.width} {F.height}" role="img" aria-label={aria}>
          {#each chart.y.ticks as v (v)}
            <line class="grid" x1={F.left} x2={F.width - F.right} y1={chart.py(v)} y2={chart.py(v)} />
            <text class="tick" x={F.left - 8} y={chart.py(v) + 4} text-anchor="end">{yTick(v)}</text>
          {/each}
          {#each chart.x.ticks as v (v)}
            <text class="tick" x={chart.px(v)} y={F.height - F.bottom + 18} text-anchor="middle">{n(v, 2)}</text>
          {/each}
          <text class="axis" x={(F.left + F.width - F.right) / 2} y={F.height - 6} text-anchor="middle">{t("props.chart.ratio")}</text>
          {#each chart.lines as l (l.diameter)}
            <g style:--line={l.flown ? "var(--accent)" : tone(l.rank)}>
              <path class="line" class:flown={l.flown} d={l.solid} />
              <path class="line dashed" class:flown={l.flown} d={l.dashed} />
              <text class="end" x={l.end.x + 6} y={l.end.y + 4}>{formatNumber(l.diameter, app.locale)}″</text>
            </g>
          {/each}
          {#if chart.flown}
            {@const p = chart.flown}
            <circle class="dot-flown" cx={p.x} cy={p.y} r="5" />
            <text class="label" x={near(p.x) ? p.x - 8 : p.x + 8} y={above(p.y)} text-anchor={near(p.x) ? "end" : "start"}>{t("props.chart.flown", { prop: name(flown) })}</text>
          {/if}
          {#if chart.best && s.best}
            {@const p = chart.best}
            <path class="dot-best" d={diamond(p)} />
            <text class="label" x={near(p.x) ? p.x - 10 : p.x + 10} y={below(p.y)} text-anchor={near(p.x) ? "end" : "start"}>{t("props.chart.best", { prop: name(s.best) })}</text>
          {/if}
        </svg>
      </figure>
    {/if}

    {#if s.options.length}
      <div class="scroll">
        <table aria-label={t("props.options.aria", { priority })}>
          <thead>
            <tr>
              <th scope="col">{t("props.col.prop")}</th>
              <th scope="col" class="num">{t(`props.col.objective.${objective}`)}&nbsp;<TheoryPill /></th>
              <th scope="col">{t("props.col.checks")}</th>
              <th scope="col"><span class="sr-only">{t("props.col.notes")}</span></th>
            </tr>
          </thead>
          <tbody>
            {#each rows as r (`${r.option.diameter_in}x${r.option.pitch_in}`)}
              <tr class:best={s.best !== null && sameProp(r.option, s.best)}>
                <td class="mono">{r.flown ? t("props.option.flown", { prop: name(r.option) }) : name(r.option)}</td>
                <td class="num mono">{gainText(r.option, objective, app.locale, r.flown)}</td>
                <td class="mono">
                  {#if r.option.ok}{t("props.checks.pass")}{:else}
                    <span class="mark" aria-hidden="true">▲</span>
                    {r.option.failed.map((f) => (CHECKS.includes(f) ? t(failedKey(f)) : f)).join(", ")}
                  {/if}
                </td>
                <td class="mono note">{optionState(r.option, r.flown).map((k) => t(k)).join(" · ")}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      {#if s.options.length > LIMIT}
        <button type="button" class="link" aria-expanded={all} onclick={() => (all = !all)}>
          {all ? t("props.options.fewer") : t("props.options.all", { count: s.options.length })}
        </button>
      {/if}
      <p class="faint">
        {#if s.objective}{t(`props.search.why.${s.objective}`, { priority, kg: n(kg, 1) })}{/if}
        {#if span}{t("props.search.span", {
            lo: n(span.lo, 2),
            hi: n(span.hi, 2),
            diameters: span.diameters.map((d) => formatNumber(d, app.locale)).join(", "),
          })}{/if}
        {#if s.best}{t("props.search.best", { gain: n(s.best.gain * 100, 1), spread: n(s.best.spread === null ? null : s.best.spread * 100, 1) })}{/if}
        {#if !view.advice.enabled}{t("props.search.rule", { times })}{/if}
      </p>
    {/if}
    {#if s.reason && REASONS.includes(s.reason)}<p class="text">{t(reasonKey(s.reason), { times })}</p>{/if}
  {/if}
</section>

{#if advised && finding}
  <section class="panel" aria-labelledby="props-suggest-title">
    <div class="head">
      <h2 id="props-suggest-title">{t("props.suggest.title")}</h2>
      <span class="faint">{t("props.suggest.about")}</span>
    </div>
    <div class="card">
      <p class="card-head"><span class="card-title">{finding.title}</span> <TheoryPill /></p>
      <p class="text">{finding.action}</p>
      <ol>
        <li>{t("props.suggest.step1")}</li>
        <li>{t("props.suggest.step2")}</li>
        <li>{t("props.suggest.step3", { kg: n(advised.kg, 1) })}</li>
        <li>{t("props.suggest.step4")}</li>
      </ol>
      <p class="faint rests">{rests}</p>
    </div>
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
  h2 {
    margin: 0;
    font-size: 15px;
  }
  p {
    margin: 0;
  }
  ul,
  ol {
    margin: 0;
    padding-left: 20px;
    font-size: 13px;
  }
  .text {
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
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 14px;
    font-size: 12px;
    color: var(--text-muted);
  }
  .cap {
    color: var(--text-2);
    font-weight: 500;
  }
  .key-flown {
    color: var(--accent);
  }
  .key-best {
    color: var(--ok);
  }
  svg {
    display: block;
    width: 100%;
    max-width: 1100px;
    height: auto;
  }
  .grid {
    stroke: var(--border-soft);
  }
  .tick,
  .end {
    fill: var(--text-faint);
    font-family: var(--mono);
    font-size: 10px;
  }
  .axis {
    fill: var(--text-faint);
    font-size: 10px;
  }
  .line {
    fill: none;
    stroke: var(--line);
    stroke-width: 1.6;
  }
  .line.flown {
    stroke-width: 2.2;
  }
  .line.dashed {
    stroke-dasharray: 5 4;
  }
  .end {
    fill: var(--line);
  }
  .dot-flown {
    fill: var(--accent);
    stroke: var(--surface);
    stroke-width: 2;
  }
  .dot-best {
    fill: var(--ok);
    stroke: var(--surface);
    stroke-width: 2;
  }
  .label {
    fill: var(--text);
    font-size: 11px;
  }
  .scroll {
    position: relative;
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
    color: var(--text-faint);
    padding: 6px 10px;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  td {
    padding: 7px 10px;
    border-bottom: 1px solid var(--border-soft);
  }
  tr.best td:first-child {
    color: var(--ok);
  }
  .num {
    text-align: right;
    white-space: nowrap;
  }
  .note {
    color: var(--text-muted);
  }
  .mark {
    display: inline-block;
    min-width: 1.4em;
    padding: 0 4px;
    margin-right: 4px;
    border-radius: 999px;
    border: 1px solid var(--caution);
    color: var(--caution);
    font-size: 11px;
    text-align: center;
    box-sizing: border-box;
  }
  .link {
    align-self: flex-start;
    padding: 6px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: transparent;
    color: var(--link);
    font-size: 13px;
    cursor: pointer;
  }
  .card {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 14px 16px;
    border-radius: 10px;
    border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--border));
    background: color-mix(in srgb, var(--accent) 6%, var(--surface-2));
  }
  .card-title {
    font-weight: 600;
  }
  .rests {
    border-top: 1px solid var(--border-soft);
    padding-top: 8px;
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }
</style>
