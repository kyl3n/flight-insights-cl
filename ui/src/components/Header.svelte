<script lang="ts">
  import { app, t } from "../lib/app.svelte";
  import { LOCALES, type Locale } from "../lib/i18n";
  import type { Status } from "../lib/status";
  import { STATUS } from "../lib/status";

  let { onTheme }: { onTheme: () => void } = $props();

  const engine = $derived.by((): { status: Status; text: string } => {
    switch (app.shell.engine) {
      case "ready":
        return { status: "ok", text: t("engine.ready") };
      case "failed":
        return { status: "out", text: t("engine.failed") };
      case "browser":
        return { status: "info", text: t("engine.browser") };
      default:
        return { status: "caution", text: t("engine.starting") };
    }
  });
</script>

<header>
  <nav aria-label={t("scope.label")} class="scope">
    <button type="button" class="pick" disabled>{t("scope.allPlatforms")}</button>
  </nav>
  <button type="button" class="search" disabled>
    <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.8" stroke-linecap="round"><circle cx="11" cy="11" r="6" /><path d="M20 20l-4.5-4.5" /></svg>
    <span>{t("search.placeholder")}</span>
    <kbd class="mono">Ctrl K</kbd>
  </button>
  <div class="right">
    <span class="pill" title={app.shell.error ?? undefined} style:--tone="var({STATUS[engine.status].tone})">
      <span aria-hidden="true">{STATUS[engine.status].glyph}</span>{engine.text}
    </span>
    {#if app.shell.laptop}
      <span class="pill mono">{app.shell.laptop}</span>
    {/if}
    <label class="lang">
      <span class="visually-hidden">{t("header.language")}</span>
      <select bind:value={app.locale}>
        {#each LOCALES as locale (locale)}
          <option value={locale as Locale}>{locale.toUpperCase()}</option>
        {/each}
      </select>
    </label>
    <button type="button" class="ghost" onclick={onTheme}>
      {app.theme === "dark" ? t("header.theme.light") : t("header.theme.dark")}
    </button>
    <button type="button" class="primary" onclick={() => (app.importOpen = true)}>
      {t("header.import")}
    </button>
  </div>
</header>

<style>
  header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px 16px;
    padding: 10px 24px;
    min-height: 60px;
    box-sizing: border-box;
    border-bottom: 1px solid var(--border-soft);
  }
  .pick,
  .search,
  .ghost,
  select {
    padding: 6px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface);
    font-size: 13px;
    color: var(--text);
  }
  .search {
    flex: 1 1 240px;
    max-width: 440px;
    display: flex;
    align-items: center;
    gap: 8px;
    background: var(--surface-2);
    color: var(--text-faint);
    text-align: left;
  }
  .search span {
    flex: 1 1 auto;
  }
  kbd {
    font-size: 11px;
    padding: 2px 6px;
    border: 1px solid var(--border);
    border-radius: 5px;
  }
  .right {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .pill {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 5px 10px;
    border-radius: 999px;
    background: var(--surface);
    border: 1px solid var(--border-soft);
    font-size: 12px;
    color: var(--text-muted);
  }
  .pill > span[aria-hidden] {
    color: var(--tone);
    font-size: 9px;
  }
  .ghost {
    cursor: pointer;
  }
  .primary {
    padding: 9px 14px;
    border: 0;
    border-radius: 8px;
    background: var(--accent);
    color: var(--accent-text);
    font-size: 13px;
    font-weight: 600;
  }
  button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
  }
</style>
