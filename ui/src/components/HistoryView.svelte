<script lang="ts">
  // An airframe's parameter history (spec: Parameter history and change attribution;
  // User interface, "Parameter history"; mock-up History), the Trends view's second tab:
  // every change set with its origin and measured effect, in-flight A/B splits, revert
  // tests and credits, newest first; picking a measured effect opens the changes ranked
  // behind it in the inspector. Every verdict and number is the engine's.
  import { tick } from "svelte";
  import { app, t } from "../lib/app.svelte";
  import {
    asideText,
    boundary,
    buildRows,
    changeItems,
    effectText,
    FILTERS,
    flightDate,
    flightNumbers,
    intervalText,
    matches,
    MAX_AB,
    MAX_REVERTS,
    origins,
    paramValue,
    rowTitle,
    shiftHow,
    shiftKey,
    stepStatus,
    verdictLook,
    type Credit,
    type FleetLink,
    type Filter,
    type RecRow,
    type RevertTest,
    type Row,
    type Shift,
    type Split,
    type SplitResult,
  } from "../lib/history";
  import type { MessageKey } from "../lib/i18n";
  import { formatNumber, formatValue, type FlightRow } from "../lib/report";
  import { engineCall } from "../lib/shell";
  import { STATUS, type Status } from "../lib/status";
  import { groupOption, type PayloadGroups, type Series, type TrendEvent } from "../lib/trends";
  import HistoryInspector from "./HistoryInspector.svelte";

  let {
    airframeId,
    platformId,
    onopentrend,
  }: {
    airframeId: string;
    platformId: string | null;
    onopentrend: (metric: string, segment: string, group: string) => void;
  } = $props();

  type Selection = { kind: "shift"; key: string } | { kind: "ab"; row: string; index: number };

  let flights = $state<FlightRow[]>([]);
  let events = $state<TrendEvent[]>([]);
  let shifts = $state<Shift[]>([]);
  let credits = $state<Credit[]>([]);
  let reverts = $state<RevertTest[]>([]);
  let splits = $state<Record<string, Split[]>>({});
  let recs = $state<RecRow[]>([]);
  let payloads = $state<PayloadGroups | null>(null);
  let fleet = $state<FleetLink[]>([]);
  let series = $state<Record<string, Series>>({});
  let loading = $state(true);
  let error = $state("");
  let checked = $state(0); // flights asked for in-flight changes so far
  let checking = $state(0);
  let filter = $state<Filter>("all");
  let selected = $state<Selection | null>(null);
  let expanded = $state<Record<string, boolean>>({});
  let inspector: HTMLElement | undefined = $state();

  // The airframe's history: what changed, what shifted, the revert tests of the worse
  // shifts, then each of the newest flights asked for changes made while it flew.
  $effect(() => {
    const id = airframeId;
    if (app.shell.engine !== "ready") return;
    let stale = false;
    loading = true;
    error = "";
    selected = null;
    splits = {};
    reverts = [];
    checked = checking = 0;
    Promise.all([
      engineCall<FlightRow[]>("flight.list", { airframe_id: id }),
      engineCall<TrendEvent[]>("trend.events", { airframe_id: id }),
      engineCall<Shift[]>("trend.attribution", { airframe_id: id }),
      engineCall<Credit[]>("trend.credits", { airframe_id: id }),
      engineCall<PayloadGroups>("trend.payloads", { airframe_id: id }),
      engineCall<RecRow[]>("recommend.history", { airframe_id: id }),
    ])
      .then(async ([fl, ev, sh, cr, pg, rr]) => {
        if (stale) return;
        flights = fl;
        events = ev;
        shifts = sh;
        credits = cr;
        payloads = pg;
        recs = rr;
        // A shift that made a metric worse is settled by putting its changes back one at a time.
        const worse = sh.filter((s) => s.verdict === "worse").slice(-MAX_REVERTS);
        const tests = await Promise.all(
          worse.map((s) =>
            engineCall<RevertTest>("trend.revert_test", {
              airframe_id: id, metric: s.metric, segment: s.segment, group: s.group, flight: s.flight,
            }).catch(() => null), // a shift with no parameter a file can put back has none
          ),
        );
        if (stale) return;
        reverts = tests.filter((x): x is RevertTest => x !== null);
        loading = false;
        pickFirst();
        const newest = fl.slice(0, MAX_AB);
        checking = newest.length;
        for (const f of newest) {
          // A flight the engine cannot split (no log behind it) has no in-flight changes to show.
          const found = await engineCall<Split[]>("flight.ab", { flight_id: f.id }).catch(() => [] as Split[]);
          if (stale) return;
          if (found.length) splits = { ...splits, [f.id]: found };
          checked += 1;
        }
      })
      .catch((e: unknown) => {
        if (stale) return;
        error = String(e);
        loading = false;
      });
    return () => {
      stale = true;
    };
  });

  // The platform's confirmed links, for each ranked change's fleet evidence.
  $effect(() => {
    const id = platformId;
    fleet = [];
    if (app.shell.engine !== "ready" || !id) return;
    engineCall<FleetLink[]>("trend.fleet", { platform_id: id })
      .then((links) => (fleet = links))
      .catch(() => (fleet = []));
  });

  const rows = $derived(buildRows({ flights, events, shifts, credits, reverts, splits, recs }));
  const shown = $derived(rows.filter((r) => matches(r, filter)));
  const numbers = $derived(flightNumbers(flights));
  const byId = $derived(new Map(flights.map((f) => [f.id, f])));
  const testOf = $derived(new Map(reverts.map((x) => [shiftKey(x), x])));

  function flightName(id: string): string {
    const n = numbers.get(id);
    if (n !== undefined) return t("history.flight", { n });
    return flightDate(byId.get(id)?.utc_us ?? null, app.locale) ?? id.slice(0, 8);
  }
  const groupName = (group: string) => {
    const g = payloads?.groups.find((x) => x.group === group);
    return g ? groupOption(g, app.locale) : group;
  };
  const segment = (s: string) => t(`segment.${s}` as MessageKey);
  const look = (status: Status) => STATUS[status];

  const shiftOf = $derived.by(() => {
    const sel = selected;
    return sel?.kind === "shift" ? (shifts.find((s) => shiftKey(s) === sel.key) ?? null) : null;
  });
  const abOf = $derived.by((): { flight: string; split: Split; result: SplitResult } | null => {
    const sel = selected;
    if (sel?.kind !== "ab") return null;
    const r = rows.find((x) => x.key === sel.row);
    const result = r?.split?.results[sel.index];
    return r?.split && result ? { flight: r.flight, split: r.split, result } : null;
  });

  // The metric around the chosen shift, read once per series.
  $effect(() => {
    const s = shiftOf;
    if (!s) return;
    const key = `${s.metric}|${s.segment}|${s.group}`;
    if (key in series) return;
    engineCall<Series>("trend.series", { airframe_id: airframeId, metric: s.metric, segment: s.segment, group: s.group })
      .then((got) => (series = { ...series, [key]: got }))
      .catch(() => {}); // the chart is a help, not the evidence: the inspector reads without it
  });

  /** What needs a decision first: the newest worse shift still unsettled, else the newest worse one. */
  function pickFirst() {
    if (selected) return;
    const worse = [...shifts].reverse().filter((s) => s.verdict === "worse");
    const open = worse.find((s) => !testOf.get(shiftKey(s))?.explained_by) ?? worse[0];
    if (open) selected = { kind: "shift", key: shiftKey(open) };
  }

  async function pick(sel: Selection) {
    selected = sel;
    // Below the timeline on a narrow window: bring the inspector into view.
    if (globalThis.matchMedia?.("(max-width: 959px)").matches) {
      await tick();
      inspector?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  const isShift = (s: Shift) => selected?.kind === "shift" && selected.key === shiftKey(s);
  const isAb = (r: Row, i: number) => selected?.kind === "ab" && selected.row === r.key && selected.index === i;
  const current = (r: Row) =>
    (selected?.kind === "ab" && selected.row === r.key) || r.shifts.some(isShift);

  const SHOWN = 4; // measured effects listed before "show all"

  /** The row's revert-test status line, from its worse shifts' tests. */
  function shiftNote(r: Row): { status: Status; text: string } | null {
    const worse = r.shifts.filter((s) => s.verdict === "worse");
    if (!worse.length) return null;
    const tests = worse.map((s) => testOf.get(shiftKey(s))).filter((x): x is RevertTest => !!x);
    const settled = tests.find((x) => x.explained_by);
    if (settled) {
      const step = settled.steps.find((x) => x.name === settled.explained_by);
      return {
        status: "ok",
        text: t("history.shift.settled", { param: settled.explained_by ?? "", flight: step?.tested_on ? flightName(step.tested_on) : "–" }),
      };
    }
    const next = tests.find((x) => x.next_step !== null);
    const step = next?.steps.find((x) => x.order === next.next_step);
    if (step) return { status: "caution", text: t("history.shift.next", { param: step.name, value: paramValue(step.before, app.locale) }) };
    return { status: "caution", text: t("history.shift.detected") };
  }

  function abNote(s: Split): string | null {
    const r = s.results.find((x) => x.verdict !== "inconclusive") ?? s.results[0];
    const top = r?.candidates[0];
    if (!r || !top) return null;
    const origin = t(top.origin === "tuning knob" ? "history.origin.tuning_knob" : "history.origin.gcs_in_flight");
    return t("history.ab.cause", { param: top.name, origin, confidence: t(`history.confidence.${r.confidence}`) });
  }

  function openFlight(id: string) {
    app.scope.flight = id;
    app.view = "flights";
  }
</script>

<div class="history">
  <section class="timeline" aria-labelledby="history-title">
    <div class="bar">
      <h2 id="history-title">{t("history.list")}</h2>
      <div class="chips" role="group" aria-label={t("history.filters")}>
        {#each FILTERS as f (f)}
          <button type="button" aria-pressed={filter === f} onclick={() => (filter = f)}>{t(`history.filter.${f}`)}</button>
        {/each}
      </div>
    </div>
    <p class="faint">{t("history.method")}</p>
    {#if error}<p class="error">{error}</p>{/if}
    {#if !loading && checking > 0 && checked < checking}
      <p class="faint" role="status">{t("history.ab.checking", { done: checked, total: checking })}</p>
    {/if}

    {#if loading}
      <p class="faint">{t("history.loading")}</p>
    {:else if rows.length === 0 && !error}
      <p class="faint">{t("history.empty")}</p>
    {:else if shown.length === 0}
      <p class="faint">{t("history.emptyFilter")}</p>
    {:else}
      <ol class="entries">
        {#each shown as r (r.key)}
          {@const flight = byId.get(r.flight)}
          {@const params = r.sets.flatMap((s) => s.changes)}
          {@const items = changeItems(r, app.locale)}
          {@const aside = r.sets.flatMap((s) => s.aside)}
          {@const note = shiftNote(r)}
          {@const list = expanded[r.key] ? r.shifts : r.shifts.slice(0, SHOWN)}
          <li class="entry">
            <div class="when">
              <span class="mono">{numbers.has(r.flight) ? flightName(r.flight) : ""}</span>
              <span class="faint">{flightDate(flight?.utc_us ?? null, app.locale) ?? ""}</span>
            </div>
            <article class="card" aria-current={current(r)}>
              <div class="head">
                <h3>{rowTitle(r, app.locale)}</h3>
                {#if r.afterAutotune}<span class="tag">{t("history.tag.autotune")}</span>{/if}
                {#if r.split}<span class="tag">{t("history.tag.split", { at: formatNumber(r.split.at_s, app.locale) })}</span>{/if}
                {#if r.sets.some((s) => s.origin === "unexplained" && s.changes.length)}
                  <span class="tag caution"><span aria-hidden="true">▲</span> {t("history.tag.unexplained")}</span>
                {/if}
                <span class="origin">{origins(r, app.locale).join(" · ")}</span>
              </div>

              {#if items.length || (r.split?.before_s && r.split.after_s)}
                <p class="changes">
                  {#each items as it, i (i)}{i ? ", " : ""}<span class:mono={it.mono}>{it.text}</span>{#if it.note}<span class="faint">{` (${it.note})`}</span>{/if}{/each}{items.length ? ". " : ""}
                  {#if r.split?.before_s && r.split.after_s}
                    {t("history.ab.sides", {
                      b0: formatNumber(r.split.before_s[0], app.locale), b1: formatNumber(r.split.before_s[1], app.locale),
                      a0: formatNumber(r.split.after_s[0], app.locale), a1: formatNumber(r.split.after_s[1], app.locale),
                    })}
                  {/if}
                </p>
              {/if}
              {#if aside.length}<p class="faint small">{t("history.aside", { list: asideText(aside, app.locale) })}</p>{/if}
              {#each r.grades as g (g.id)}
                <p class="small">
                  <span aria-hidden="true" style:color="var(--ok)">●</span>
                  {#if g.autotune}
                    {g.doneOn
                      ? t("history.grade.autotune", { flight: flightName(g.doneOn), grade: t(`grade.${g.grade.replace(" ", "_")}` as MessageKey) })
                      : t("history.grade.autotuneOnly", { grade: t(`grade.${g.grade.replace(" ", "_")}` as MessageKey) })}
                  {:else}
                    {t("trends.event.recommendation", {
                      param: g.param ?? "–",
                      proposed: paramValue(g.proposed, app.locale),
                      grade: t(`grade.${g.grade.replace(" ", "_")}` as MessageKey),
                    })}
                  {/if}
                </p>
              {/each}

              {#if r.split}
                <div class="effects">
                  <span class="label">{t("history.effect.ab")}</span>
                  {#if r.split.skipped}
                    <p class="faint small">{t(r.split.before_s ? "history.ab.short.after" : "history.ab.short.before")}</p>
                  {:else if r.split.results.length === 0}
                    <p class="faint small">{t("history.ab.none")}</p>
                  {:else}
                    <ul>
                      {#each r.split.results as x, i (i)}
                        {@const v = verdictLook(x.verdict, x.method, app.locale)}
                        <li>
                          <button type="button" class="effect" aria-pressed={isAb(r, i)} onclick={() => pick({ kind: "ab", row: r.key, index: i })}>
                            <span class="name">{x.name[app.locale]} <span class="faint">· {segment(x.segment)}</span></span>
                            <span class="mono num">{effectText(x.effect, x.unit, app.locale)}</span>
                            <span class="mono faint num">{intervalText(x.low, x.high, app.locale)}</span>
                            <span class="verdict" style:color="var({look(v.status).tone})"><span aria-hidden="true">{look(v.status).glyph}</span> {v.text}</span>
                          </button>
                        </li>
                      {/each}
                    </ul>
                  {/if}
                </div>
              {:else if r.readings.length}
                <div class="effects">
                  <span class="label">{t("history.effect.reading")}</span>
                  <ul>
                    {#each r.readings as x, i (i)}
                      {@const st = stepStatus(x.step.state)}
                      {@const sh = shifts.find((s) => shiftKey(s) === shiftKey(x.test))}
                      <li>
                        <button type="button" class="effect" aria-pressed={sh ? isShift(sh) : false} disabled={!sh} onclick={() => pick({ kind: "shift", key: shiftKey(x.test) })}>
                          <span class="name">{x.test.name[app.locale]} <span class="faint">· {segment(x.test.segment)}</span></span>
                          <span class="mono num">{x.step.value === null ? "–" : formatValue(x.step.value, x.test.unit, app.locale)}</span>
                          <span class="mono faint num">{t("history.reading.range", {
                            before: formatNumber(x.test.before, app.locale),
                            after: formatNumber(x.test.after, app.locale),
                          })}</span>
                          <span class="verdict" style:color="var({look(st).tone})"><span aria-hidden="true">{look(st).glyph}</span> {t(`history.state.${x.step.state.replaceAll(" ", "_")}` as MessageKey)}</span>
                        </button>
                      </li>
                    {/each}
                  </ul>
                </div>
              {/if}
              {#if !r.split && (r.shifts.length || (!r.readings.length && (params.length || r.builds.length || r.firmware.length)))}
                <div class="effects">
                  <span class="label">{t("history.effect.title")}</span>
                  {#if r.shifts.length}
                    <ul>
                      {#each list as s (shiftKey(s))}
                        {@const v = verdictLook(s.verdict, shiftHow(s), app.locale)}
                        <li>
                          <button type="button" class="effect" aria-pressed={isShift(s)} onclick={() => pick({ kind: "shift", key: shiftKey(s) })}>
                            <span class="name">{s.name[app.locale]} <span class="faint">· {segment(s.segment)}</span></span>
                            <span class="mono num">{effectText(s.effect, s.unit, app.locale)}</span>
                            <span class="mono faint num">{intervalText(s.low, s.high, app.locale)}</span>
                            <span class="verdict" style:color="var({look(v.status).tone})"><span aria-hidden="true">{look(v.status).glyph}</span> {v.text}</span>
                          </button>
                        </li>
                      {/each}
                    </ul>
                    {#if r.shifts.length > SHOWN}
                      <button type="button" class="link" onclick={() => (expanded = { ...expanded, [r.key]: !expanded[r.key] })}>
                        {expanded[r.key] ? t("history.less") : t("history.more", { count: r.shifts.length })}
                      </button>
                    {/if}
                  {:else}
                    <p class="faint small"><span aria-hidden="true">◆</span> {t("history.effect.none")}</p>
                  {/if}
                </div>
              {/if}

              {#if note || r.credits.length || r.undoes.length || r.split || r.shifts.length}
                <div class="foot">
                  <div class="notes">
                    {#if note}<span style:color="var({look(note.status).tone})"><span aria-hidden="true">{look(note.status).glyph}</span> {note.text}</span>{/if}
                    {#each r.credits as c (shiftKey(c))}
                      <span style:color="var({c.kept ? '--ok' : '--caution'})">
                        <span aria-hidden="true">{c.kept ? "●" : "▲"}</span>
                        {c.kept
                          ? t("history.credit.kept", { param: c.param, metric: c.name[app.locale] })
                          : t("history.credit.undone", { param: c.param, metric: c.name[app.locale], flight: c.undone_on ? flightName(c.undone_on) : "–" })}
                      </span>
                    {/each}
                    {#each r.undoes as c (shiftKey(c))}
                      <span style:color="var(--caution)">
                        <span aria-hidden="true">▲</span>
                        {t("history.undoes", { param: c.param, flight: flightName(c.flight), metric: c.name[app.locale], value: paramValue(c.value_then, app.locale) })}
                      </span>
                    {/each}
                    {#if r.split}{@const cause = abNote(r.split)}{#if cause}<span class="faint">{cause}</span>{/if}{/if}
                  </div>
                  <div class="buttons">
                    {#if r.shifts[0] && !r.shifts.some(isShift)}
                      {@const first = r.shifts[0]}
                      <button type="button" class="secondary" onclick={() => pick({ kind: "shift", key: shiftKey(first) })}>{t("history.shift.open")}</button>
                    {/if}
                    {#if r.split}<button type="button" class="secondary" onclick={() => openFlight(r.flight)}>{t("history.openFlight")}</button>{/if}
                  </div>
                </div>
              {/if}
            </article>
          </li>
        {/each}
      </ol>
      {#if flights.length > MAX_AB}<p class="faint">{t("history.ab.bounded", { count: MAX_AB })}</p>{/if}
    {/if}
  </section>

  <aside class="inspector" bind:this={inspector} aria-label={t("history.inspector")}>
    {#if shiftOf}
      <HistoryInspector
        shift={shiftOf}
        flight={boundary(shiftOf)}
        test={testOf.get(shiftKey(shiftOf)) ?? null}
        credits={credits.filter((c) => shiftKey(c) === shiftKey(shiftOf!))}
        {fleet}
        series={series[`${shiftOf.metric}|${shiftOf.segment}|${shiftOf.group}`] ?? null}
        {flightName}
        {groupName}
        onopentrend={() => shiftOf && onopentrend(shiftOf.metric, shiftOf.segment, shiftOf.group)}
        onopenflight={openFlight}
      />
    {:else if abOf}
      <HistoryInspector
        ab={abOf}
        flight={abOf.flight}
        {fleet}
        {flightName}
        {groupName}
        onopenflight={openFlight}
      />
    {:else}
      <p class="faint">{t("history.inspector.empty")}</p>
    {/if}
  </aside>
</div>

<style>
  .history {
    display: flex;
    flex-wrap: wrap;
    gap: 20px;
    align-items: flex-start;
  }
  .timeline {
    flex: 999 1 560px;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .inspector {
    flex: 1 1 340px;
    max-width: 100%;
    box-sizing: border-box;
    position: sticky;
    top: 12px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 16px 18px;
    background: var(--surface);
    border: 1px solid var(--accent);
    border-radius: 12px;
    max-height: calc(100vh - 24px);
    overflow-y: auto;
  }
  @media (max-width: 959px) {
    .inspector {
      position: static;
      max-height: none;
    }
  }
  .bar {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
    justify-content: space-between;
  }
  h2 {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
  }
  h3 {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
    overflow-wrap: anywhere;
  }
  p {
    margin: 0;
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
  }
  .small {
    font-size: 12px;
  }
  .error {
    color: var(--out);
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .chips button {
    padding: 5px 11px;
    border: 1px solid var(--border);
    border-radius: 999px;
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    color: var(--text-muted);
    background: transparent;
  }
  .chips button[aria-pressed="true"] {
    color: var(--text);
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 16%, transparent);
  }
  .entries {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .entry {
    display: grid;
    grid-template-columns: 104px minmax(0, 1fr);
    gap: 14px;
  }
  .when {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    padding-top: 14px;
    font-size: 13px;
    text-align: right;
  }
  @media (max-width: 640px) {
    .entry {
      grid-template-columns: minmax(0, 1fr);
      gap: 4px;
    }
    .when {
      flex-direction: row;
      gap: 8px;
      align-items: baseline;
      padding-top: 0;
    }
  }
  .card {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 14px 16px;
    background: var(--surface);
    border: 1px solid var(--border-soft);
    border-radius: 12px;
    min-width: 0;
  }
  .card[aria-current="true"] {
    border-color: var(--accent);
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: baseline;
  }
  .origin {
    margin-left: auto;
    color: var(--text-faint);
    font-size: 12px;
  }
  .tag {
    padding: 1px 8px;
    border: 1px solid var(--accent);
    border-radius: 999px;
    font-size: 11px;
    color: var(--link);
  }
  .tag.caution {
    border-color: var(--caution);
    color: var(--caution);
  }
  .changes {
    font-size: 13px;
    color: var(--text-2);
    overflow-wrap: anywhere;
  }
  .effects {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding-top: 10px;
    border-top: 1px solid var(--border-soft);
  }
  .label {
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--text-faint);
  }
  .effects ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
  }
  .effect {
    width: 100%;
    display: flex;
    flex-wrap: wrap;
    gap: 4px 12px;
    align-items: baseline;
    padding: 4px 6px;
    margin: 0 -6px;
    box-sizing: content-box;
    background: none;
    border: 1px solid transparent;
    border-radius: 6px;
    color: var(--text);
    font-size: 13px;
    text-align: left;
    cursor: pointer;
  }
  .effect:disabled {
    cursor: default;
  }
  .effect[aria-pressed="true"] {
    border-color: var(--border);
    background: var(--surface-2);
  }
  .effect .name {
    flex: 1 1 200px;
    min-width: 0;
  }
  .num {
    white-space: nowrap;
  }
  .verdict {
    flex: 1 1 auto;
    text-align: right;
    font-weight: 600;
    font-size: 12px;
  }
  .foot {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
    justify-content: space-between;
    padding-top: 10px;
    border-top: 1px solid var(--border-soft);
  }
  .notes {
    display: flex;
    flex-direction: column;
    gap: 3px;
    font-size: 12px;
    font-weight: 500;
    min-width: 0;
  }
  .buttons {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-left: auto;
  }
  .secondary {
    padding: 6px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: transparent;
    color: var(--text);
    font-size: 13px;
    cursor: pointer;
  }
  .link {
    align-self: flex-start;
    background: none;
    border: none;
    padding: 0;
    color: var(--link);
    cursor: pointer;
    font-size: 12px;
  }
</style>
