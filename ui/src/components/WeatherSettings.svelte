<script lang="ts">
  // Settings, weather data (spec: Weather and wind; mock-up SettingsWeather): the
  // Open-Meteo key, which goes straight to the OS keychain through the shell and is never
  // read back, and the downloader's last round as the shell reports it: counts and
  // schedule instants only, never a tile or a place. Requests go out only at the fixed
  // slot instants, so there is no "download now": one would follow someone coming back
  // from a flight.
  import { app, t } from "../lib/app.svelte";
  import type { MessageKey } from "../lib/i18n";
  import { inDesktopApp } from "../lib/shell";
  import {
    roundLook,
    utcInstant,
    weatherKeyClear,
    weatherKeySet,
    weatherStatus,
    type WeatherStatus,
  } from "../lib/weather";
  import StatusBadge from "./StatusBadge.svelte";

  let status = $state<WeatherStatus | null>(null);
  let key = $state("");
  let note = $state("");
  let error = $state("");

  async function refresh() {
    if (!inDesktopApp()) return;
    try {
      status = await weatherStatus();
    } catch (e) {
      error = String(e);
    }
  }

  $effect(() => {
    if (app.shell.engine === "ready") refresh();
  });

  async function act(what: () => Promise<void>, done: MessageKey) {
    error = "";
    note = "";
    try {
      await what();
      note = t(done);
      await refresh();
    } catch (e) {
      error = String(e);
    }
  }

  const saveKey = () =>
    act(async () => {
      const k = key;
      key = ""; // not kept here once handed over
      await weatherKeySet(k);
    }, "wx.key.saved");

  const look = $derived(status ? roundLook(status) : null);
</script>

<section class="card" id="weather" aria-labelledby="wx-settings-title">
  <h2 id="wx-settings-title">{t("wx.settings.title")}</h2>
  <p class="faint">{t("wx.settings.about")}</p>

  {#if !inDesktopApp()}
    <p class="faint">{t("ai.off.browser")}</p>
  {:else}
    <div class="rows">
      <div class="line">
        <strong>{t("wx.key")}</strong>
        <StatusBadge status={status?.key_set ? "ok" : "info"} label={status?.key_set ? t("wx.key.set") : t("wx.key.none")} />
        <span class="faint">{t("wx.key.kept")}</span>
      </div>
      <form class="row" onsubmit={(e) => { e.preventDefault(); saveKey(); }}>
        <label>
          <span>{status?.key_set ? t("wx.key.replace") : t("wx.key.enter")}</span>
          <input type="password" autocomplete="off" spellcheck="false" bind:value={key} />
        </label>
        <button type="submit" class="primary" disabled={!key.trim()}>{t("wx.key.save")}</button>
        {#if status?.key_set}
          <button type="button" onclick={() => act(weatherKeyClear, "wx.key.removed")}>{t("wx.key.remove")}</button>
        {/if}
      </form>
      <p class="faint">{t("wx.key.plan")}</p>
      <div class="line kp">
        <strong>{t("wx.kpSource")}</strong>
        <StatusBadge status="ok" label={t("wx.kpSource.nokey")} />
        <span class="faint">{t("wx.kpSource.about")}</span>
      </div>
    </div>

    <h3>{t("wx.status")} <span class="faint">{t("wx.status.about")}</span></h3>
    {#if status && look}
      <table>
        <tbody>
          <tr>
            <th scope="row">{t("wx.status.round")}</th>
            <td>
              {status.last_ms ? utcInstant(status.last_ms, app.locale) : t("wx.status.never")}
              {#if status.why}<span class="faint"> · {status.why}</span>{/if}
            </td>
            <td class="end"><StatusBadge status={look.status} label={t(look.label)} /></td>
          </tr>
          <tr>
            <th scope="row">{t("wx.status.next")}</th>
            <td colspan="2">{utcInstant(status.next_ms, app.locale)} <span class="faint">{t("wx.status.slots")}</span></td>
          </tr>
          {#if status.tiles !== undefined}
            <tr>
              <th scope="row">{t("wx.status.tiles")}</th>
              <td colspan="2"><span class="mono">{status.tiles}</span> <span class="faint">{t("wx.status.tilesAbout")}</span></td>
            </tr>
            <tr>
              <th scope="row">{t("wx.status.kept")}</th>
              <td colspan="2" class="mono">{status.kept ?? 0}</td>
            </tr>
            <tr>
              <th scope="row">{t("wx.status.shared")}</th>
              <td colspan="2">
                {t("wx.status.sharedText", { taken: status.from_fleet ?? 0, given: status.shared ?? 0 })}
              </td>
            </tr>
          {/if}
        </tbody>
      </table>
    {:else}
      <p class="faint">{t("wx.status.unknown")}</p>
    {/if}

    <h3>{t("wx.contain")} <span class="faint">{t("wx.contain.about")}</span></h3>
    <ul>
      <li>{t("wx.contain.numbers")}</li>
      <li>{t("wx.contain.schedule")}</li>
      <li>{t("wx.contain.backfill")}</li>
      <li>{t("wx.contain.kp")}</li>
    </ul>
    <p class="faint credit">{t("wx.credit")}</p>
  {/if}
  {#if note}<p class="ok" role="status">{note}</p>{/if}
  {#if error}<p class="error" role="alert">{error}</p>{/if}
</section>

<style>
  .card {
    padding: 16px 18px;
    border-radius: 12px;
    background: var(--surface);
    border: 1px solid var(--border-soft);
    display: flex;
    flex-direction: column;
    gap: 10px;
    scroll-margin-top: 16px;
  }
  h2 {
    margin: 0;
    font-size: 15px;
  }
  h3 {
    margin: 6px 0 0;
    font-size: 14px;
  }
  h3 .faint {
    font-weight: 400;
    margin-left: 8px;
  }
  p {
    margin: 0;
  }
  .rows {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .line {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
    font-size: 13px;
  }
  .line strong {
    font-weight: 500;
  }
  .kp {
    padding-top: 10px;
    border-top: 1px solid var(--border-soft);
  }
  .row {
    display: flex;
    gap: 8px;
    align-items: flex-end;
    flex-wrap: wrap;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
    color: var(--text-muted);
  }
  input {
    min-width: 0;
    width: min(260px, 70vw);
    padding: 7px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-2);
    color: var(--text);
    font-size: 13px;
  }
  button {
    padding: 8px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface);
    color: var(--text);
    font-size: 13px;
    cursor: pointer;
  }
  .primary {
    border: 0;
    background: var(--accent);
    color: var(--accent-text);
    font-weight: 600;
  }
  button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  table {
    border-collapse: collapse;
    font-size: 13px;
    width: 100%;
  }
  th {
    text-align: left;
    font-weight: 400;
    padding: 7px 14px 7px 0;
    white-space: nowrap;
    vertical-align: top;
  }
  th,
  td {
    border-top: 1px solid var(--border-soft);
  }
  td {
    padding: 7px 10px 7px 0;
  }
  .end {
    text-align: right;
  }
  ul {
    margin: 0;
    padding-left: 18px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 13px;
  }
  .faint {
    font-size: 12px;
    color: var(--text-faint);
  }
  .credit {
    font-size: 11px;
  }
  .ok {
    color: var(--ok, var(--text));
  }
  .error {
    color: var(--out, #e5484d);
  }
  @media (max-width: 520px) {
    th {
      white-space: normal;
    }
  }
</style>
