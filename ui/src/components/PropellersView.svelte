<script lang="ts">
  // An airframe's propellers (spec: Power and efficiency analysis, "Propeller and motor
  // choice"; mock-up Propellers): the propeller flown as each weighed hover measured it,
  // any other propeller or motor predicted against it, the propellers the setup's
  // Priority favours, and what the predictions rest on (`airframe.propellers`,
  // `airframe.prop_candidate`, `airframe.prop_search`). Every figure for another
  // propeller is theory and labelled so; the app suggests one only once its predictions
  // are validated, and never changes the aircraft. Worked out on this laptop.
  import { app, t } from "../lib/app.svelte";
  import { benchFamilies, type Family } from "../lib/bench";
  import {
    adviceText,
    familyState,
    fixed,
    flightTagger,
    keyProp,
    percent,
    propellers,
    propName,
    propSearch,
    withoutList,
    type GradedChange,
    type Propellers,
    type PropSearch,
  } from "../lib/props";
  import { unitText, type FlightRow } from "../lib/report";
  import { engineCall } from "../lib/shell";
  import PropSearchPanel from "./PropSearchPanel.svelte";
  import PropTryPanel from "./PropTryPanel.svelte";
  import TheoryPill from "./TheoryPill.svelte";

  let { airframeId }: { airframeId: string } = $props();

  let view = $state<Propellers | null>(null);
  let found = $state<PropSearch | null>(null);
  let families = $state<Family[]>([]);
  let flights = $state<FlightRow[]>([]);
  let loading = $state(false);
  let searching = $state(false);
  let error = $state("");
  let searchError = $state("");

  const ready = $derived(app.shell.engine === "ready");

  // The airframe's propellers, and the search, which takes longer: each shows as it comes.
  $effect(() => {
    const id = airframeId;
    if (!ready) return;
    let live = true;
    view = null;
    found = null;
    error = "";
    searchError = "";
    loading = true;
    searching = true;
    propellers(id)
      .then((v) => {
        if (live) view = v;
      })
      .catch((e: unknown) => {
        if (live) error = String(e);
      })
      .finally(() => {
        if (live) loading = false;
      });
    propSearch(id)
      .then((v) => {
        if (live) found = v;
      })
      .catch((e: unknown) => {
        if (live) searchError = String(e);
      })
      .finally(() => {
        if (live) searching = false;
      });
    engineCall<FlightRow[]>("flight.list", { airframe_id: id })
      .then((rows) => {
        if (live) flights = rows;
      })
      .catch(() => {
        if (live) flights = [];
      });
    return () => {
      live = false;
    };
  });

  // the propeller families' names (knowledge/rules/props.json), read once
  $effect(() => {
    if (!ready) return;
    benchFamilies().then(
      (f) => (families = f),
      () => (families = []),
    );
  });

  const n = (v: number | null | undefined, digits: number) => fixed(v, digits, app.locale);
  const u = (unit: string) => unitText(unit, app.locale);
  const tag = $derived(flightTagger(flights, app.locale));
  const familyName = (id: string) => families.find((f) => f.id === id)?.label[app.locale] ?? id;
  const strip = $derived(view ? adviceText(view.advice, app.locale) : null);
  const m = $derived(view?.measured ?? null);
  const without = $derived(withoutList(m?.without));
  const needs = $derived(view ? Object.entries(view.missing).filter(([k]) => k !== "prop") : []);

  const motorText = $derived.by(() => {
    const motor = m?.motor;
    if (!motor) return t("props.measured.motor.none");
    if (motor.source === "datasheet") return t("props.measured.motor.datasheet", { kv: n(motor.kv, 0) });
    return t("props.measured.motor.table", {
      // the engine names the tables after "tables: " (performance/motor.py, drive_for)
      tables: motor.from.replace(/^tables:\s*/, ""),
      steps: motor.steps,
      rms: n(motor.rms === null ? null : motor.rms * 100, 1),
      kv: n(motor.kv, 0),
      kvFrom: t(motor.kv_from === "full throttle" ? "props.measured.kv.full" : "props.measured.kv.given"),
    });
  });

  const record = $derived(view?.track.record ?? null);
  function changeText(c: GradedChange): string {
    const named = (key: string) => {
      const p = keyProp(key);
      return p ? propName(p, app.locale) : key;
    };
    const base = { before: named(c.before), after: named(c.after), flights: c.flights };
    if (!c.graded) return t("props.track.notGraded", base);
    const speed = t("props.track.change", { ...base, speed: n((c.speed_error ?? NaN) * 100, 1) });
    if (c.power_error === undefined) return speed;
    const power = t("props.track.power", {
      power: n(c.power_error * 100, 1),
      largest: n((c.power_largest ?? NaN) * 100, 1),
      inside: n((c.inside_95 ?? NaN) * 100, 0),
    });
    return `${speed} · ${power}`;
  }

  function showRest() {
    document.getElementById("props-rest")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
</script>

<div class="stack">
  {#if error}<p class="error">{error}</p>{/if}
  {#if loading && !view}<p class="faint">{t("props.loading")}</p>{/if}

  {#if view && m && strip}
    <div class="strip" role="status">
      <TheoryPill />
      <p>{t(strip.key, strip.vars)}</p>
      <button type="button" class="how" onclick={showRest}>{t("props.status.how")}</button>
    </div>

    {#if !view.prop || needs.length}
      <section class="panel" aria-labelledby="props-needs-title">
        <h2 id="props-needs-title">{t("props.needs")}</h2>
        <ul>
          {#if !view.prop}<li>{t("props.missing.prop")}</li>{/if}
          {#each needs as [k, why] (k)}<li>{why}</li>{/each}
        </ul>
      </section>
    {/if}

    <section class="panel" aria-labelledby="props-measured-title">
      <div class="head">
        <h2 id="props-measured-title">
          {view.prop ? t("props.measured.title", { prop: propName(view.prop, app.locale) }) : t("props.measured.titleNone")}
        </h2>
        <span class="faint">{t("props.measured.about")}</span>
      </div>
      {#if m.flights.length}
        <div class="scroll">
          <table aria-label={t("props.measured.titleNone")}>
            <thead>
              <tr>
                <th scope="col">{t("props.col.flight")}</th>
                <th scope="col" class="num">{t("props.col.thrust")}</th>
                <th scope="col" class="num">{t("props.col.speed")}</th>
                <th scope="col" class="num">{t("props.col.power")}</th>
                <th scope="col" class="num">{t("props.col.gw")}</th>
                <th scope="col" class="num">{t("props.col.ct")}</th>
                <th scope="col" class="num">{t("props.col.cp")}</th>
                <th scope="col" class="num">{t("props.col.drive")}</th>
                <th scope="col" class="num">{t("props.col.fm")}</th>
                <th scope="col" class="num">{t("props.col.from")}</th>
              </tr>
            </thead>
            <tbody>
              {#each m.flights as f (f.flight_id)}
                <tr>
                  <td class="nowrap">{tag(f.flight_id)} · {f.mass_kg === null ? "–" : `${n(f.mass_kg, 1)} ${u("kg")}`}</td>
                  <td class="num mono">{n(f.thrust_g / 1000, 2)} {u("kg")}</td>
                  <td class="num mono">{n(f.rpm, 0)} {u("rpm")}</td>
                  <td class="num mono">{n(f.power_w, 0)} {u("W")}</td>
                  <td class="num mono">{n(f.g_per_w, 1)}</td>
                  <td class="num mono">{n(f.ct, 4)}</td>
                  <td class="num mono">{n(f.cp, 4)}</td>
                  <td class="num mono">{percent(f.efficiency, 0, app.locale)}</td>
                  <td class="num mono">{n(f.figure_of_merit, 2)}</td>
                  <td class="num">{t(f.power_from === "escs" ? "props.from.escs" : "props.from.packs")}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else}
        <p class="text">{t("props.measured.none")}</p>
      {/if}
      <p class="faint">
        {#if m.flights.length}{t("props.measured.rule")}{/if}
        {motorText}
        {#if without.count}{t("props.measured.without", { count: without.count, reasons: without.text })}{/if}
      </p>
    </section>

    {#if view.prop}
      {#key airframeId}
        <PropTryPanel {airframeId} {view} {found} {families} {tag} />
      {/key}
    {/if}

    <PropSearchPanel {view} {found} {searching} error={searchError} {tag} {familyName} />

    <section class="panel" id="props-rest" aria-labelledby="props-rest-title">
      <div class="head">
        <h2 id="props-rest-title">{t("props.rest.title")}</h2>
        <span class="faint">{t("props.rest.about")}</span>
      </div>
      <div class="scroll">
        <table aria-label={t("props.rest.families")}>
          <thead>
            <tr>
              <th scope="col">{t("props.col.family")}</th>
              <th scope="col" class="num">{t("props.col.tables")}</th>
              <th scope="col" class="num">{t("props.col.aircraft")}</th>
              <th scope="col" class="num">{t("props.col.spread")}</th>
              <th scope="col" class="num">{t("props.col.scatter")}</th>
              <th scope="col" class="num">{t("props.col.state")}</th>
            </tr>
          </thead>
          <tbody>
            {#each view.families as f (f.id)}
              <tr class:flown={view.prop?.family === f.id}>
                <td>{familyName(f.id)}</td>
                <td class="num mono">{f.tables}</td>
                <td class="num mono">{f.aircraft}</td>
                <td class="num mono">{percent(f.spread_power, 1, app.locale)}</td>
                <td class="num mono">{f.tables > 0 ? percent(f.noise, 1, app.locale) : "–"}</td>
                <td class="num mono">{t(familyState(f))}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <p class="faint">
        {t("props.rest.tables")}
        <button type="button" class="inline-link" onclick={() => (app.trendsTab = "range")}>{t("props.rest.enter")}</button>
      </p>
      <h3>{t("props.track.title")}</h3>
      {#if record && (record.changes > 0 || view.track.changes.length)}
        <p class="text">
          {t("props.track.record", {
            changes: record.changes,
            flights: record.flights,
            speed: n(record.speed_error === null ? null : record.speed_error * 100, 1),
            power: n(record.power_error === null ? null : record.power_error * 100, 1),
          })}
        </p>
        <ul>
          {#each view.track.changes as c, i (i)}<li>{changeText(c)}</li>{/each}
        </ul>
      {:else}
        <p class="faint">{t("props.track.none")}</p>
      {/if}
    </section>
  {/if}
</div>

<style>
  .stack {
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-width: 0;
  }
  .strip {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 14px;
    padding: 10px 14px;
    border-radius: 10px;
    border: 1px dashed color-mix(in srgb, var(--ai) 40%, var(--border));
    background: color-mix(in srgb, var(--ai) 6%, transparent);
    font-size: 12px;
    color: var(--text-2);
  }
  .strip p {
    flex: 1 1 420px;
  }
  .how,
  .inline-link {
    padding: 0;
    border: 0;
    background: none;
    color: var(--link);
    font-size: 12px;
    text-decoration: underline;
    cursor: pointer;
  }
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
  h3 {
    margin: 4px 0 0;
    font-size: 13px;
    font-weight: 600;
  }
  p {
    margin: 0;
  }
  ul {
    margin: 0;
    padding-left: 18px;
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
  .num {
    text-align: right;
    white-space: nowrap;
  }
  .nowrap {
    white-space: nowrap;
  }
  tr.flown td:first-child {
    color: var(--link);
  }
</style>
