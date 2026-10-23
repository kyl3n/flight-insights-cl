<script lang="ts">
  // The flights of the airframe or platform in scope, newest first, each with the
  // worst of its findings; opening one shows its report.
  import { app, t } from "../lib/app.svelte";
  import { formatDuration, formatTakeoff, severityLook, type FlightRow } from "../lib/report";
  import { engineCall } from "../lib/shell";
  import StatusBadge from "./StatusBadge.svelte";

  let flights = $state<FlightRow[]>([]);
  let error = $state("");
  const ready = $derived(app.shell.engine === "ready");

  $effect(() => {
    if (!ready) return;
    const params = { airframe_id: app.scope.airframe, platform_id: app.scope.platform };
    engineCall<FlightRow[]>("flight.list", params)
      .then((rows) => {
        flights = rows;
        error = "";
      })
      .catch((e: unknown) => (error = String(e)));
  });

  function duration(f: FlightRow): string {
    const d = formatDuration(f.duration_s);
    return d ? t("flights.duration", d) : "–";
  }
</script>

<main>
  <div class="heading">
    <h1>{t("flights.title")}</h1>
    <p class="faint">{t("flights.summary", { count: flights.length })}</p>
  </div>
  {#if error}<p class="error">{error}</p>{/if}
  {#if !ready}
    <p class="faint">{t("setup.unavailable")}</p>
  {:else if flights.length === 0}
    <p class="faint">{t("flights.empty")}</p>
  {:else}
    <table>
      <thead>
        <tr>
          <th scope="col">{t("flights.col.flight")}</th>
          <th scope="col">{t("flights.col.airframe")}</th>
          <th scope="col">{t("flights.col.duration")}</th>
          <th scope="col">{t("flights.col.modes")}</th>
          <th scope="col">{t("flights.col.status")}</th>
        </tr>
      </thead>
      <tbody>
        {#each flights as f (f.id)}
          <tr>
            <td>
              <button type="button" class="link" onclick={() => (app.scope.flight = f.id)}>
                {formatTakeoff(f.utc_us, app.locale) ?? t("flights.noTime")}
              </button>
            </td>
            <td>{f.airframe}</td>
            <td class="mono">{duration(f)}</td>
            <td class="faint">{f.modes.join(", ")}</td>
            <td>
              {#if !f.analysis}
                <span class="faint">{t("flights.notAnalysed")}</span>
              {:else if f.analysis.worst}
                {@const look = severityLook(f.analysis.worst)}
                <StatusBadge status={look.status} label="{t(look.label)} · {f.analysis.counts[f.analysis.worst]}" />
              {:else}
                <StatusBadge status="ok" />
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</main>

<style>
  main {
    padding: 20px 24px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .heading {
    display: flex;
    align-items: baseline;
    gap: 14px;
  }
  h1 {
    margin: 0;
    font-size: 20px;
  }
  p {
    margin: 0;
  }
  .faint {
    color: var(--text-faint);
    font-size: 13px;
  }
  .error {
    color: var(--out);
  }
  table {
    border-collapse: collapse;
    width: 100%;
  }
  th,
  td {
    text-align: left;
    padding: 8px 10px;
    border-bottom: 1px solid var(--border-soft);
  }
  th {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-muted);
  }
  .link {
    background: none;
    border: none;
    padding: 0;
    color: var(--link);
    cursor: pointer;
    font-size: 14px;
  }
</style>
