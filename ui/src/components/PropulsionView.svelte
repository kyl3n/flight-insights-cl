<script lang="ts">
  // Propulsion and power for one flight (spec: User interface, "Propulsion and
  // power"): each motor's output and its ESC's RPM, temperature, current and supply
  // voltage, the battery's voltage, current, power and resistance, with the motor that
  // works hardest or runs hottest picked out. Motors and segments can be chosen.
  import { untrack } from "svelte";
  import { app, t } from "../lib/app.svelte";
  import { motorColour, standout, zoomTo, type MotorRow, type PropulsionView, type Trace } from "../lib/flightviews";
  import { formatNumber, unitText } from "../lib/report";
  import { engineCall } from "../lib/shell";
  import TimePlot from "./TimePlot.svelte";

  let { flightId }: { flightId: string } = $props();

  let view = $state<PropulsionView | null>(null);
  let error = $state("");
  let segment = $state<"flight" | "hover">("flight");
  let hidden = $state<number[]>([]); // motors not drawn

  // The whole flight first; choosing hover asks again for just that stretch, so it is
  // drawn from its own bins (every sample, where it holds fewer than the bins).
  let context = $state<{ window_s: [number, number]; hover_s: [number, number][] } | null>(null);
  $effect(() => {
    void flightId;
    view = null;
    context = null;
    segment = "flight";
    hidden = [];
  });
  $effect(() => {
    const hover = segment === "hover";
    const window_s = untrack(() => (hover && context ? zoomTo(context, "hover") : null));
    engineCall<PropulsionView>("flight.propulsion", { flight_id: flightId, window_s })
      .then((v) => ((view = v), (context ??= { window_s: v.window_s, hover_s: v.hover_s }), (error = "")))
      .catch((e: unknown) => (error = String(e)));
  });

  const range = $derived(view ? view.shown_s : ([0, 1] as [number, number]));
  const shown = $derived(view ? view.motors.filter((m) => !hidden.includes(m.number)) : []);
  const hardest = $derived(view ? standout(view.motors, (m) => m.hover_output) : null);
  const hottest = $derived(view ? standout(view.motors, (m) => m.peak_temp) : null);
  const has = (key: "output" | "rpm" | "temp" | "curr" | "volt") => view?.motors.some((m) => m[key]) ?? false;

  function traces(key: "output" | "rpm" | "temp" | "curr" | "volt"): Trace[] {
    return shown.flatMap((m) => {
      const env = m[key];
      return env ? [{ label: t("prop.motor", { n: m.number }), env, colour: motorColour(m.number) }] : [];
    });
  }

  function toggle(n: number) {
    hidden = hidden.includes(n) ? hidden.filter((x) => x !== n) : [...hidden, n];
  }

  const num = (v: number | null | undefined, digits = 1) =>
    v === null || v === undefined ? "–" : formatNumber(Math.round(v * 10 ** digits) / 10 ** digits, app.locale);
  const withUnit = (v: number | null | undefined, unit: string | undefined, digits = 1) =>
    v === null || v === undefined
      ? "–"
      : unit === "%"
        ? `${num(v, digits)}%`
        : `${num(v, digits)} ${unitText(unit ?? "", app.locale)}`.trim();
  const cell = (m: MotorRow, key: "hover_output" | "hover_rpm" | "peak_temp" | "hover_curr" | "hover_volt", unit: string | undefined, digits = 1) =>
    withUnit(m[key], unit, digits);
</script>

