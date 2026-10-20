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
  import { engineCall } from "../lib/shell";
  import type { AirframeView, PlatformView } from "../lib/setup";

  let platforms: PlatformView[] = $state([]);
  let airframes: AirframeView[] = $state([]);
  let creating = $state(false);
  let newName = $state("");
  let newVehicle: "copter" | "plane" = $state("copter");
  let newAirframe: Record<string, string> = $state({});
  let error = $state("");
  const ready = $derived(app.shell.engine === "ready");

  async function load() {
    if (!ready) return;
    try {
      platforms = await engineCall<PlatformView[]>("platform.list");
      airframes = await engineCall<AirframeView[]>("airframe.list");
    } catch (e) {
      error = String(e);
    }
  }

  $effect(() => {
    if (ready && app.setupPlatform === null) load();
  });

  async function createPlatform() {
    try {
      const p = await engineCall<PlatformView>("platform.create", { name: newName, vehicle: newVehicle });
      creating = false;
      newName = "";
      app.setupPlatform = p.id;
    } catch (e) {
      error = String(e);
    }
  }

  async function addAirframe(platformId: string) {
    try {
      await engineCall("airframe.create", { platform_id: platformId, name: newAirframe[platformId] ?? "" });
      newAirframe[platformId] = "";
      await load();
    } catch (e) {
      error = String(e);
    }
  }

  const attention = $derived(platforms.filter((p) => !p.completeness.ready).length);
  const shown = $derived(
    platforms.filter((p) =>
      filter === "all" ? true
      : filter === "attention" ? !p.completeness.ready
      : filter === "healthy" ? p.completeness.ready
      : true,
    ),
  );
</script>

<main>
  <section class="fleet" aria-labelledby="fleet-title">
    <div class="heading">
      <h1 id="fleet-title">{t("fleet.title")}</h1>
      <p>{t("fleet.summary", { platforms: platforms.length, airframes: airframes.length, attention })}</p>
    </div>
    <div role="group" aria-label={t("fleet.filter.label")} class="filters">
      {#each filters as [id, label] (id)}
        <button type="button" aria-pressed={filter === id} onclick={() => (filter = id)}>{t(label)}</button>
      {/each}
    </div>
    {#if error}<p class="error">{error}</p>{/if}
    {#if !ready}
      <p class="faint">{t("setup.unavailable")}</p>
    {/if}
    {#if creating}
      <form class="empty" onsubmit={(e) => { e.preventDefault(); createPlatform(); }}>
        <label>{t("setup.create.name")}<input type="text" bind:value={newName} required /></label>
        <label>
          {t("setup.create.vehicle")}
          <select bind:value={newVehicle}>
            <option value="copter">copter</option>
            <option value="plane">plane</option>
          </select>
        </label>
        <button type="submit" class="primary" disabled={!newName.trim()}>{t("setup.create.start")}</button>
      </form>
    {:else if platforms.length === 0}
      <div class="empty">
        <h2>{t("fleet.empty.title")}</h2>
        <p>{t("fleet.empty.body")}</p>
        <button type="button" class="primary" disabled={!ready} onclick={() => (creating = true)}>
          {t("fleet.empty.action")}
        </button>
      </div>
    {:else}
      {#each shown as p (p.id)}
        <section class="platform">
          <div class="row">
            <h2>{p.name}</h2>
            <span class="faint">{p.vehicle} · {t("fleet.platform.airframes", { count: p.airframes })}</span>
            {#if p.completeness.ready}
              <StatusBadge status="ok" label={t("fleet.platform.ready")} />
            {:else}
              <StatusBadge status="caution" label={t("fleet.platform.notReady", { percent: p.completeness.percent })} />
            {/if}
            <button type="button" class="link" onclick={() => (app.setupPlatform = p.id)}>
              {t("fleet.platform.continue")}
            </button>
          </div>
          <div class="cards">
            {#each airframes.filter((a) => a.platform.id === p.id) as a (a.id)}
              <article>
                <span class="mono name">{a.name}</span>
                <span class="faint">{t("fleet.airframe.build", { number: a.build.number })}</span>
                {#if a.differs_from_reference.length}
                  <span class="faint">{t("fleet.airframe.differs", { count: a.differs_from_reference.length })}</span>
                {/if}
              </article>
            {/each}
            <form class="add" onsubmit={(e) => { e.preventDefault(); addAirframe(p.id); }}>
              <input type="text" placeholder={t("fleet.airframe.name")} bind:value={newAirframe[p.id]} />
              <button type="submit" disabled={!(newAirframe[p.id] ?? "").trim()}>{t("fleet.airframe.add")}</button>
            </form>
          </div>
        </section>
      {/each}
      <button type="button" class="link" onclick={() => (creating = true)}>{t("fleet.empty.action")}</button>
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
  .platform {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .platform .row {
    flex-wrap: wrap;
    justify-content: flex-start;
    gap: 12px;
    align-items: center;
  }
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 12px;
  }
  article,
  .add {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 14px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .name {
    font-size: 17px;
  }
  input,
  select {
    padding: 7px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-2);
    color: var(--text);
    font-size: 13px;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 5px;
    font-size: 13px;
  }
  .add button,
  .link {
    align-self: flex-start;
    padding: 6px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: transparent;
    color: var(--link);
    font-size: 13px;
    cursor: pointer;
  }
  .error {
    margin: 0;
    color: var(--out);
    font-size: 13px;
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
