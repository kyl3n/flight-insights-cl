<script lang="ts">
  // One flight's weather (spec: Weather and wind; mock-up Weather): the record worked
  // out on this laptop from whole regional tiles, at the surface and the flight's height
  // band, the wind the aircraft measured beside the model's, and the K-index. Every value
  // comes from the engine's flight.weather; none is worked out here.
  import { app, t } from "../lib/app.svelte";
  import { unitText } from "../lib/report";
  import {
    band,
    bearing,
    covered,
    flightWeather,
    kpLevel,
    modelName,
    num,
    signed,
    type FlightWeather,
  } from "../lib/weather";

  let { flightId }: { flightId: string } = $props();

  let view = $state<FlightWeather | null>(null);
  let error = $state("");

  $effect(() => {
    void flightId;
    view = null;
    error = "";
    flightWeather(flightId)
      .then((v) => (view = v))
      .catch((e: unknown) => (error = String(e)));
  });

  const ms = $derived(unitText("m/s", app.locale));
  const m = $derived(unitText("m", app.locale));
  const degC = $derived(unitText("degC", app.locale));

  function openSettings() {
    app.settingsSection = "weather";
    app.view = "settings";
  }
</script>

{#if error}<p class="error">{error}</p>{/if}
{#if !view && !error}<p class="faint">{t("report.analysing")}</p>{/if}

{#if view}
  {@const rec = view.record}
  {@const kp = kpLevel(rec?.kp_max ?? null, view.kp_elevated)}
  <div class="layout">
    <div class="stack">
      <section class="panel" aria-labelledby="wx-record">
        <div class="head">
          <h2 id="wx-record">{t("wx.record")}</h2>
          <span class="faint">{t("wx.record.about")}</span>
        </div>
        {#if !rec}
          <p class="faint">{t("wx.record.cannot")}</p>
        {:else if !covered(rec)}
          <p>{t("wx.record.none")}</p>
          <p class="faint">{t("wx.record.noneWhy")}</p>
          <div><button type="button" onclick={openSettings}>{t("wx.settings")}</button></div>
        {:else}
          <div class="chips">
            <span class="chip">{t("wx.model")} <strong>{modelName(rec.quality)}</strong></span>
            <span class="chip" class:grade-forecast={rec.quality !== "reanalysis"} class:grade-reanalysis={rec.quality === "reanalysis"}>
              {t(`wx.grade.${rec.quality}` as const)}
            </span>
            <span class="chip">{t("wx.coverage", { pct: num(rec.coverage * 100, 0, app.locale) })}</span>
            {#if rec.kp_max !== null}
              <span class="chip" class:kp-elevated={kp === "elevated"}>
                {t("wx.kp", { kp: num(rec.kp_max, 1, app.locale) })} · {t(`wx.kp.${kp}` as const)}
              </span>
            {/if}
          </div>
          <div class="scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col"></th>
                  <th scope="col">{t("wx.col.surface")}</th>
                  <th scope="col">
                    {band(rec, app.locale) ? t("wx.col.band", { band: band(rec, app.locale) ?? "", unit: m }) : t("wx.col.bandNone")}
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <th scope="row">{t("wx.row.wind")}</th>
                  <td class="mono">{num(rec.surface_wind_ms, 1, app.locale)} {ms}</td>
                  <td class="mono">
                    {num(rec.wind_ms, 1, app.locale)} {ms} · {bearing(rec.wind_from_deg)}
                    {#if rec.wind_max_ms != null}<span class="faint">{t("wx.largest", { value: num(rec.wind_max_ms, 1, app.locale), unit: ms })}</span>{/if}
                  </td>
                </tr>
                <tr>
                  <th scope="row">{t("wx.row.gusts")}</th>
                  <td class="mono">{num(rec.gust_max_ms, 1, app.locale)} {ms}</td>
                  <td class="faint">{t("wx.gusts.where")}</td>
                </tr>
                <tr>
                  <th scope="row">{t("wx.row.temperature")}</th>
                  <td class="mono" colspan="2">{num(rec.temperature_c, 1, app.locale)} {degC} <span class="faint">{t("wx.atTakeoff")}</span></td>
                </tr>
                <tr>
                  <th scope="row">{t("wx.row.humidity")}</th>
                  <td class="mono" colspan="2">
                    {num(rec.humidity_pct, 0, app.locale)}% · {t("wx.dewPoint", { value: num(rec.dew_point_c, 1, app.locale), unit: degC })}
                  </td>
                </tr>
                <tr>
                  <th scope="row">{t("wx.row.pressure")}</th>
                  <td class="mono" colspan="2">{num(rec.pressure_hpa, 0, app.locale)} {unitText("hPa", app.locale)} <span class="faint">{t("wx.localOnly")}</span></td>
                </tr>
                <tr>
                  <th scope="row">{t("wx.row.density")}</th>
                  <td class="mono">{num(rec.density_altitude_m, 0, app.locale)} {m} <span class="faint">{t("wx.atTakeoff")}</span></td>
                  <td class="mono">{num(rec.density_altitude_top_m, 0, app.locale)} {m} <span class="faint">{t("wx.atTop")}</span></td>
                </tr>
                <tr>
                  <th scope="row">{t("wx.row.sky")}</th>
                  <td class="mono" colspan="2">
                    {t("wx.sky", {
                      rain: num(rec.precipitation_mm_h, 1, app.locale),
                      cloud: num(rec.cloud_cover_pct, 0, app.locale),
                      base: num(rec.cloud_base_m, 0, app.locale),
                      unit: m,
                    })}
                  </td>
                </tr>
                <tr>
                  <th scope="row">{t("wx.row.boundary")}</th>
                  <td class="mono" colspan="2">
                    {#if rec.boundary_layer_m != null}{num(rec.boundary_layer_m, 0, app.locale)} {m}{:else}<span class="faint">{t("wx.boundary.none")}</span>{/if}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="faint">{t("wx.localNote")}</p>
        {/if}
      </section>

      <section class="panel" aria-labelledby="wx-measured">
        <div class="head">
          <h2 id="wx-measured">{t("wx.measured")}</h2>
          <span class="faint">{t("wx.measured.about")}</span>
        </div>
        {#if !view.measured}
          <p class="faint">{t("wx.measured.none")}</p>
        {:else}
          {@const own = view.measured}
          <dl class="pairs">
            <dt>{t("wx.measured.wind")}</dt>
            <dd class="mono">
              {num(own.speed_ms, 1, app.locale)} {ms} · {bearing(own.from_deg)}
              <span class="faint">{t("wx.measured.peak", { value: num(own.peak_ms, 1, app.locale), unit: ms })}</span>
            </dd>
            <dt>{t("wx.measured.where")}</dt>
            <dd>{t("wx.measured.windows", { n: own.windows, height: num(own.height_m, 0, app.locale), unit: m })}</dd>
            <dt>{t("wx.measured.from")}</dt>
            <dd>
              {own.source === "flight" ? t("wx.measured.fromFlight") : t("wx.measured.fromAirframe", { n: own.drag_from.length })}
            </dd>
            {#if view.difference && rec && covered(rec)}
              <dt>{t("wx.measured.gap", { model: modelName(rec.quality) })}</dt>
              <dd class="mono">
                {signed(view.difference.speed_ms, 1, app.locale)} {ms}
                {#if view.difference.from_deg !== undefined}
                  · {t("wx.measured.turned", { deg: signed(view.difference.from_deg, 0, app.locale) })}
                {/if}
              </dd>
            {:else}
              <dt>{t("wx.measured.gapNone")}</dt>
              <dd class="faint">{t("wx.measured.gapWhy")}</dd>
            {/if}
          </dl>
        {/if}
      </section>
    </div>

    <aside class="panel side" aria-labelledby="wx-why">
      <p class="faint">{t("wx.why.kicker")}</p>
      <h2 id="wx-why">{t("wx.why")}</h2>
      <ul>
        <li>{t("wx.why.trends")}</li>
        {#if rec && rec.kp_max !== null}
          <li>
            {kp === "elevated"
              ? t("wx.why.kpElevated", { kp: num(rec.kp_max, 1, app.locale), level: num(view.kp_elevated, 0, app.locale) })
              : t("wx.why.kpQuiet", { kp: num(rec.kp_max, 1, app.locale), level: num(view.kp_elevated, 0, app.locale) })}
          </li>
        {/if}
        {#if rec && covered(rec) && rec.quality !== "reanalysis"}<li>{t("wx.why.upgrade")}</li>{/if}
      </ul>
      <p class="private">{t("wx.private")}</p>
      <div><button type="button" onclick={openSettings}>{t("wx.settings")}</button></div>
      <p class="faint credit">{t("wx.credit")}</p>
    </aside>
  </div>
{/if}

<style>
  .layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(240px, 320px);
    gap: 12px;
    align-items: start;
  }
  @media (max-width: 900px) {
    .layout {
      grid-template-columns: minmax(0, 1fr);
    }
  }
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
  .chips {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .chip {
    padding: 3px 9px;
    border: 1px solid var(--border);
    border-radius: 6px;
    font-size: 12px;
    color: var(--text-muted);
  }
  .chip strong {
    color: var(--text);
    font-weight: 500;
  }
  .grade-forecast {
    color: var(--caution);
    border-color: color-mix(in srgb, var(--caution) 45%, transparent);
  }
  .grade-reanalysis {
    color: var(--ok);
    border-color: color-mix(in srgb, var(--ok) 45%, transparent);
  }
  .kp-elevated {
    color: var(--caution);
    border-color: color-mix(in srgb, var(--caution) 45%, transparent);
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
    padding: 6px 14px 6px 0;
    white-space: nowrap;
  }
  tbody th {
    color: var(--text);
    font-weight: 400;
  }
  td {
    padding: 6px 14px 6px 0;
    border-top: 1px solid var(--border-soft);
  }
  tbody th {
    border-top: 1px solid var(--border-soft);
  }
  td .faint {
    margin-left: 6px;
    font-family: var(--font);
  }
  .pairs {
    display: grid;
    grid-template-columns: minmax(120px, max-content) minmax(0, 1fr);
    gap: 8px 16px;
    margin: 0;
    font-size: 13px;
  }
  .pairs dt {
    color: var(--text-muted);
  }
  .pairs dd {
    margin: 0;
  }
  .pairs .faint {
    margin-left: 6px;
    font-family: var(--font);
  }
  @media (max-width: 520px) {
    .pairs {
      grid-template-columns: minmax(0, 1fr);
      gap: 2px;
    }
    .pairs dd {
      margin-bottom: 8px;
    }
    td,
    th {
      padding-right: 8px;
    }
  }
  .side ul {
    margin: 0;
    padding-left: 18px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    font-size: 13px;
  }
  .private {
    font-size: 12px;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-2);
  }
  .credit {
    font-size: 11px;
  }
  button {
    padding: 7px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface);
    color: var(--text);
    font-size: 13px;
    cursor: pointer;
  }
</style>
