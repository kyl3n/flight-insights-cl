<script lang="ts">
  // The fleet overview (spec: User interface, "Fleet"; mock-up Main): every platform
  // and its airframes, each with its status, tuning stage, key metrics and last
  // flight, most urgent first; and what needs attention across the fleet.
  import { app, t } from "../lib/app.svelte";
  import { getRegistry } from "../lib/data";
  import {
    byUrgency,
    findingCount,
    keep,
    stagePosition,
    statusLook,
    type AirframeHealth,
    type Attention,
    type FleetOverview,
    type Filter,
  } from "../lib/fleet";
  import type { MessageKey } from "../lib/i18n";
  import {
    findingTitle,
    formatDuration,
    formatNumber,
    formatTakeoff,
    formatValue,
    severityLook,
    stageKey,
    subsystem,
    subsystemName,
    type Registry,
  } from "../lib/report";
  import { engineCall } from "../lib/shell";
  import type { AirframeView, PlatformView } from "../lib/setup";
  import { cardKey } from "../lib/trends";
  import StatusBadge from "./StatusBadge.svelte";
  import Value from "./Value.svelte";

  let filter: Filter = $state("all");
  const filters: [Filter, MessageKey][] = [
    ["all", "fleet.filter.all"],
    ["attention", "fleet.filter.attention"],
    ["tuning", "fleet.filter.tuning"],
    ["healthy", "fleet.filter.healthy"],
  ];

  let platforms: PlatformView[] = $state([]);
  let airframes: AirframeView[] = $state([]);
  let fleet = $state<FleetOverview>({ airframes: {}, attention: [] });
  let registry = $state<Registry | null>(null);
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
      [fleet, registry] = await Promise.all([engineCall<FleetOverview>("fleet.overview"), getRegistry()]);
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

  function openFlight(airframeId: string, flight: string | null) {
    app.scope.airframe = airframeId;
    app.scope.flight = flight;
    app.view = "flights";
  }

  function openTrends(airframeId: string, card: string | null = null) {
    app.scope.airframe = airframeId;
    app.trendCard = card;
    app.view = "trends";
  }

  function openItem(item: Attention) {
    if (item.kind === "drift") openTrends(item.airframe_id, cardKey(item));
    else openFlight(item.airframe_id, item.flight);
  }

  function lastText(h: AirframeHealth): string {
    const when = formatTakeoff(h.last?.utc_us ?? null, app.locale) ?? "–";
    const d = formatDuration(h.last?.duration_s ?? null);
    return d ? t("fleet.last", { when, min: d.min }) : when;
  }

  function itemText(item: Attention): string {
    if (!registry) return "";
    if (item.kind === "finding") return findingTitle(registry, item, app.locale);
    const metric = registry.metrics[item.metric];
    return t("attention.drift", {
      metric: metric?.name[app.locale] ?? item.metric,
      low: item.baseline ? formatNumber(Math.round(item.baseline.low * 10) / 10, app.locale) : "–",
      high: item.baseline ? formatValue(Math.round(item.baseline.high * 10) / 10, item.unit, app.locale) : "–",
    });
  }

  const itemArea = (item: Attention) =>
    subsystemName(item.kind === "drift" ? subsystem(item.metric) : item.analyzer, app.locale);

  const needing = $derived(airframes.filter((a) => keep("attention", fleet.airframes[a.id])).length);
  const shownAirframes = (platformId: string) =>
    byUrgency(
      airframes.filter((a) => a.platform.id === platformId && keep(filter, fleet.airframes[a.id])),
      fleet.airframes,
    );
  const shown = $derived(platforms.filter((p) => filter === "all" || shownAirframes(p.id).length > 0));
</script>

