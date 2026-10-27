<script lang="ts">
  // The Navigation and GNSS tab of one flight (spec: User interface, "The Navigation and
  // GNSS view"; mock-up Navigation): the fleet goal, GNSS events with their class and
  // evidence, outages and drills, how the aircraft coped with the one picked, which
  // evidence each receiver logged, the readiness to fly without GNSS, the setup findings
  // that would get it home, and the airframe's recovery card in short. Every status and
  // verdict is the engine's (flight.navigation, airframe.recovery_card); times are
  // seconds after arming and distances relative metres.
  import { app, t } from "../lib/app.svelte";
  import { getRegistry } from "../lib/data";
  import type { MessageKey } from "../lib/i18n";
  import {
    AREA_LOOK,
    CHANNELS,
    actionSteps,
    areaText,
    copingText,
    eventFacts,
    firstPick,
    goalLook,
    picked,
    receiversLabel,
    rowLabel,
    span,
    verdictWord,
    type CopingRow,
    type Judged,
    type NavEvent,
    type NavigationView,
    type Outage,
    type Pick,
  } from "../lib/navigation";
  import { recoveryCard, type RecoveryCard } from "../lib/recovery";
  import { findingAction, findingTitle, severityLook, type Registry } from "../lib/report";
  import { engineCall } from "../lib/shell";
  import { STATUS, type Status } from "../lib/status";
  import ProcedureSteps from "./ProcedureSteps.svelte";
  import StatusBadge from "./StatusBadge.svelte";

  // `run` is the flight's analysis, once the report has it: the view lays that out.
  let { flightId, run }: { flightId: string; run: string | null } = $props();

  let view = $state<NavigationView | null>(null);
  let reg = $state<Registry | null>(null);
  let card = $state<RecoveryCard | null>(null);
  let cardFailed = $state(false);
  let error = $state("");
  let pick = $state<Pick | null>(null);

  $effect(() => {
    void flightId;
    view = null;
    pick = null;
  });
  $effect(() => {
    const id = flightId;
    if (!run) return;
    error = "";
    engineCall<NavigationView>("flight.navigation", { flight_id: id })
      .then((v) => {
        view = v;
        pick ??= firstPick(v);
      })
      .catch((e: unknown) => (error = String(e)));
  });
  $effect(() => {
    getRegistry().then((r) => (reg = r)).catch(() => {});
  });
  const airframe = $derived(view?.airframe_id ?? null);
  $effect(() => {
    const id = airframe;
    card = null;
    cardFailed = false;
    if (!id) return;
    recoveryCard(id).then(
      (c) => (card = c),
      () => (cardFailed = true),
    );
  });

  const chosen = $derived(view ? picked(view, pick) : null);
  const lost = $derived(card?.sections.find((s) => s.id === "gnss_lost") ?? null);
  const spoof = $derived(card?.sections.find((s) => s.id === "spoofing") ?? null);
  const goalLine = $derived(card?.goal?.will.at(-1) ?? null);

  const isEvent = (x: NavEvent | Outage): x is NavEvent => "class" in x;
  const className = (e: NavEvent) => t(`nav.class.${e.class}` as MessageKey);
  const outageName = (o: Outage) => t(o.drill ? "nav.outage.drill" : "nav.outage.outage");
  const judged = (r: CopingRow) => (r.values.judged ?? []) as Judged[];
  const tone = (s: Status) => `var(${STATUS[s].tone})`;

  function subtitle(x: NavEvent | Outage): string {
    const time = span(x.start_s, x.end_s, app.locale);
    return isEvent(x)
      ? t("nav.coped.event", { kind: className(x), time })
      : t("nav.coped.outage", { kind: outageName(x), time });
  }
</script>

