<script lang="ts">
  // An airframe's trends (spec: Performance trends and ideal ranges; User interface,
  // "Performance dashboard"; mock-up Trends): its metric cards, then one metric as a
  // point per flight against its zones and the airframe's own baseline, with every
  // change on the same axis, the platform's other airframes on request, and the same
  // subsystem's other metrics over the same flights.
  import { app, t } from "../lib/app.svelte";
  import { getRegistry } from "../lib/data";
  import type { MessageKey } from "../lib/i18n";
  import {
    formatNumber,
    formatTakeoff,
    formatValue,
    subsystem,
    subsystemName,
    zoneLook,
    type Registry,
  } from "../lib/report";
  import { engineCall } from "../lib/shell";
  import type { AirframeView } from "../lib/setup";
  import { STATUS } from "../lib/status";
  import {
    cardKey,
    changeTone,
    groupOption,
    placeEvents,
    sparkPath,
    worstCheck,
    type PayloadGroups,
    type Series,
    type TrendCard,
    type TrendEvent,
  } from "../lib/trends";
  import AirframePicker from "./AirframePicker.svelte";
  import PlatformMultiples from "./PlatformMultiples.svelte";
  import TrendChart from "./TrendChart.svelte";

  let registry = $state<Registry | null>(null);
  let airframe = $state<AirframeView | null>(null);
  let cards = $state<TrendCard[]>([]);
  let events = $state<TrendEvent[]>([]);
  let payloadGroups = $state<PayloadGroups | null>(null);
  let series = $state<Series | null>(null);
  let siblings = $state<Series[]>([]); // the platform's airframes, this one included
  let names = $state<Record<string, string>>({});
  let key = $state<string | null>(null); // the card shown: metric@segment
  let payloadGroup = $state<string | null>(null); // 'band:<n>' or 'label:<text>'; "*": every payload
  let compare = $state(false);
  let picked = $state<number | null>(null);
  let pickedEvent = $state<number | null>(null); // the flight index whose changes are open
  let layout = $state<{ left: number; width: number; count: number } | null>(null);
  let loading = $state(false);
  let error = $state("");

  // The airframe in scope: its cards, changes and payload groups.
  $effect(() => {
    const id = app.scope.airframe;
    if (app.shell.engine !== "ready" || id === null) return;
    loading = true;
    Promise.all([
      getRegistry(),
      engineCall<AirframeView>("airframe.get", { id }),
      engineCall<TrendCard[]>("trend.overview", { airframe_id: id }),
      engineCall<TrendEvent[]>("trend.events", { airframe_id: id }),
      engineCall<PayloadGroups>("trend.payloads", { airframe_id: id }),
    ])
      .then(([reg, af, c, ev, pg]) => {
        registry = reg;
        airframe = af;
        cards = c;
        events = ev;
        payloadGroups = pg;
        const wanted = app.trendCard;
        const first = c.find((x) => x.drift) ?? c[0];
        const card = c.find((x) => cardKey(x) === wanted) ?? first;
        key = card ? cardKey(card) : null;
        payloadGroup = card?.group ?? pg.latest;
        app.trendCard = null;
        error = "";
      })
      .catch((e: unknown) => (error = String(e)))
      .finally(() => (loading = false));
  });

  const card = $derived(cards.find((c) => cardKey(c) === key) ?? null);

  // The chosen metric's series, and the platform's other airframes when compared.
  $effect(() => {
    const c = card;
    const id = app.scope.airframe;
    if (!c || id === null) {
      series = null;
      return;
    }
    const params = { airframe_id: id, metric: c.metric, segment: c.segment };
    const which = payloadGroup === "*" ? { any_payload: true } : { group: payloadGroup };
    picked = null;
    pickedEvent = null;
    engineCall<Series>("trend.series", { ...params, ...which })
      .then((s) => (series = s))
      .catch((e: unknown) => (error = String(e)));
  });

  $effect(() => {
    const c = card;
    const platform = airframe?.platform.id;
    if (!compare || !c || !platform) {
      siblings = [];
      return;
    }
    Promise.all([
      engineCall<{ airframes: Series[] }>("trend.platform", { platform_id: platform, metric: c.metric, segment: c.segment }),
      engineCall<AirframeView[]>("airframe.list", { platform_id: platform }),
    ])
      .then(([v, list]) => {
        names = Object.fromEntries(list.map((a) => [a.id, a.name]));
        siblings = v.airframes;
      })
      .catch((e: unknown) => (error = String(e)));
  });

  const name = (metric: string) => registry?.metrics[metric]?.name[app.locale] ?? metric;
  const value = (v: number, unit: string) => formatValue(Math.round(v * 100) / 100, unit, app.locale);
  const segments = $derived(card ? cards.filter((c) => c.metric === card.metric) : []);
  const groups = $derived.by(() => {
    const out = new Map<string, TrendCard[]>();
    for (const c of cards) out.set(subsystem(c.metric), [...(out.get(subsystem(c.metric)) ?? []), c]);
    return [...out.entries()];
  });
  const related = $derived(card ? cards.filter((c) => subsystem(c.metric) === subsystem(card.metric) && c !== card) : []);
  const placed = $derived(series ? placeEvents(series.points, events) : []);
  const lane = $derived.by(() => {
    const at = new Map<number, TrendEvent[]>();
    for (const p of placed) at.set(p.x, [...(at.get(p.x) ?? []), p.event]);
    return [...at.entries()];
  });
  const point = $derived(series && picked !== null ? (series.points[picked] ?? null) : null);
  const direction = $derived(series?.direction ?? "band");

  function eventText(e: TrendEvent): string {
    switch (e.kind) {
      case "firmware":
        return t("trends.event.firmware", { from: e.from, to: e.to });
      case "build":
        return t("trends.event.build", { number: e.number, reason: e.reason ?? "–" });
      case "recommendation": {
        const grade = `grade.${e.grade.replace(" ", "_")}` as MessageKey;
        return t("trends.event.recommendation", {
          param: e.param ?? "–",
          proposed: e.proposed === null ? "–" : formatNumber(e.proposed, app.locale),
          grade: t(grade),
        });
      }
      case "ground station change":
        return t("trends.event.station");
      default:
        return t("trends.event.parameter", { origin: e.origin, params: e.params.join(", ") || "–" });
    }
  }

  const glyph = (e: TrendEvent) => (e.kind === "firmware" ? "◆" : e.kind === "recommendation" ? "●" : e.kind === "build" ? "■" : "▲");
  const xAt = (i: number) => (layout ? layout.left + ((i + 0.5) / Math.max(layout.count, 1)) * layout.width : 0);
  const when = (i: number) => formatTakeoff(series?.points[i]?.utc_us ?? null, app.locale) ?? t("trends.point", { n: i + 1 });

  function openFlight(flight: string) {
    app.scope.flight = flight;
    app.view = "flights";
  }
