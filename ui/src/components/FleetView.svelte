<script lang="ts">
  import { app, t } from "../lib/app.svelte";
  import type { MessageKey } from "../lib/i18n";
  import StatusBadge from "./StatusBadge.svelte";

  type Filter = "all" | "attention" | "tuning" | "healthy";
  let filter: Filter = $state("all");
  const filters: [Filter, MessageKey][] = [
    ["all", "fleet.filter.all"],
    ["attention", "fleet.filter.attention"],
    ["tuning", "fleet.filter.tuning"],
    ["healthy", "fleet.filter.healthy"],
  ];
  // Platforms and airframes arrive with platform setup and import.
  const platforms: never[] = [];
</script>

<main>
  <section class="fleet" aria-labelledby="fleet-title">
    <div class="heading">
      <h1 id="fleet-title">{t("fleet.title")}</h1>
      <p>{t("fleet.summary", { platforms: 0, airframes: 0, attention: 0 })}</p>
    </div>
    <div role="group" aria-label={t("fleet.filter.label")} class="filters">
      {#each filters as [id, label] (id)}
        <button type="button" aria-pressed={filter === id} onclick={() => (filter = id)}>{t(label)}</button>
      {/each}
    </div>
    {#if platforms.length === 0}
      <div class="empty">
        <h2>{t("fleet.empty.title")}</h2>
        <p>{t("fleet.empty.body")}</p>
        <button type="button" class="primary" disabled title={t("fleet.empty.later")}>
          {t("fleet.empty.action")}
        </button>
      </div>
    {/if}
  </section>

  <aside>
    <section class="card" aria-labelledby="laptop-title">
      <h2 id="laptop-title">{t("laptop.title")}</h2>
      <dl>
        <dt>{t("laptop.code")}</dt>
        <dd class="mono">{app.shell.laptop ?? "—"}</dd>
        <dt>{t("laptop.engine")}</dt>
        <dd>
          {#if app.shell.engine === "ready"}
            <StatusBadge status="ok" label={t("engine.ready")} />
          {:else if app.shell.engine === "failed"}
            <StatusBadge status="out" label={t("engine.failed")} />
          {:else if app.shell.engine === "browser"}
            <StatusBadge status="info" label={t("engine.browser")} />
          {:else}
            <StatusBadge status="caution" label={t("engine.starting")} />
          {/if}
        </dd>
        {#if app.shell.engine_version}
          <dt>{t("laptop.version")}</dt>
          <dd class="mono">{app.shell.engine_version}</dd>
        {/if}
      </dl>
      {#if app.shell.first_start}
        <p class="note">{t("laptop.firstStart")}</p>
      {/if}
      {#if app.shell.error}
        <p class="error"><strong>{t("laptop.error")}:</strong> {app.shell.error}</p>
      {/if}
    </section>
    <section class="card" aria-labelledby="attention-title">
      <div class="row">
        <h2 id="attention-title">{t("attention.title")}</h2>
        <span class="faint">{t("attention.order")}</span>
      </div>
      <p class="faint">{t("attention.none")}</p>
    </section>
  </aside>
</main>

<style>
  main {
    display: flex;
    flex-wrap: wrap;
    gap: 24px;
    padding: 24px;
    align-items: flex-start;
  }
  .fleet {
    flex: 999 1 620px;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .heading {
    display: flex;
    align-items: baseline;
    gap: 14px;
    flex-wrap: wrap;
  }
  h1 {
    margin: 0;
    font-size: 26px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }
  .heading p {
    margin: 0;
    font-size: 13px;
    color: var(--text-muted);
  }
  .filters {
    display: flex;
    flex-wrap: wrap;
    gap: 2px;
    padding: 3px;
    background: var(--surface-2);
    border: 1px solid var(--border-soft);
    border-radius: 9px;
    align-self: flex-start;
  }
  .filters button {
    padding: 6px 12px;
    border: 0;
    border-radius: 7px;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    color: var(--text-muted);
    background: transparent;
  }
  .filters button[aria-pressed="true"] {
    color: var(--text);
    background: var(--raised);
  }
  .empty,
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .empty {
    padding: 28px;
    max-width: 560px;
  }
  h2 {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
  }
  .empty p {
    margin: 0;
    color: var(--text-2);
  }
  .primary {
    align-self: flex-start;
    padding: 9px 14px;
    border: 0;
    border-radius: 8px;
    background: var(--accent);
    color: var(--accent-text);
    font-size: 13px;
    font-weight: 600;
  }
  .primary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  aside {
    flex: 1 1 320px;
    max-width: 400px;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  dl {
    margin: 0;
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 6px 14px;
    font-size: 13px;
  }
  dt {
    color: var(--text-faint);
  }
  dd {
    margin: 0;
  }
  .row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px;
  }
  .faint,
  .note {
    margin: 0;
    font-size: 12px;
    color: var(--text-faint);
  }
  .error {
    margin: 0;
    font-size: 12px;
    color: var(--out);
    white-space: pre-wrap;
  }
</style>
