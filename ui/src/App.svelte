<script lang="ts">
  import { onMount } from "svelte";
  import ConfigReview from "./components/ConfigReview.svelte";
  import FleetView from "./components/FleetView.svelte";
  import FlightReport from "./components/FlightReport.svelte";
  import FlightsView from "./components/FlightsView.svelte";
  import ImportDialog from "./components/ImportDialog.svelte";
  import Header from "./components/Header.svelte";
  import PlanView from "./components/PlanView.svelte";
  import Rail from "./components/Rail.svelte";
  import SettingsView from "./components/SettingsView.svelte";
  import { aiSettingsSet, aiStatus, onAiProgress } from "./lib/ai";
  import SetupView from "./components/SetupView.svelte";
  import StationsView from "./components/StationsView.svelte";
  import TrendsView from "./components/TrendsView.svelte";
  import type { PlatformView, SetupSchema } from "./lib/setup";
  import { app, t, type View } from "./lib/app.svelte";
  import type { MessageKey } from "./lib/i18n";
  import { engineCall, onPendingImports, onShellStatus, pendingImports, shellStatus } from "./lib/shell";
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
    let stopAi = () => {};
    onAiProgress(() => refreshAi()).then((unlisten) => (stopAi = unlisten));
    let stopPending = () => {};
    pendingImports().then((p) => (app.pending = p));
    onPendingImports((p) => (app.pending = p)).then((unlisten) => (stopPending = unlisten));
    return () => {
      stop();
      stopAi();
      stopPending();
    };
  });

  // The AI layer's status, once the engine is up: for the header pill and the report.
  function refreshAi() {
    aiStatus()
      .then((s) => (app.ai = s))
      .catch(() => (app.ai = null));
  }
  $effect(() => {
    if (app.shell.engine === "ready") refreshAi();
  });
  // Replies are written in the interface's language.
  $effect(() => {
    const ai = app.ai;
    if (ai && ai.language !== app.locale) {
      aiSettingsSet(ai.monthly_cap_usd, app.locale).then(refreshAi, () => {});
    }
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
    stations: "nav.stations",
    imports: "nav.imports",
    settings: "nav.settings",
  };
</script>

{#if app.importOpen}
  <ImportDialog />
{/if}

<div class="frame">
  <Rail />
  <div class="column">
    <Header onTheme={toggleTheme} />
    {#if app.view === "fleet" && setup}
      <SetupView schema={setup.schema} platform={setup.platform} />
    {:else if app.view === "fleet"}
      <FleetView />
    {:else if app.view === "flights" && app.scope.flight}
      <FlightReport flightId={app.scope.flight} />
    {:else if app.view === "flights"}
      <FlightsView />
    {:else if app.view === "trends"}
      <TrendsView />
    {:else if app.view === "plan"}
      <PlanView />
    {:else if app.view === "params"}
      <ConfigReview />
    {:else if app.view === "stations"}
      <StationsView />
    {:else if app.view === "settings"}
      <SettingsView />
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