{#if error}<p class="error">{error}</p>{/if}
{#if !view && !error}<p class="faint">{t("report.analysing")}</p>{/if}

{#if view}
  {@const u = view.units}
  <div class="stack">
    {#if view.simulated}<p class="notice">{t("views.simulated")}</p>{/if}

    <section class="panel">
      <div class="row">
        <h2>{t("prop.motors")}</h2>
        <div role="group" aria-label={t("views.segment")} class="toggle">
          {#each ["flight", "hover"] as const as s (s)}
            <button type="button" aria-pressed={segment === s} disabled={s === "hover" && !view.hover_s.length} onclick={() => (segment = s)}>
              {t(`views.segment.${s}`)}
            </button>
          {/each}
        </div>
      </div>
      {#if view.motors.length === 0}
        <p class="faint">{t("prop.none")}</p>
      {:else}
        <table>
          <thead>
            <tr>
              <th>{t("prop.col.motor")}</th>
              <th>{t("prop.col.output")}</th>
              {#if has("rpm")}<th>{t("prop.col.rpm")}</th>{/if}
              {#if has("temp")}<th>{t("prop.col.temp")}</th>{/if}
              {#if has("curr")}<th>{t("prop.col.curr")}</th>{/if}
              {#if has("volt")}<th>{t("prop.col.volt")}</th>{/if}
            </tr>
          </thead>
          <tbody>
            {#each view.motors as m (m.number)}
              <tr>
                <td>
                  <label class="pick">
                    <input type="checkbox" checked={!hidden.includes(m.number)} onchange={() => toggle(m.number)} />
                    <span class="swatch" style:background={motorColour(m.number)}></span>
                    {t("prop.motor", { n: m.number })}
                  </label>
                </td>
                <td class="mono">
                  {cell(m, "hover_output", "%")}
                  {#if hardest === m.number}<span class="flag">▲ {t("prop.hardest")}</span>{/if}
                </td>
                {#if has("rpm")}<td class="mono">{cell(m, "hover_rpm", u.rpm, 0)}</td>{/if}
                {#if has("temp")}
                  <td class="mono">
                    {cell(m, "peak_temp", u.temp)}
                    {#if hottest === m.number}<span class="flag">▲ {t("prop.hottest")}</span>{/if}
                  </td>
                {/if}
                {#if has("curr")}<td class="mono">{cell(m, "hover_curr", u.curr)}</td>{/if}
                {#if has("volt")}<td class="mono">{cell(m, "hover_volt", u.volt, 2)}</td>{/if}
              </tr>
            {/each}
          </tbody>
        </table>
        <p class="faint">{t("prop.hoverNote")}</p>
      {/if}
    </section>

    {#if has("output")}
      <section class="panel">
        <h2>{t("prop.output")}</h2>
        <TimePlot traces={traces("output")} unit="%" {range} armed={view.armed_s} hover={view.hover_s} modes={view.modes} level={view.saturation_pct} label={t("prop.output")} height={200} />
        <p class="faint">{t("prop.saturation", { pct: num(view.saturation_pct, 0) })}</p>
      </section>
    {/if}
    {#each [["rpm", "prop.rpm"], ["temp", "prop.temp"], ["curr", "prop.curr"], ["volt", "prop.volt"]] as const as [key, title] (key)}
      {#if has(key)}
        <section class="panel">
          <h2>{t(title)}</h2>
          <TimePlot traces={traces(key)} unit={u[key] ?? ""} {range} armed={view.armed_s} hover={view.hover_s} modes={view.modes} label={t(title)} />
        </section>
      {/if}
    {/each}

    {#if view.battery}
      {@const b = view.battery}
      <section class="panel">
        <h2>{t("prop.battery")}</h2>
        <p class="faint">
          {b.resting_volt !== null
            ? t("prop.bat.summary", { rest: withUnit(b.resting_volt, u.volt, 2), low: withUnit(b.lowest_volt, u.volt, 2) })
            : t("prop.bat.lowOnly", { low: withUnit(b.lowest_volt, u.volt, 2) })}
        </p>
        {#each [["volt", "prop.bat.volt", u.volt], ["curr", "prop.bat.curr", u.curr], ["power", "prop.bat.power", u.power], ["res", "prop.bat.res", u.res]] as const as [key, title, unit] (key)}
          {@const env = b[key]}
          {#if env}
            <h3>{t(title)}</h3>
            <TimePlot traces={[{ label: t(title), env, colour: "#4A9FD8" }]} unit={unit ?? ""} {range} armed={view.armed_s} hover={view.hover_s} modes={view.modes} label={t(title)} height={140} />
          {/if}
        {/each}
      </section>
    {/if}
    <p class="faint">{t("views.legend")}</p>
    <p class="faint">{t("prop.later")}</p>
  </div>
{/if}

<style>
  .stack {
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
  h2 {
    margin: 0;
    font-size: 14px;
  }
  h3 {
    margin: 6px 0 0;
    font-size: 13px;
    color: var(--text-muted);
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
  .row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .toggle {
    display: flex;
    gap: 4px;
  }
  .toggle button {
    background: none;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text-2);
    padding: 3px 10px;
    cursor: pointer;
    font-size: 12px;
  }
  .toggle button[aria-pressed="true"] {
    border-color: var(--accent);
    color: var(--text);
  }
  .toggle button:disabled {
    opacity: 0.5;
    cursor: default;
  }
  table {
    border-collapse: collapse;
    font-size: 13px;
    align-self: flex-start;
  }
  th {
    text-align: left;
    font-weight: 500;
    color: var(--text-muted);
    padding: 4px 14px 4px 0;
  }
  td {
    padding: 3px 14px 3px 0;
    border-top: 1px solid var(--border-soft);
  }
  .pick {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
  }
  .swatch {
    width: 10px;
    height: 10px;
    border-radius: 2px;
  }
  .flag {
    margin-left: 6px;
    font-size: 11px;
    color: var(--caution);
  }
</style>
