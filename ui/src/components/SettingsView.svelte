<script lang="ts">
  // Settings (spec: AI layer, "Sending"; "Visible to the user"). The Claude API key is
  // pasted here once and goes straight to the OS keychain through the shell: this view
  // never holds it afterwards and can never read it back. The monthly cap holds back
  // any request that could pass it. Below, every packet the AI layer built, exactly as
  // checked, with what both location checks said: what left this machine. Between them,
  // the weather key and the downloader's state (WeatherSettings).
  import { app, t } from "../lib/app.svelte";
  import {
    aiKeyClear,
    aiKeySet,
    aiSettingsSet,
    aiStatus,
    hlcTime,
    prettyPacket,
    type SentPacket,
  } from "../lib/ai";
  import type { MessageKey } from "../lib/i18n";
  import { formatTakeoff } from "../lib/report";
  import { engineCall, inDesktopApp } from "../lib/shell";
  import WeatherSettings from "./WeatherSettings.svelte";

  let key = $state("");
  let cap = $state<number>(20);
  let note = $state("");
  let error = $state("");
  let sent = $state<SentPacket[]>([]);

  async function refresh() {
    if (!inDesktopApp()) return;
    try {
      app.ai = await aiStatus();
      if (app.ai) cap = app.ai.monthly_cap_usd;
      sent = await engineCall<SentPacket[]>("ai.sent", { limit: 200 });
    } catch (e) {
      error = String(e);
    }
  }

  $effect(() => {
    if (app.shell.engine === "ready") refresh();
  });

  // Another view asked for a section (the flight's Weather tab: "Weather settings").
  $effect(() => {
    const section = app.settingsSection;
    if (!section) return;
    app.settingsSection = null;
    requestAnimationFrame(() => document.getElementById(section)?.scrollIntoView({ block: "start" }));
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
      await aiKeySet(k);
    }, "settings.key.saved");

  const money = (usd: number) =>
    new Intl.NumberFormat(app.locale, { style: "currency", currency: "USD" }).format(usd);
  const verdict = (v: string | null) => t(`sent.verdict.${v ?? "none"}` as MessageKey);
</script>

<main>
  <h1>{t("settings.title")}</h1>

  <section class="card" aria-labelledby="ai-title">
    <h2 id="ai-title">{t("settings.ai")}</h2>
    <p class="faint">{t("settings.ai.about")}</p>
    {#if !app.ai}
      <p class="faint">{t("ai.off.browser")}</p>
    {:else}
      <p>
        {#if app.ai.key_rejected}{t("settings.key.rejected")}{:else if app.ai.key_set}{t("settings.key.set")}{:else}{t("settings.key.none")}{/if}
      </p>
      <p class="faint">{t("settings.key.fleet")}</p>
      <form class="row" onsubmit={(e) => { e.preventDefault(); saveKey(); }}>
        <label>
          <span>{t("settings.key")}</span>
          <input type="password" autocomplete="off" spellcheck="false" placeholder="sk-ant-…" bind:value={key} />
        </label>
        <button type="submit" class="primary" disabled={!key.trim()}>{t("settings.key.save")}</button>
        {#if app.ai.key_set}
          <button type="button" onclick={() => act(aiKeyClear, "settings.key.remove")}>{t("settings.key.remove")}</button>
        {/if}
      </form>
      <form class="row" onsubmit={(e) => { e.preventDefault(); act(() => aiSettingsSet(cap, app.locale), "settings.cap.saved"); }}>
        <label>
          <span>{t("settings.cap")}</span>
          <input type="number" min="0" max="10000" step="1" bind:value={cap} />
        </label>
        <button type="submit">{t("settings.cap.save")}</button>
      </form>
      {#if app.ai.month}
        <p class="faint">
          {t("settings.spend", { month: app.ai.month.month, cost: money(app.ai.month.cost_usd), count: app.ai.month.exchanges })}
          {#if app.ai.queued} · {t("settings.queued", { count: app.ai.queued })}{/if}
        </p>
      {/if}
    {/if}
    {#if note}<p class="ok" role="status">{note}</p>{/if}
    {#if error}<p class="error" role="alert">{error}</p>{/if}
  </section>

  <WeatherSettings />

  <section class="card" aria-labelledby="sent-title">
    <h2 id="sent-title">{t("sent.title")}</h2>
    <p class="faint">{t("sent.about")}</p>
    {#if sent.length === 0}
      <p class="faint">{t("sent.empty")}</p>
    {/if}
    <ul class="sent">
      {#each sent as p (p.id)}
        {@const when = hlcTime(p.sent_hlc ?? p.created_hlc)}
        <li class:refused={!p.sent}>
          <div class="line">
            <strong>{t(`sent.purpose.${p.purpose}` as MessageKey)}</strong>
            <span class="faint">{when ? formatTakeoff(when.getTime() * 1000, app.locale) : ""}</span>
            <span class:sent-tag={p.sent} class:kept-tag={!p.sent}>{p.sent ? t("sent.sent") : t("sent.kept")}</span>
            <span class="faint">{t("sent.engine", { verdict: verdict(p.engine_verdict) })}</span>
            <span class="faint">{t("sent.shell", { verdict: verdict(p.shell_verdict) })}</span>
          </div>
          {#each [...p.engine_reasons, ...p.shell_reasons] as r, i (i)}
            <p class="reason mono">{r.path} · {r.rule}</p>
          {/each}
          <details>
            <summary>{t("sent.show")} <span class="mono faint">{p.sha256.slice(0, 12)}</span></summary>
            <pre class="mono">{prettyPacket(p.packet_json)}</pre>
          </details>
        </li>
      {/each}
    </ul>
  </section>
</main>

<style>
  main {
    padding: 16px 24px 32px;
    display: flex;
    flex-direction: column;
    gap: 16px;
    max-width: 980px;
  }
  h1 {
    margin: 0;
    font-size: 20px;
  }
  h2 {
    margin: 0;
    font-size: 15px;
  }
  .card {
    padding: 16px 18px;
    border-radius: 12px;
    background: var(--surface);
    border: 1px solid var(--border-soft);
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  p {
    margin: 0;
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
    min-width: 260px;
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
  summary {
    cursor: pointer;
    font-size: 13px;
  }
  .faint {
    font-size: 12px;
    color: var(--text-faint);
  }
  .sent {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .sent li {
    border: 1px solid var(--border-soft);
    border-radius: 8px;
    padding: 8px 10px;
  }
  .line {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    align-items: baseline;
    font-size: 13px;
  }
  .sent-tag {
    color: var(--ai);
  }
  .kept-tag {
    color: var(--text-muted);
  }
  .reason {
    font-size: 12px;
    color: var(--caution, var(--text-muted));
  }
  pre {
    max-height: 360px;
    overflow: auto;
    font-size: 11px;
    background: var(--surface-2);
    padding: 8px;
    border-radius: 6px;
  }
  .ok {
    color: var(--ok, var(--text));
  }
  .error {
    color: var(--out, #e5484d);
  }
</style>
