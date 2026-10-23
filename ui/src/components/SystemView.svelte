<script lang="ts">
  // System and buses for one flight (spec: User interface, "System and buses"): the
  // main loop's load, its long loops and longest loop from the performance monitor,
  // blocks the logger dropped, and every error the firmware logged, by subsystem and
  // meaning, marked on every plot at the moment it was logged.
  import { untrack } from "svelte";
  import { app, t } from "../lib/app.svelte";
  import { groupErrors, zoomTo, type SystemView } from "../lib/flightviews";
  import { formatNumber, unitText } from "../lib/report";
  import { engineCall } from "../lib/shell";
  import TimePlot from "./TimePlot.svelte";

  let { flightId }: { flightId: string } = $props();

  let view = $state<SystemView | null>(null);
  let error = $state("");
  let segment = $state<"flight" | "hover">("flight");

  // The whole flight first; choosing hover asks again for just that stretch, so it is
  // drawn from its own bins (every sample, where it holds fewer than the bins).
  let context = $state<{ window_s: [number, number]; hover_s: [number, number][] } | null>(null);
  $effect(() => {
    void flightId;
    view = null;
    context = null;
    segment = "flight";
  });
  $effect(() => {
    const hover = segment === "hover";
    const window_s = untrack(() => (hover && context ? zoomTo(context, "hover") : null));
    engineCall<SystemView>("flight.system", { flight_id: flightId, window_s })
      .then((v) => ((view = v), (context ??= { window_s: v.window_s, hover_s: v.hover_s }), (error = "")))
      .catch((e: unknown) => (error = String(e)));
  });

  const range = $derived(view ? view.shown_s : ([0, 1] as [number, number]));
  const errorTimes = $derived(view ? view.errors.map((e) => e.t) : []);
  const plots = [
    ["load", "sys.load"],
    ["long_loops", "sys.longLoops"],
    ["max_loop", "sys.maxLoop"],
    ["log_dropped", "sys.dropped"],
  ] as const;
  const num = (v: number, digits = 1) => formatNumber(Math.round(v * 10 ** digits) / 10 ** digits, app.locale);
</script>

{#if error}<p class="error">{error}</p>{/if}
{#if !view && !error}<p class="faint">{t("report.analysing")}</p>{/if}

{#if view}
  <div class="stack">
    {#if view.simulated}<p class="notice">{t("sys.simulated")}</p>{/if}
    <div class="row">
      <p class="faint">
        {view.loop_period_us !== null ? t("sys.period", { us: num(view.loop_period_us, 0) }) : ""}
      </p>
      <div role="group" aria-label={t("views.segment")} class="toggle">
        {#each ["flight", "hover"] as const as s (s)}
          <button type="button" aria-pressed={segment === s} disabled={s === "hover" && !view.hover_s.length} onclick={() => (segment = s)}>
            {t(`views.segment.${s}`)}
          </button>
        {/each}
      </div>
    </div>

    {#each plots as [key, title] (key)}
      {@const env = view[key]}
      {#if env}
        <section class="panel">
          <h2>{t(title)}</h2>
          <TimePlot
            traces={[{ label: t(title), env, colour: "#4A9FD8" }]}
            unit={view.units[key] ?? ""}
            {range}
            armed={view.armed_s}
            hover={view.hover_s}
            modes={view.modes}
            errors={errorTimes}
            label={t(title)}
            height={150}
          />
        </section>
      {/if}
    {/each}

    <section class="panel">
      <h2>{t("sys.errors")}</h2>
      {#if view.errors.length === 0}
        <p class="faint">{t("sys.errors.none")}</p>
      {:else}
        <table>
          <thead>
            <tr>
              <th>{t("sys.col.time")}</th>
              <th>{t("sys.col.subsystem")}</th>
              <th>{t("sys.col.meaning")}</th>
              <th>{t("sys.col.count")}</th>
            </tr>
          </thead>
          <tbody>
            {#each groupErrors(view.errors) as e (`${e.subsystem}|${e.code}`)}
              <tr>
                <td class="mono">{num(e.t)} {unitText("s", app.locale)}</td>
                <td class="mono">{e.subsystem}</td>
                <td>{e.meaning} <span class="faint mono">({e.code})</span></td>
                <td class="mono">{e.count}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </section>
    <p class="faint">{t("views.legend")}</p>
    <p class="faint">{t("sys.notLogged")}</p>
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
</style>
