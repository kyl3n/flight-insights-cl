<script lang="ts">
  import { app, t, type View } from "../lib/app.svelte";
  import type { MessageKey } from "../lib/i18n";

  const top: [View, MessageKey, string][] = [
    ["fleet", "nav.fleet", "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z"],
    ["flights", "nav.flights", "M6 3h9l4 4v14H6zM14 3v5h5M9 13h7M9 17h5"],
    ["trends", "nav.trends", "M3 17l5-5 4 3 8-8M15 7h5v5"],
    ["params", "nav.params", "M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"],
    ["plan", "nav.plan", "M10 6h10M10 12h10M10 18h10M3.5 6l1.5 1.5L7.5 5M3.5 12l1.5 1.5L7.5 11"],
    ["ask", "nav.ask", "M4 5h16v11H9l-5 4zM8 10h8"],
  ];
  const bottom: [View, MessageKey, string][] = [
    ["stations", "nav.stations", "M12 13v8M8 21h8M8.5 9.5a5 5 0 0 1 7 0M5.5 6.5a9 9 0 0 1 13 0M12 12.5h.01"],
    ["sync", "nav.sync", "M20 12a8 8 0 0 1-14.2 5M4 12a8 8 0 0 1 14.2-5M18 3v4h-4M6 21v-4h4"],
    ["imports", "nav.imports", "M12 3v11M7 9l5 5 5-5M4 17v3h16v-3"],
    ["settings", "nav.settings", "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM12 2v3M12 19v3M2 12h3M19 12h3"],
  ];
</script>

{#snippet item(view: View, label: MessageKey, path: string)}
  <button
    type="button"
    class:current={app.view === view}
    aria-current={app.view === view ? "page" : undefined}
    onclick={() => (app.view = view)}
  >
    <svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
      <path d={path} />
    </svg>
    {t(label)}
  </button>
{/snippet}

<nav class="rail" aria-label={t("nav.main")}>
  <svg role="img" aria-label={t("app.title")} width="36" height="36" viewBox="0 0 36 36" class="logo">
    <rect x="1" y="1" width="34" height="34" rx="9" fill="#13222D" stroke="#2A4558" />
    <path d="M8 25l6-7 5 4 9-12" fill="none" stroke="#5AB0E8" stroke-width="2.2"
      stroke-linecap="round" stroke-linejoin="round" />
    <circle cx="28" cy="10" r="2.4" fill="#5AB0E8" />
  </svg>
  {#each top as [view, label, path] (view)}
    {@render item(view, label, path)}
  {/each}
  <div class="spacer"></div>
  {#each bottom as [view, label, path] (view)}
    {@render item(view, label, path)}
  {/each}
</nav>

<style>
  .rail {
    flex: 0 0 76px;
    width: 76px;
    box-sizing: border-box;
    background: var(--rail);
    border-right: 1px solid var(--border-soft);
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    padding: 14px 0 16px;
  }
  .logo {
    margin-bottom: 14px;
  }
  .spacer {
    flex: 1 1 auto;
  }
  button {
    width: 68px;
    padding: 8px 2px 7px;
    box-sizing: border-box;
    text-align: center;
    overflow-wrap: anywhere;
    hyphens: auto;
    border: 0;
    border-radius: 10px;
    background: transparent;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    font-size: 10.5px;
    line-height: 1.2;
    font-weight: 500;
    color: var(--text-muted);
    cursor: pointer;
  }
  button.current {
    color: var(--link);
    background: color-mix(in srgb, var(--accent) 15%, transparent);
  }
  @media (max-width: 720px) {
    .rail {
      flex: 1 1 100%;
      width: 100%;
      flex-direction: row;
      overflow-x: auto;
      padding: 8px;
    }
    .spacer {
      display: none;
    }
  }
</style>