</script>

<main>
  <div class="heading">
    <h1>{airframe ? t("trends.title", { airframe: airframe.name }) : t("nav.trends")}</h1>
    <AirframePicker />
  </div>
  <p class="faint">{t("trends.about")}</p>
  {#if error}<p class="error">{error}</p>{/if}

  {#if app.scope.airframe !== null && !loading && cards.length === 0 && !error}
    <p class="faint">{t("trends.none")}</p>
  {:else if card && registry}
    <div class="layout">
      <nav aria-label={t("trends.metrics")} class="cards">
        {#each groups as [group, list] (group)}
          <h2>{subsystemName(group, app.locale)}</h2>
          {#each list as c (cardKey(c))}
            {@const look = STATUS[zoneLook(c.zone).status]}
            <button type="button" class="card" aria-pressed={cardKey(c) === key} onclick={() => ((key = cardKey(c)), (payloadGroup = c.group))}>
              <span class="row">
                <span class="cname">{name(c.metric)}</span>
                <span class="faint">{t(`segment.${c.segment}` as MessageKey)}</span>
              </span>
              <span class="row">
                <span class="mono" style:color="var({look.tone})">{look.glyph} {value(c.value, c.unit)}</span>
                <svg viewBox="0 0 120 28" width="120" height="28" aria-hidden="true">
                  <path d={sparkPath(c.sparkline)} fill="none" stroke={c.drift ? "var(--caution)" : "var(--text-faint)"} stroke-width="1.5" />
                </svg>
              </span>
              {#if c.drift}<span class="drift">▲ {t("fleet.drift")}</span>{/if}
            </button>
          {/each}
        {/each}
      </nav>

      <div class="main">
        <section class="panel">
          <div class="row">
            <h2>{name(card.metric)}</h2>
            {#if series}<span class="faint">{t("trends.count", { count: series.points.length })}</span>{/if}
          </div>
          <div class="controls">
            {#if segments.length > 1}
              <label>
                {t("trends.segment")}
                <select bind:value={key}>
                  {#each segments as s (cardKey(s))}
                    <option value={cardKey(s)}>{t(`segment.${s.segment}` as MessageKey)}</option>
                  {/each}
                </select>
              </label>
            {/if}
            <label>
              {t("trends.payload")}
              <select bind:value={payloadGroup}>
                {#each payloadGroups?.groups ?? [] as g (g.group)}
                  <option value={g.group}>{groupOption(g, app.locale)}</option>
                {/each}
                <option value="*">{t("trends.payload.any")}</option>
              </select>
            </label>
            {#if payloadGroups && worstCheck(payloadGroups.check) !== null}
              <span class="faint">{t("trends.payload.check", { error: formatNumber(worstCheck(payloadGroups.check) ?? 0, app.locale), count: payloadGroups.check.length })}</span>
            {/if}
            {#if airframe}
              <label class="check"><input type="checkbox" bind:checked={compare} /> {t("trends.compare")}</label>
            {/if}
            {#if series && series.points.length}
              <label>
                {t("trends.flight")}
                <select bind:value={picked}>
                  <option value={null}>–</option>
                  {#each series.points as _, i (i)}
                    <option value={i}>{when(i)}</option>
                  {/each}
                </select>
              </label>
            {/if}
          </div>

          {#if series}
            <div class="chart">
              <TrendChart
                {series}
                {picked}
                label={t("trends.chartLabel", { metric: name(card.metric) })}
                onpick={(i) => ((picked = i), (pickedEvent = null))}
                onlayout={(box) => (layout = box)}
              />
              <div class="lane" aria-label={t("trends.changes")} role="group">
                {#each lane as [x, list] (x)}
                  <button
                    type="button"
                    class="mark"
                    style:left="{xAt(x)}px"
                    aria-pressed={pickedEvent === x}
                    title={list.map(eventText).join("\n")}
                    onclick={() => ((pickedEvent = x), (picked = null))}
                  >{glyph(list[0]!)}</button>
                {/each}
              </div>
            </div>
            <div class="legend">
              <span style:color="var(--ok)">■ {t("zone.target")}</span>
              <span style:color="var(--caution)">■ {t("zone.caution")}</span>
              <span style:color="var(--out)">■ {t("zone.out")}</span>
              <span>┅ {t("trends.baseline")}</span>
              <span class="faint">{t("trends.ranges", { source: series.source.title })}</span>
            </div>
            <p class="faint">
              {series.baseline
                ? t("trends.baseline.value", {
                    low: formatNumber(Math.round(series.baseline.low * 10) / 10, app.locale),
                    high: value(series.baseline.high, series.unit),
                    count: series.baseline.flights,
                  })
                : t("trends.baseline.none")}
            </p>
            {#if series.drift}
              {@const since = series.points.findIndex((p) => p.flight === series?.drift?.since)}
              <p class="caution">▲ {t("trends.drift", { when: when(since) })}</p>
            {/if}
            {#if siblings.length && app.scope.airframe}
              <h3>{t("trends.platform")}</h3>
              <PlatformMultiples rows={siblings} current={app.scope.airframe} {names} />
            {/if}
          {/if}
        </section>

        {#if point && picked !== null && series}
          {@const look = STATUS[zoneLook(point.zone).status]}
          {@const delta = picked > 0 ? point.value - (series.points[picked - 1]?.value ?? point.value) : null}
          {@const tone = changeTone(delta, direction)}
          <section class="panel">
            <h2>{t("trends.picked", { n: picked + 1, when: when(picked) })}</h2>
            <p>
              <span class="mono" style:color="var({look.tone})">{look.glyph} {value(point.value, series.unit)}</span>
              · {t(zoneLook(point.zone).label)}
              {#if point.payload}· {point.payload}{/if}
              {#if point.takeoff_kg !== null}· {t("trends.point.weighed", { kg: formatNumber(point.takeoff_kg, app.locale) })}
              {:else if point.kg !== null}· {t("trends.point.kg", { kg: formatNumber(point.kg, app.locale) })}{/if}
            </p>
            {#if delta !== null}
              <p class:better={tone === "better"} class:worse={tone === "worse"}>
                {t("trends.sincePrevious", { delta: `${delta > 0 ? "+" : ""}${value(delta, series.unit)}` })}
              </p>
            {/if}
            <button type="button" class="link" onclick={() => openFlight(point.flight)}>{t("trends.open")}</button>
          </section>
        {/if}

        {#if pickedEvent !== null}
          <section class="panel">
            <h2>{t("trends.changes")} · {when(pickedEvent)}</h2>
            <ul>
              {#each lane.find(([x]) => x === pickedEvent)?.[1] ?? [] as e, i (i)}
                <li><span aria-hidden="true">{glyph(e)}</span> {eventText(e)}</li>
              {/each}
            </ul>
            <p class="faint">{t("trends.why")}</p>
          </section>
        {:else if series && lane.length === 0}
          <p class="faint">{t("trends.changes.none")}</p>
        {/if}

        {#if related.length}
          <section class="panel">
            <h2>{t("trends.related")}</h2>
            <div class="multiples">
              {#each related as c (cardKey(c))}
                {@const tone = changeTone(c.since_previous, registry.metrics[c.metric]?.direction ?? "band")}
                <button type="button" class="multiple" onclick={() => ((key = cardKey(c)), (payloadGroup = c.group))}>
                  <span class="cname">{name(c.metric)}</span>
                  <span class="mono">{value(c.value, c.unit)}</span>
                  <svg viewBox="0 0 120 28" width="100%" height="28" aria-hidden="true" preserveAspectRatio="none">
                    <path d={sparkPath(c.sparkline)} fill="none" stroke="var(--text-faint)" stroke-width="1.5" />
                  </svg>
                  {#if c.since_previous !== null}
                    <span class="faint" class:worse={tone === "worse"} class:better={tone === "better"}>
                      {t("trends.sincePrevious", { delta: `${c.since_previous > 0 ? "+" : ""}${value(c.since_previous, c.unit)}` })}
                    </span>
                  {/if}
                </button>
              {/each}
            </div>
          </section>
        {/if}
      </div>
    </div>
  {/if}
</main>

<style>
  main {
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }
  h1 {
    margin: 0;
    font-size: 24px;
    font-weight: 600;
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
  .caution {
    color: var(--caution);
    font-size: 13px;
  }
  .better {
    color: var(--ok);
  }
  .worse {
    color: var(--out);
  }
  .layout {
    display: flex;
    gap: 16px;
    align-items: flex-start;
    flex-wrap: wrap;
  }
  .cards {
    flex: 1 1 240px;
    max-width: 300px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .cards h2 {
    margin-top: 8px;
    font-size: 12px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .card,
  .multiple {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 8px 10px;
    background: var(--surface);
    border: 1px solid var(--border-soft);
    border-radius: 10px;
    color: var(--text);
    text-align: left;
    cursor: pointer;
  }
  .card[aria-pressed="true"] {
    border-color: var(--accent);
  }
  .cname {
    font-size: 13px;
  }
  .drift {
    font-size: 11px;
    color: var(--caution);
  }
  .main {
    flex: 3 1 560px;
    min-width: 0;
    display: flex;
    flex-direction: column;
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
  }
  .row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 14px;
    align-items: center;
    font-size: 13px;
    color: var(--text-muted);
  }
  .controls label {
    display: inline-flex;
    gap: 6px;
    align-items: center;
  }
  select {
    background: var(--surface-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 4px 6px;
  }
  .chart {
    position: relative;
  }
  .lane {
    position: relative;
    height: 24px;
    border-top: 1px solid var(--border-soft);
  }
  .mark {
    position: absolute;
    top: 3px;
    transform: translateX(-50%);
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 5px;
    color: var(--accent);
    font-size: 11px;
    padding: 0 4px;
    cursor: pointer;
  }
  .mark[aria-pressed="true"] {
    border-color: var(--accent);
  }
  h3 {
    margin: 6px 0 0;
    font-size: 13px;
    color: var(--text-muted);
  }
  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 14px;
    font-size: 12px;
    color: var(--text-muted);
  }
  ul {
    margin: 0;
    padding-left: 18px;
    font-size: 13px;
  }
  .multiples {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 8px;
  }
  .link {
    align-self: flex-start;
    background: none;
    border: none;
    padding: 0;
    color: var(--link);
    cursor: pointer;
    font-size: 13px;
  }
</style>
