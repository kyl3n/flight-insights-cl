<script lang="ts">
  import { onMount } from "svelte";
  import FleetView from "./components/FleetView.svelte";
  import Header from "./components/Header.svelte";
  import Rail from "./components/Rail.svelte";
  import SetupView from "./components/SetupView.svelte";
  import type { PlatformView, SetupSchema } from "./lib/setup";
  import { app, t, type View } from "./lib/app.svelte";
  import type { MessageKey } from "./lib/i18n";
  import { engineCall, onShellStatus, shellStatus } from "./lib/shell";
  import { applyTheme, loadTheme, saveTheme } from "./lib/theme";

  const storage = (() => {
    try {
      return globalThis.localStorage;
    } catch {
      return undefined;
    }
  })();

  app.theme = loadTheme(storage);

  $effect(() => {
    applyTheme(document.documentElement, app.theme);
    document.documentElement.lang = app.locale;
  });

  function toggleTheme() {
    app.theme = app.theme === "dark" ? "light" : "dark";
    saveTheme(storage, app.theme);
  }

  onMount(() => {
    let stop = () => {};
    // The shell starts the engine in the background: ask now, and listen for the result.
    shellStatus().then((s) => (app.shell = s));
    onShellStatus((s) => (app.shell = s)).then((unlisten) => (stop = unlisten));
    return () => stop();
  });

  // The setup wizard needs the schema and the platform; both come from the engine.
  let setup: { schema: SetupSchema; platform: PlatformView } | null = $state(null);
  $effect(() => {
    const id = app.setupPlatform;
    if (id === null) {
      setup = null;
      return;
    }
    Promise.all([
      engineCall<SetupSchema>("setup.schema"),
      engineCall<PlatformView>("platform.get", { id }),
    ]).then(([schema, platform]) => (setup = { schema, platform }));
  });

  const viewNames: Record<View, MessageKey> = {
    fleet: "nav.fleet",
    flights: "nav.flights",
    trends: "nav.trends",
    params: "nav.params",
    plan: "nav.plan",
    ask: "nav.ask",
    imports: "nav.imports",
    settings: "nav.settings",
  };
</script>

<div class="frame">
  <Rail />
  <div class="column">
    <Header onTheme={toggleTheme} />
    {#if app.view === "fleet" && setup}
      <SetupView schema={setup.schema} platform={setup.platform} />
    {:else if app.view === "fleet"}
      <FleetView />
    {:else}
      <main class="later">
        <p>{t("view.later", { view: t(viewNames[app.view]) })}</p>
      </main>
    {/if}
  </div>
</div>

<style>
  .frame {
    min-height: 100vh;
    display: flex;
    flex-wrap: wrap;
  }
  .column {
    flex: 999 1 560px;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .later {
    padding: 24px;
    color: var(--text-muted);
  }
</style>