{#if error}<p class="error">{error}</p>{/if}
{#if !view && !error}<p class="faint">{t("report.analysing")}</p>{/if}

{#if view && reg}
  {@const v = view}
  {@const registry = reg}
  <p class="faint about">{t("nav.about")}</p>
  <div class="layout">
    <div class="main">
      {#if card?.goal}
        {@const met = card.goal.met}
        {@const look = goalLook(met)}
        <section class="panel goal" class:met aria-labelledby="nav-goal">
          <div class="row">
            <h2 id="nav-goal"><span class="eyebrow">{t("nav.goal.label")}</span> {t("nav.goal.title")}</h2>
            <span class="verdict" style:color={tone(look.status)}>
              {STATUS[look.status].glyph} {t(met ? "nav.goal.ready" : "nav.goal.notReady", { airframe: card.airframe })}
            </span>
          </div>
          <ul class="plain">
            {#each card.goal.will as p, i (i)}<li>{card.will_label[app.locale]} {p.text[app.locale]}</li>{/each}
          </ul>
          <p class="faint">{t("nav.goal.from")}</p>
        </section>
      {/if}

      <section class="panel" aria-labelledby="nav-events">
        <div class="head">
          <h2 id="nav-events">{t("nav.events.title")}</h2>
          <span class="faint">{t("nav.events.about")}</span>
        </div>
        {#if v.skipped.gnss}
          <p class="faint">{t("nav.skipped", { reason: v.skipped.gnss })}</p>
        {:else if !v.events.length}
          <p class="faint">{t("nav.events.none")}</p>
        {/if}
        {#if v.events.length}
          <ul class="items">
            {#each v.events as e (e.n)}
              {@const look = severityLook(e.finding?.severity ?? "info")}
              {@const facts = eventFacts(e, app.locale)}
              <li>
                <button
                  type="button"
                  class="item"
                  aria-pressed={pick?.kind === "event" && pick.n === e.n}
                  onclick={() => (pick = { kind: "event", n: e.n })}
                >
                  <span class="item-head">
                    <span class="kind" style:color={tone(look.status)}>{STATUS[look.status].glyph} {className(e)}</span>
                    <span class="faint">
                      {t("nav.event.meta", {
                        confidence: t(`confidence.${e.confidence}` as MessageKey),
                        receivers: receiversLabel(e.receivers, app.locale),
                      })}
                    </span>
                    <span class="when mono">{span(e.start_s, e.end_s, app.locale)}</span>
                  </span>
                  {#if e.finding}<span class="text">{findingTitle(registry, e.finding, app.locale)}</span>{/if}
                  {#if facts.length}<span class="faint">{facts.join(" · ")}</span>{/if}
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      </section>

      <section class="panel" aria-labelledby="nav-outages">
        <div class="head">
          <h2 id="nav-outages">{t("nav.outages.title")}</h2>
          <span class="faint">{t("nav.outages.about")}</span>
        </div>
        {#if v.skipped.no_gnss}
          <p class="faint">{t("nav.skipped", { reason: v.skipped.no_gnss })}</p>
        {:else if !v.outages.length}
          <p class="faint">{t("nav.outages.none")}</p>
        {/if}
        {#if v.outages.length}
          <ul class="items">
            {#each v.outages as o (o.n)}
              {@const look = severityLook(o.finding?.severity ?? "info")}
              <li>
                <button
                  type="button"
                  class="item"
                  aria-pressed={pick?.kind === "outage" && pick.n === o.n}
                  onclick={() => (pick = { kind: "outage", n: o.n })}
                >
                  <span class="item-head">
                    <span class="kind" style:color={tone(look.status)}>{STATUS[look.status].glyph} {outageName(o)}</span>
                    {#if o.events.length}<span class="faint">{t("nav.outage.inEvent", { events: o.events.join(", ") })}</span>{/if}
                    <span class="when mono">{span(o.start_s, o.end_s, app.locale)}</span>
                  </span>
                  {#if o.finding}<span class="text">{findingTitle(registry, o.finding, app.locale)}</span>{/if}
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      </section>

      <section class="panel" aria-labelledby="nav-coped">
        <div class="head">
          <h2 id="nav-coped">{t("nav.coped.title")}</h2>
          {#if chosen}<span class="faint">{subtitle(chosen)}</span>{/if}
        </div>
        {#if !chosen}
          <p class="faint">{v.events.length || v.outages.length ? t("nav.coped.pick") : t("nav.coped.none")}</p>
        {:else}
          <div class="scroll">
            <table class="rows stack">
              <thead>
                <tr><th scope="col"></th><th scope="col">{t("nav.coped.what")}</th><th scope="col"></th></tr>
              </thead>
              <tbody>
                {#each chosen.coping as r (r.id)}
                  <tr>
                    <th scope="row">{rowLabel(r, app.locale)}</th>
                    <td>
                      {#if r.verdict === "judged"}
                        <ul class="plain">
                          {#each judged(r) as j (j.finding.id)}<li>{findingTitle(registry, j.finding, app.locale)}</li>{/each}
                          {#if Array.isArray(r.values.names) && r.values.names.length}
                            <li class="faint">{t("nav.cope.failsafe.named", { names: r.values.names.join(", ") })}</li>
                          {/if}
                        </ul>
                      {:else}
                        {copingText(r, app.locale)}
                      {/if}
                    </td>
                    <td class="status" style:color={tone(r.status)}>{STATUS[r.status].glyph} {verdictWord(r, app.locale)}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
          {#if isEvent(chosen) && chosen.heading_finding}
            <p class="notice">■ {findingTitle(registry, chosen.heading_finding, app.locale)}</p>
          {/if}
          {#if chosen.finding}
            <details>
              <summary>{t("nav.coped.todo")}</summary>
              <p>{findingAction(registry, chosen.finding, app.locale)}</p>
            </details>
          {/if}
        {/if}
      </section>

      <section class="panel" aria-labelledby="nav-receivers">
        <div class="head">
          <h2 id="nav-receivers">{t("nav.receivers.title")}</h2>
          <span class="faint">{t("nav.receivers.about")}</span>
        </div>
        {#if !v.receivers.length}
          <p class="faint">{t("nav.receivers.empty")}</p>
        {:else}
          <div class="scroll">
            <table class="rows evidence">
              <thead>
                <tr>
                  <th scope="col">{t("nav.receivers.col.evidence")}</th>
                  {#each v.receivers as r (r.instance)}
                    <th scope="col">
                      {t("nav.receivers.gps", { n: r.instance })}
                      {#if r.type}<span class="faint mono type">{r.param} {r.type}</span>{/if}
                    </th>
                  {/each}
                  <th scope="col">{t("nav.receivers.col.independent")}</th>
                </tr>
              </thead>
              <tbody>
                {#each CHANNELS as c (c)}
                  <tr>
                    <th scope="row">{t(`nav.receivers.row.${c}` as MessageKey)}</th>
                    {#each v.receivers as r (r.instance)}<td>{@render mark(r.logged[c], "nav.logged", "nav.notLogged")}</td>{/each}
                    <td></td>
                  </tr>
                {/each}
                {#each [["estimator", v.independent.estimator, "nav.logged", "nav.notLogged"], ["barometer", v.independent.barometer, "nav.logged", "nav.notLogged"], ["second", v.independent.second_receiver, "nav.fitted", "nav.notFitted"]] as const as [name, ok, yes, no] (name)}
                  <tr>
                    <th scope="row">{t(`nav.receivers.row.${name}`)}</th>
                    {#each v.receivers as r (r.instance)}<td></td>{/each}
                    <td>{@render mark(ok, yes, no)}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
          <p class="faint">{t("nav.receivers.note")}</p>
        {/if}
      </section>

      <section class="panel" aria-labelledby="nav-ready">
        <div class="head">
          <h2 id="nav-ready">{t("nav.ready.title")}</h2>
          <span class="faint">{t("nav.ready.about")}</span>
        </div>
        {#if v.skipped.no_gnss_readiness}
          <p class="faint">{t("nav.skipped", { reason: v.skipped.no_gnss_readiness })}</p>
        {/if}
        {#if v.readiness.length || card?.goal}
          <div class="scroll">
            <table class="rows stack">
              <thead>
                <tr><th scope="col">{t("nav.ready.col.area")}</th><th scope="col">{t("nav.ready.col.now")}</th><th scope="col"></th></tr>
              </thead>
              <tbody>
                {#each v.readiness as a (a.area)}
                  {@const look = AREA_LOOK[a.status]}
                  <tr>
                    <th scope="row">{t(`nav.areaName.${a.area}` as MessageKey)}</th>
                    <td>{areaText(a, app.locale)}</td>
                    <td class="status" style:color={tone(look.status)}>{STATUS[look.status].glyph} {t(look.label)}</td>
                  </tr>
                {/each}
                {#if card?.goal && goalLine}
                  {@const look = goalLook(card.goal.met)}
                  <tr>
                    <th scope="row">{t("nav.areaName.goal")}</th>
                    <td>{goalLine.text[app.locale]}</td>
                    <td class="status" style:color={tone(look.status)}>{STATUS[look.status].glyph} {t(look.label)}</td>
                  </tr>
                {/if}
              </tbody>
            </table>
          </div>
          {#if card?.goal}<p class="faint">{t("nav.ready.note")}</p>{/if}
        {/if}
      </section>

      <section class="panel" aria-labelledby="nav-home">
        <div class="head">
          <h2 id="nav-home">{t("nav.home.title")}</h2>
          <span class="faint">{t("nav.home.about")}</span>
        </div>
        {#if !v.actions.length}
          <p class="faint">{t("nav.home.none")}</p>
        {:else}
          <ol class="actions">
            {#each v.actions as a, i (a.finding.id)}
              {@const look = severityLook(a.finding.severity)}
              <li class="action">
                <div class="action-head">
                  <span class="num mono">{i + 1}</span>
                  <strong>{findingTitle(registry, a.finding, app.locale)}</strong>
                  <StatusBadge status={look.status} label={t(look.label)} />
                </div>
                <p>{findingAction(registry, a.finding, app.locale)}</p>
                {#if a.steps.length}<ProcedureSteps steps={actionSteps(a)} />{/if}
              </li>
            {/each}
          </ol>
          <p class="faint">{t("nav.home.plan")}</p>
        {/if}
        <p class="faint">{t("nav.home.catalogue")}</p>
        {#if v.sources.length}
          <details>
            <summary>{t("nav.home.sources", { count: v.sources.length })}</summary>
            <ul class="sources">{#each v.sources as s, i (i)}<li>{s}</li>{/each}</ul>
          </details>
        {/if}
      </section>
    </div>

    <aside class="panel summary" aria-labelledby="nav-card">
      {#if card}
        <span class="eyebrow accent">{t("nav.card.label", { airframe: card.airframe })}</span>
        <h2 id="nav-card">{t("nav.card.title")}</h2>
        <dl>
          {#if lost?.will.length}
            <dt>{card.will_label[app.locale]}</dt>
            <dd>{#each lost.will as p, i (i)}<p>{p.text[app.locale]}</p>{/each}</dd>
          {/if}
          {#if lost?.facts.length}
            <dt>{t("recovery.measured")}</dt>
            <dd><ul class="plain">{#each lost.facts as p, i (i)}<li>{p.text[app.locale]}</li>{/each}</ul></dd>
          {/if}
          {#if goalLine}
            <dt>{t("nav.card.linkToo")}</dt>
            <dd><p>{goalLine.text[app.locale]}</p></dd>
          {/if}
        </dl>
        {#if lost?.steps.length}
          <ol class="steps">{#each lost.steps as p, i (i)}<li>{p.text[app.locale]}</li>{/each}</ol>
        {/if}
        {#if spoof}
          <div class="spoof">
            <strong>▲ {spoof.title[app.locale]}</strong>
            <ol class="steps">{#each spoof.steps as p, i (i)}<li>{p.text[app.locale]}</li>{/each}</ol>
          </div>
        {/if}
        <button type="button" class="primary" onclick={() => (app.recoveryCard = v.airframe_id)}>{t("nav.card.print")}</button>
        <p class="faint">{t("nav.card.about")}</p>
      {:else if cardFailed || !v.airframe_id}
        <h2 id="nav-card">{t("fleet.recovery")}</h2>
        <p class="faint">{t("recovery.none")}</p>
      {:else}
        <h2 id="nav-card">{t("fleet.recovery")}</h2>
        <p class="faint">{t("recovery.loading")}</p>
      {/if}
    </aside>
  </div>
{/if}

{#snippet mark(ok: boolean, yes: MessageKey, no: MessageKey)}
  {@const s = ok ? ("ok" as const) : ("info" as const)}
  <span class="status" style:color={tone(s)}>{STATUS[s].glyph} {t(ok ? yes : no)}</span>
{/snippet}

<style>
  .about {
    margin: 0 0 10px;
  }
  .layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(260px, 320px);
    gap: 12px;
    align-items: start;
  }
  @media (max-width: 1000px) {
    .layout {
      grid-template-columns: minmax(0, 1fr);
    }
  }
  .main {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
  }
  .panel {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 14px 16px;
    background: var(--surface);
    border: 1px solid var(--border-soft);
    border-radius: 12px;
    min-width: 0;
  }
  .summary {
    position: sticky;
    top: 12px;
    gap: 10px;
  }
  @media (max-width: 1000px) {
    .summary {
      position: static;
    }
  }
  h2 {
    margin: 0;
    font-size: 14px;
  }
  p {
    margin: 0;
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 10px;
  }
  .row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 6px 12px;
    flex-wrap: wrap;
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
  }
  .error {
    color: var(--out);
  }
  .notice {
    color: var(--out);
    font-size: 13px;
  }
  .mono {
    font-family: var(--mono);
  }
  .eyebrow {
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--text-muted);
    margin-right: 6px;
  }
  .eyebrow.accent {
    color: var(--accent);
    text-transform: none;
    letter-spacing: 0;
    font-size: 12px;
  }
  .goal {
    border-color: color-mix(in srgb, var(--out) 45%, var(--border));
    background: color-mix(in srgb, var(--out) 7%, var(--surface));
  }
  .goal.met {
    border-color: color-mix(in srgb, var(--ok) 45%, var(--border));
    background: color-mix(in srgb, var(--ok) 6%, var(--surface));
  }
  .verdict {
    font-size: 13px;
    font-weight: 600;
  }
  ul.plain {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .goal ul.plain {
    font-size: 13px;
    color: var(--text-2);
  }
  .items {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .item {
    width: 100%;
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 10px 12px;
    text-align: left;
    background: var(--surface-2);
    border: 1px solid var(--border-soft);
    border-radius: 10px;
    color: var(--text);
    cursor: pointer;
    font-size: 13px;
  }
  .item[aria-pressed="true"] {
    border-color: var(--accent);
  }
  .item-head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 10px;
  }
  .kind {
    font-weight: 600;
  }
  .when {
    margin-left: auto;
    font-size: 12px;
    color: var(--text-2);
  }
  .text {
    color: var(--text-2);
  }
  .scroll {
    overflow-x: auto;
  }
  table.rows {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  .rows th,
  .rows td {
    text-align: left;
    padding: 6px 10px 6px 0;
    border-top: 1px solid var(--border-soft);
    vertical-align: top;
  }
  .rows thead th {
    border-top: none;
    font-weight: 500;
    font-size: 12px;
    color: var(--text-muted);
  }
  .rows tbody th {
    font-weight: normal;
    color: var(--text-2);
    min-width: 120px;
  }
  .rows td.status {
    text-align: right;
    white-space: nowrap;
    font-size: 12px;
    font-weight: 600;
    padding-right: 0;
  }
  /* on a phone a row's label and status share a line, and what it says goes below */
  @media (max-width: 600px) {
    .stack thead {
      display: none;
    }
    .stack tr {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 2px 10px;
      padding: 6px 0;
      border-top: 1px solid var(--border-soft);
    }
    .stack tbody th,
    .stack td {
      border-top: none;
      padding: 0;
      min-width: 0;
    }
    .stack td.status {
      grid-row: 1;
      grid-column: 2;
    }
    .stack td:not(.status) {
      grid-column: 1 / -1;
    }
  }
  .evidence td,
  .evidence thead th {
    min-width: 96px;
  }
  .evidence .status {
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
  }
  .type {
    display: block;
  }
  details summary {
    cursor: pointer;
    color: var(--link);
    font-size: 13px;
  }
  details p {
    margin-top: 6px;
    font-size: 13px;
    color: var(--text-2);
  }
  .actions {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .action {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 10px 12px;
    background: var(--surface-2);
    border: 1px solid var(--border-soft);
    border-radius: 10px;
    font-size: 13px;
  }
  .action-head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 6px 10px;
  }
  .action-head strong {
    flex: 1 1 200px;
    font-weight: 600;
  }
  .num {
    color: var(--text-faint);
  }
  .action > p {
    color: var(--text-2);
  }
  .sources {
    margin: 6px 0 0;
    padding-left: 16px;
    font-size: 12px;
    color: var(--text-faint);
    overflow-wrap: anywhere;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  dl {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-size: 13px;
  }
  dt {
    color: var(--text-muted);
    font-size: 12px;
  }
  dt:not(:first-child) {
    margin-top: 6px;
  }
  dd {
    margin: 0;
    color: var(--text);
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .steps {
    margin: 0;
    padding-left: 18px;
    font-size: 13px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .spoof {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 10px 12px;
    border: 1px dashed color-mix(in srgb, var(--caution) 60%, var(--border));
    border-radius: 10px;
    font-size: 13px;
  }
  .spoof strong {
    color: var(--caution);
    font-weight: 600;
  }
  .primary {
    align-self: flex-start;
    background: var(--accent);
    color: var(--accent-text);
    border: none;
    border-radius: 8px;
    padding: 8px 14px;
    font-weight: 600;
    cursor: pointer;
  }
</style>