<main>
  <section class="fleet" aria-labelledby="fleet-title">
    <div class="heading">
      <h1 id="fleet-title">{t("fleet.title")}</h1>
      <p>{t("fleet.summary", { platforms: platforms.length, airframes: airframes.length, attention: needing })}</p>
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
            {#each shownAirframes(p.id) as a (a.id)}
              {@const h = fleet.airframes[a.id]}
              {@const look = statusLook(h?.status ?? "none")}
              <article class="airframe">
                <div class="row">
                  <button type="button" class="name mono plain" onclick={() => openFlight(a.id, h?.last?.flight ?? null)}>
                    {a.name}
                  </button>
                  <StatusBadge status={look.status} label={t(look.label)} />
                </div>
                <span class="faint">
                  {t("fleet.airframe.build", { number: a.build.number })}{#if a.differs_from_reference.length}
                    · {t("fleet.airframe.differs", { count: a.differs_from_reference.length })}{/if}
                </span>
                {#if h?.stage}
                  {@const pos = stagePosition(h.stage)}
                  <span class="stage">
                    {pos ? t("fleet.stage", { ...pos, stage: t(stageKey(h.stage)) }) : t(stageKey(h.stage))}
                  </span>
                {:else if h?.planned}
                  <span class="stage done">{t("fleet.stage.clear")}</span>
                {/if}
                {#if h?.metrics?.length && registry}
                  <div class="metrics">
                    {#each h.metrics as m (cardKey(m))}
                      {@const info = registry.metrics[m.metric]}
                      {#if info}
                        <span class="metric">
                          <Value id={m.metric} metric={info} value={m.value} zone={m.zone} context={t(`segment.${m.segment}` as MessageKey)} />
                          {#if m.drift}<span class="drift">▲ {t("fleet.drift")}</span>{/if}
                        </span>
                      {/if}
                    {/each}
                  </div>
                {/if}
                {#if h?.last}
                  <div class="row foot">
                    <span class="faint">{lastText(h)}</span>
                    <span class="faint">
                      {findingCount(h) === 0
                        ? t("fleet.findings.none")
                        : findingCount(h) === 1
                          ? t("fleet.findings.one")
                          : t("fleet.findings", { count: findingCount(h) })}
                    </span>
                  </div>
                  {#if h.current === false}<span class="faint">{t("fleet.stale")}</span>{/if}
                  <span class="links">
                    <button type="button" class="link" onclick={() => openTrends(a.id)}>{t("fleet.trends")}</button>
                    <button type="button" class="link" onclick={() => (app.recoveryCard = a.id)}>{t("fleet.recovery")}</button>
                  </span>
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
        {#if app.shell.engine !== "browser"}
          <dt>{t("laptop.ai")}</dt>
          <dd>
            {#if app.shell.ai === "ready"}
              <StatusBadge status="ok" label={t("ai.ready")} />
            {:else if app.shell.ai === "blocked"}
              <StatusBadge status="out" label={t("ai.blocked")} />
            {:else}
              <StatusBadge status="caution" label={t("ai.pending")} />
            {/if}
          </dd>
        {/if}
      </dl>
      {#if app.shell.ai === "blocked" && app.shell.ai_failed.length}
        <p class="error"><strong>{t("ai.failed")}:</strong> {app.shell.ai_failed.join("; ")}</p>
      {/if}
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
      {#each fleet.attention as item, i (i)}
        {@const look = severityLook(item.severity)}
        <button type="button" class="item" onclick={() => openItem(item)}>
          <span class="row">
            <StatusBadge status={look.status} label={t(look.label)} />
            <span class="mono who">{item.airframe}</span>
          </span>
          <span class="text">{itemText(item)}</span>
          <span class="faint">{formatTakeoff(item.utc_us, app.locale) ?? "–"} · {itemArea(item)}</span>
        </button>
      {:else}
        <p class="faint">{t("attention.none")}</p>
      {/each}
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
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
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
  .plain {
    background: none;
    border: 0;
    padding: 0;
    color: var(--text);
    cursor: pointer;
    text-align: left;
  }
  .plain:hover {
    color: var(--link);
  }
  .airframe {
    gap: 8px;
  }
  .stage {
    font-size: 12px;
    color: var(--accent);
  }
  .stage.done {
    color: var(--text-faint);
  }
  .metrics {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .metric {
    display: inline-flex;
    flex-direction: column;
    gap: 2px;
  }
  .drift {
    font-size: 11px;
    color: var(--caution);
  }
  .foot {
    font-size: 12px;
  }
  .item {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 10px 0;
    border: 0;
    border-top: 1px solid var(--border-soft);
    background: none;
    color: var(--text);
    text-align: left;
    cursor: pointer;
  }
  .item:hover .text {
    color: var(--link);
  }
  .item .row {
    justify-content: flex-start;
    align-items: center;
  }
  .who {
    font-size: 13px;
  }
  .text {
    font-size: 13px;
    line-height: 1.4;
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
  .links {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 14px;
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
