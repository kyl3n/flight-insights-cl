<script lang="ts">
  // One flight's incident timeline (spec: Whole-system incident timeline; Failsafe causes
  // and RF early warning; mock-up Incidents): every source on one time axis in seconds
  // after arming, the first fault that explains the rest, whether each failsafe worked
  // as configured and on time, what warned first, and the near misses. The lanes are the
  // engine's flight.timeline; the verdicts are the analysis's findings, worded by the
  // registry. Selecting an event opens what was read for it beside the timeline.
  import { app, t } from "../lib/app.svelte";
  import { CATALOGS, type MessageKey } from "../lib/i18n";
  import {
    at,
    axis,
    detailKey,
    failsafeChecks,
    formatSeconds,
    incidentFinding,
    incidentText,
    lanes,
    nearMisses,
    placeTags,
    role,
    sameEvent,
    sentence,
    settingPairs,
    timingLook,
    unanswered,
    verdictLook,
    type Detail,
    type FailsafeCheck,
    type LaneId,
    type Mark,
    type Timeline,
    type TimelineEvent,
    type Tone,
  } from "../lib/incidents";
  import { findingAction, findingTitle, formatNumber, severityLook, type Analysis, type Registry } from "../lib/report";
  import { engineCall } from "../lib/shell";
  import { STATUS, type Status } from "../lib/status";
  import StatusBadge from "./StatusBadge.svelte";

  let {
    flightId,
    analysis,
    registry,
  }: { flightId: string; analysis: Analysis | null; registry: Registry | null } = $props();

  interface TlogSession {
    status: string; // its clock: aligned, corrected, unconfirmed, no clock, missing
  }

  let timeline = $state<Timeline | null>(null);
  let sessions = $state<TlogSession[] | null>(null);
  let error = $state("");
  let picked = $state<Detail | null>(null);
  let trackPx = $state(600);
  let scroller = $state<HTMLDivElement | null>(null);

  $effect(() => {
    const id = flightId;
    timeline = null;
    sessions = null;
    error = "";
    picked = null;
    engineCall<Timeline>("flight.timeline", { flight_id: id })
      .then((v) => {
        if (id === flightId) timeline = v;
      })
      .catch((e: unknown) => (error = String(e)));
    engineCall<TlogSession[]>("flight.tlogs", { flight_id: id })
      .then((v) => {
        if (id === flightId) sessions = v;
      })
      .catch(() => (sessions = null));
  });

  const secs = (s: number) => formatSeconds(s, app.locale);
  /** An engine code's label, or the code itself where the interface has none for it. */
  const label = (key: string, fallback: string) => (key in CATALOGS.en ? t(key as MessageKey) : fallback);
  const kindName = (failsafe: string) => label(`incidents.kind.${failsafe}`, failsafe);
  const laneName = (id: LaneId) => t(`incidents.lane.${id}` as MessageKey);
  const toneStatus = (tone: Tone): Status => (tone === "neutral" ? "info" : tone);

  const ff = $derived(timeline?.first_fault ?? null);
  const rows = $derived(
    timeline ? lanes(timeline, app.locale, { firstFault: t("incidents.firstFault"), seconds: secs }) : [],
  );
  const ax = $derived(timeline ? axis(timeline) : null);
  const marks = $derived(rows.flatMap((l) => l.marks.map((m) => ({ lane: l.id, mark: m }))));
  const tags = $derived(
    ax ? new Map(rows.flatMap((l) => (l.id === "mode" ? [] : [...placeTags(l.marks, ax, trackPx)]))) : new Map(),
  );

  /** The mark an event of the first fault or a failsafe stands for, on its lane. */
  function markFor(e: TimelineEvent): Mark | null {
    if (e.kind.startsWith("mode:")) {
      const reason = e.kind.slice("mode:".length);
      return (
        marks.find(
          ({ mark: m }) =>
            m.detail.type === "mode" && m.detail.change.reason === reason && Math.abs(m.from - e.at_s) < 0.05,
        )?.mark ?? null
      );
    }
    return marks.find(({ mark: m }) => m.detail.type === "event" && sameEvent(m.detail.event, e))?.mark ?? null;
  }

  function selectEvent(e: TimelineEvent) {
    picked = markFor(e)?.detail ?? { type: "event", lane: null, event: e };
  }

  /** The failsafe's own error on the errors lane. */
  function selectFailsafe(c: FailsafeCheck) {
    const found = marks.find(
      ({ lane, mark: m }) =>
        lane === "errors" &&
        m.detail.type === "event" &&
        m.detail.event.kind === `error:${c.facts.failsafe}` &&
        Math.abs(m.from - c.facts.at_s) < 0.1,
    );
    if (found) picked = found.mark.detail;
  }

  /** The loss's own span: the event on the link, receiver or battery lane that began with it. */
  function selectLoss(began: unknown) {
    if (typeof began !== "number") return;
    const found = marks.find(
      ({ lane, mark: m }) =>
        (lane === "link" || lane === "receiver" || lane === "battery") &&
        m.to !== null &&
        Math.abs(m.from - began) < 0.15,
    );
    if (found) picked = found.mark.detail;
  }

  // By default, where the chain starts, as the mock-up opens on it.
  const selected = $derived.by((): Detail | null => {
    if (picked) return picked;
    const first = ff?.chain[0];
    return first ? (markFor(first)?.detail ?? { type: "event", lane: null, event: first }) : null;
  });
  const selectedKey = $derived(selected ? detailKey(selected) : null);
  const selectedLane = $derived.by((): LaneId | null => {
    if (!selected) return null;
    if (selected.type === "event") return selected.lane;
    if (selected.type === "mode") return "mode";
    if (selected.type === "command") return "commands";
    if (selected.type === "warning") return "warnings";
    if (selected.type === "fix") return "gnss";
    return "pilot";
  });

  const incident = $derived(incidentFinding(analysis));
  const incidentLook = $derived(incident ? severityLook(incident.severity) : null);
  const words = $derived(incident && registry ? incidentText(registry, incident, ff, app.locale) : null);

  const failsafesSkipped = $derived(analysis?.analyzers.failsafes?.skipped ?? null);
  const checks = $derived(analysis ? failsafeChecks(analysis) : []);
  const losses = $derived(analysis ? unanswered(analysis) : []);
  const near = $derived(analysis ? nearMisses(analysis) : []);
  const summary = $derived(
    t("incidents.failsafes.summary", {
      fired: checks.length,
      missed: losses.filter((l) => l.finding.code === "failsafe.missed").length,
      off: losses.filter((l) => l.finding.code === "failsafe.off_during_loss").length,
    }),
  );

  const sources = $derived(
    sessions === null
      ? ""
      : [
          t("incidents.sources.onboard"),
          ...sessions.map((s) =>
            t("incidents.sources.tlog", { status: label(`incidents.clock.${s.status.replace(" ", "_")}`, s.status) }),
          ),
        ].join(" · "),
  );

  const responseText = (response: string) =>
    response === "continue" || response === "disarm" ? t(`incidents.response.${response}`) : response;
  const num = (v: number) => formatNumber(v, app.locale);
  const title = (f: Parameters<typeof findingTitle>[1]) => (registry ? findingTitle(registry, f, app.locale) : "");

  function roleText(e: TimelineEvent | null): string | null {
    const r = e ? role(ff, e) : null;
    if (!r) return null;
    return t(`incidents.role.${r.kind}`, { step: r.step, of: r.of });
  }

  /** The first-fault event a mode span stands for, if it is one. */
  function modeEvent(reason: string, atS: number): TimelineEvent | null {
    if (!ff) return null;
    return [...ff.chain, ...ff.explained].find((e) => e.kind === `mode:${reason}` && Math.abs(e.at_s - atS) < 0.05) ?? null;
  }

  // On a screen too narrow for the whole axis, the lanes open on where the chain starts.
  $effect(() => {
    const first = ff?.chain[0];
    const width = trackPx; // again whenever the lanes are drawn at another width
    if (!scroller || !ax || !first) return;
    const over = scroller.scrollWidth - scroller.clientWidth;
    if (over <= 0) return;
    scroller.scrollLeft = Math.min(over, Math.max(0, (at(ax, first.at_s) / 100) * width - 24));
  });

  const left = (s: number) => (ax ? `${at(ax, s)}%` : "0");
  const width = (m: Mark) => (ax && m.to !== null ? `max(${at(ax, m.to) - at(ax, m.from)}%, 3px)` : undefined);
</script>

{#if error}<p class="error">{t("report.error", { error })}</p>{/if}
{#if !timeline && !error}<p class="faint">{t("incidents.loading")}</p>{/if}

{#if timeline && ax}
  {@const tl = timeline}
  <p class="faint about">{t("incidents.about")}</p>
  <div class="layout">
    <section class="banner" style:--tone="var({STATUS[incidentLook?.status ?? 'info'].tone})" aria-live="polite">
      {#if incident && words && incidentLook}
        <h2>
          <span aria-hidden="true" class="glyph">{STATUS[incidentLook.status].glyph}</span>
          {sentence(words.title)}
        </h2>
        <p>{words.action}</p>
      {:else if !ff}
        <p>{t("incidents.none")}</p>
      {/if}
      {#if ff}
        <div class="chips">
          <span class="faint">{t("incidents.chain")}</span>
          {#each ff.chain as e, i (`${e.kind}@${e.at_s}`)}
            {#if i > 0}<span aria-hidden="true" class="faint">→</span>{/if}
            <button type="button" class="chip" onclick={() => selectEvent(e)}>
              {e.words[app.locale]} · {secs(e.at_s)}
            </button>
          {/each}
        </div>
        {#if ff.explained.length}
          <div class="chips">
            <span class="faint">{t("incidents.explains")}</span>
            {#each ff.explained as e (`${e.kind}@${e.at_s}`)}
              <button type="button" class="chip" onclick={() => selectEvent(e)}>
                {e.words[app.locale]} · {secs(e.at_s)}
              </button>
            {/each}
          </div>
        {/if}
      {/if}
    </section>

    <section class="panel timeline" aria-labelledby="tl-title">
      <div class="head">
        <h2 id="tl-title">{t("incidents.axis")}</h2>
        {#if sources}<span class="faint">{sources}</span>{/if}
        <span class="faint legend"><span class="swatch" aria-hidden="true"></span>{t("incidents.notArmed")}</span>
      </div>
      {#if sessions && sessions.length === 0}<p class="faint">{t("incidents.sources.noTlog")}</p>{/if}
      <div class="scroll" bind:this={scroller}>
        <div class="grid" style:--lanes={rows.length}>
          <div class="overlay" style:grid-row="1 / {rows.length + 1}" aria-hidden="true">
            <div class="unarmed" style:left="0" style:width={left(0)}></div>
            <div class="unarmed" style:left={left(tl.armed_s)} style:right="0"></div>
            {#if ff?.chain[0]}<div class="fault-line" style:left={left(ff.chain[0].at_s)}></div>{/if}
          </div>
          {#each rows as lane, li (lane.id)}
            <div class="lane-name" style:grid-row={li + 1}>{laneName(lane.id)}</div>
            <div class="track" style:grid-row={li + 1}>
              {#each lane.marks as m (m.key)}
                {@const tag = tags.get(m.key)}
                <button
                  type="button"
                  class="mark tone-{m.tone}"
                  class:span={m.to !== null}
                  class:mode={lane.id === "mode"}
                  aria-pressed={selectedKey === detailKey(m.detail)}
                  aria-label="{laneName(lane.id)}: {m.title}, {secs(m.from)}"
                  title={m.title}
                  style:left={left(m.from)}
                  style:width={width(m)}
                  onclick={() => (picked = m.detail)}
                >
                  {#if lane.id === "mode"}<span class="inside">{m.label}</span>{:else if m.to === null}{m.glyph}{/if}
                </button>
                {#if tag}
                  <span class="tag tone-{m.tone}" class:flip={tag.flip} style:left={left(tag.anchor)} aria-hidden="true">{tag.text}</span>
                {/if}
              {/each}
            </div>
          {/each}
          <div class="lane-name ticks-name" style:grid-row={rows.length + 1}></div>
          <div class="ticks" style:grid-row={rows.length + 1} aria-hidden="true" bind:clientWidth={trackPx}>
            {#each ax.ticks as tick (tick)}
              <span class="tick mono" style:left={left(tick)}>{secs(tick)}</span>
            {/each}
          </div>
        </div>
      </div>
    </section>

    <aside class="panel inspector" aria-live="polite" aria-label={t("inspect.evidence")}>
      {#if selected}
        {@render inspect(selected)}
      {:else}
        <p class="faint">{t("incidents.inspect.none")}</p>
      {/if}
    </aside>

    <section class="panel failsafes" aria-labelledby="fs-title">
      <div class="head">
        <h2 id="fs-title">{t("incidents.failsafes")}</h2>
        {#if analysis && !failsafesSkipped}<span class="faint">{summary}</span>{/if}
      </div>
      {#if !analysis}
        <p class="faint">{t("incidents.failsafes.waiting")}</p>
      {:else if failsafesSkipped}
        <p class="faint">{t("incidents.failsafes.skipped", { reason: failsafesSkipped })}</p>
      {:else}
        {#if checks.length === 0}<p class="faint">{t("incidents.failsafes.none")}</p>{/if}
        {#each checks as c (c.evidence.id)}
          {@render check(c)}
        {/each}
        <h3>{t("incidents.losses")}</h3>
        {#if losses.length === 0}
          <p class="faint">{t("incidents.losses.none")}</p>
        {:else}
          <ul class="losses">
            {#each losses as l (l.finding.id)}
              {@const look = severityLook(l.finding.severity)}
              <li>
                <StatusBadge status={look.status} label={t(look.label)} />
                <button type="button" class="link" onclick={() => selectLoss(l.evidence?.values.began_s)}>
                  {title(l.finding)}
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      {/if}
    </section>

    <section class="panel near" aria-labelledby="near-title">
      <div class="head">
        <h2 id="near-title">{t("incidents.near")}</h2>
        <span class="faint">{t("incidents.near.about")}</span>
      </div>
      {#if !analysis}
        <p class="faint">{t("incidents.failsafes.waiting")}</p>
      {:else if near.length === 0}
        <p class="faint">{t("incidents.near.none")}</p>
      {:else}
        <ul class="cards">
          {#each near as n (n.finding.id)}
            {@const look = severityLook(n.finding.severity)}
            <li class="card" style:--tone="var({STATUS[look.status].tone})">
              <div class="card-head">
                <span class="tone-text"><span aria-hidden="true">{STATUS[look.status].glyph}</span> {t(`incidents.near.${n.kind}`)}</span>
                {#if n.at_s !== null}<span class="mono faint">{secs(n.at_s)}</span>{/if}
              </div>
              <p>{title(n.finding)}</p>
            </li>
          {/each}
        </ul>
      {/if}
    </section>
  </div>
{/if}

{#snippet check(c: FailsafeCheck)}
  {@const f = c.facts}
  {@const timing = timingLook(c)}
  <article class="check">
    <div class="check-head">
      <button type="button" class="chip strong" style:--tone="var({STATUS[verdictLook(c.verdict)].tone})" onclick={() => selectFailsafe(c)}>
        {kindName(f.failsafe)} · {secs(f.at_s)} → {responseText(f.response)}
      </button>
      <span class="did">
        {t("incidents.didItWork")}
        <StatusBadge status={verdictLook(c.verdict)} label={t(`incidents.verdict.${c.verdict}`)} />
      </span>
    </div>
    <p>{title(c.finding)}</p>
    <div class="table-scroll">
      <table class="facts">
        <thead>
          <tr>
            <th scope="col">{t("incidents.col.check")}</th>
            <th scope="col">{t("incidents.col.configured")}</th>
            <th scope="col">{t("incidents.col.measured")}</th>
            <th scope="col"><span class="sr-only">{t("incidents.didItWork")}</span></th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">{t("incidents.row.trigger")}</th>
            <td data-label={t("incidents.col.configured")}>{f.wait_s !== null ? t("incidents.trigger.wait", { wait: secs(f.wait_s) }) : "–"}</td>
            <td data-label={t("incidents.col.measured")}>
              {#if f.loss_from !== null && f.loss_began_s !== null && f.waited_s !== null}
                {t("incidents.trigger.measured", { began: secs(f.loss_began_s), at: secs(f.at_s), waited: secs(f.waited_s) })}
                <br /><span class="faint">{t("incidents.trigger.from", { from: f.loss_from })}</span>
              {:else}
                {t("incidents.trigger.unmeasured")}
              {/if}
            </td>
            <td class="verdict">
              <span class="tone-text" style:--tone="var({STATUS[timing.status].tone})">
                <span aria-hidden="true">{STATUS[timing.status].glyph}</span>
                {t(`incidents.timing.${timing.key}`)}{#if timing.key === "late" && f.late_s !== null}&nbsp;{secs(f.late_s)}{/if}
              </span>
            </td>
          </tr>
          <tr>
            <th scope="row">{t("incidents.row.action")}</th>
            <td data-label={t("incidents.col.configured")}>
              {#each [...settingPairs(f.setting, f.setting_value), ["FS_OPTIONS", String(f.fs_options)]] as [n, v] (n)}
                <span class="mono setting">{n} {v}</span>
              {/each}
              <br /><span class="faint">{t("incidents.action.allows", { list: f.allowed.map(responseText).join(", ") })}</span>
            </td>
            <td data-label={t("incidents.col.measured")}>
              {t("incidents.action.measured", {
                response: responseText(f.response),
                after: f.responded_after_s !== null ? secs(f.responded_after_s) : "–",
                mode: f.mode_before,
              })}
            </td>
            <td class="verdict">
              <span class="tone-text" style:--tone="var({STATUS[verdictLook(c.verdict)].tone})">
                <span aria-hidden="true">{STATUS[verdictLook(c.verdict)].glyph}</span>
                {t(`incidents.verdict.${c.verdict}`)}
              </span>
            </td>
          </tr>
          <tr>
            <th scope="row">{t("incidents.row.others")}</th>
            <td data-label={t("incidents.col.configured")}></td>
            <td>
              {c.others.length
                ? c.others.map((o) => t("incidents.others.item", { name: kindName(o.failsafe), at: secs(o.at_s) })).join("; ")
                : t("incidents.others.none")}
            </td>
            <td></td>
          </tr>
          <tr>
            <th scope="row">{t("incidents.row.open")}</th>
            <td data-label={t("incidents.col.configured")}></td>
            <td>
              {f.left_open.length
                ? f.left_open.map((n) => label(`incidents.open.${n}`, n)).join("; ")
                : t("incidents.open.none")}
            </td>
            <td class="verdict faint">{t("inspect.confidence", { level: t(`confidence.${c.finding.confidence}` as MessageKey) })}</td>
          </tr>
          <tr>
            <th scope="row">{t("incidents.row.ended")}</th>
            <td data-label={t("incidents.col.configured")}></td>
            <td>{f.lasted_s !== null ? t("incidents.ended.resolved", { lasted: secs(f.lasted_s) }) : t("incidents.ended.open")}</td>
            <td></td>
          </tr>
        </tbody>
      </table>
    </div>
    {#if c.late}<p class="notice">▲ {title(c.late)}</p>{/if}

    <h3>{t("incidents.lead")}</h3>
    {#if !c.lead}
      <p class="faint">{t("incidents.lead.missing")}</p>
    {:else}
      {#if c.lead.precursors.length}
        <div class="table-scroll">
          <table class="lead">
            <thead>
              <tr>
                <th scope="col">{t("incidents.lead.precursor")}</th>
                <th scope="col" class="num">{t("incidents.lead.before")}</th>
              </tr>
            </thead>
            <tbody>
              {#each c.lead.precursors as p (p.id)}
                <tr>
                  <td>{label(`incidents.precursor.${p.id}`, p.id)}</td>
                  <td class="num" class:faint={p.lead_s === null}>{p.lead_s !== null ? secs(p.lead_s) : t("incidents.lead.none")}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
      {#if c.lead.finding && registry}
        <p class="faint">{c.lead.finding.code === "failsafe.unwarned" ? `${title(c.lead.finding)}. ` : ""}{findingAction(registry, c.lead.finding, app.locale)}</p>
      {/if}
    {/if}
  </article>
{/snippet}

{#snippet inspect(d: Detail)}
  {@const lane = selectedLane}
  {#if d.type === "event"}
    {@const e = d.event}
    {@const mark = markFor(e)}
    {@const tone = toneStatus(mark?.tone ?? "caution")}
    <p class="kicker tone-text" style:--tone="var({STATUS[tone].tone})">
      <span aria-hidden="true">{STATUS[tone].glyph}</span>
      {lane ? t("incidents.inspect.event", { lane: laneName(lane) }) : ""}
    </p>
    <h3 class="inspect-title">{sentence(e.words[app.locale])}</h3>
    <dl>
      {#if e.to_s !== undefined && !e.kind.startsWith("error:")}
        <dt>{t("incidents.inspect.span")}</dt><dd>{secs(e.at_s)} – {secs(e.to_s)}</dd>
        <dt>{t("incidents.inspect.lasted")}</dt><dd>{secs(e.seconds ?? e.to_s - e.at_s)}</dd>
      {:else}
        <dt>{t("incidents.inspect.at")}</dt><dd>{secs(e.at_s)}</dd>
      {/if}
      {#if e.kind.startsWith("error:")}
        {#if e.code !== undefined}<dt>{t("incidents.inspect.code")}</dt><dd class="mono">{e.kind.slice(6)} {e.code}</dd>{/if}
        <dt>{t("incidents.inspect.ended")}</dt><dd>{e.to_s !== undefined ? secs(e.to_s) : t("incidents.inspect.unresolved")}</dd>
      {/if}
      {#if e.monitor !== undefined && e.under !== undefined}
        <dt>{t("incidents.inspect.battery")}</dt><dd class="mono">{t("incidents.inspect.batteryValue", { monitor: e.monitor, volts: num(e.under) })}</dd>
      {/if}
      {#if e.what !== undefined}<dt>{t("incidents.inspect.read")}</dt><dd class="mono">{e.what}</dd>{/if}
      {#if e.name !== undefined}
        <dt>{t("incidents.inspect.param")}</dt>
        <dd class="mono">{e.name} {e.from !== undefined ? num(e.from) : "–"} → {e.to !== undefined ? num(e.to) : "–"}</dd>
      {/if}
      {#if e.mode !== undefined}<dt>{t("incidents.inspect.mode")}</dt><dd class="mono">{e.mode}</dd>{/if}
      {#if roleText(e)}<dt>{t("incidents.inspect.role")}</dt><dd>{roleText(e)}</dd>{/if}
    </dl>
  {:else if d.type === "mode"}
    {@const own = modeEvent(d.change.reason, d.change.at_s)}
    <p class="kicker">{t("incidents.inspect.event", { lane: laneName("mode") })}</p>
    <h3 class="inspect-title mono">{d.change.mode}</h3>
    <dl>
      <dt>{t("incidents.inspect.span")}</dt><dd>{secs(d.change.at_s)} – {secs(d.to_s)}</dd>
      <dt>{t("incidents.inspect.reason")}</dt><dd class="mono">{d.change.reason}</dd>
      {#if own && roleText(own)}<dt>{t("incidents.inspect.role")}</dt><dd>{roleText(own)}</dd>{/if}
    </dl>
  {:else if d.type === "command"}
    <p class="kicker">{t("incidents.inspect.event", { lane: laneName("commands") })}</p>
    <h3 class="inspect-title mono">{d.command.command}</h3>
    <dl>
      <dt>{t("incidents.inspect.at")}</dt><dd>{secs(d.command.at_s)}</dd>
      <dt>{t("incidents.inspect.result")}</dt><dd>{d.command.result}</dd>
      <dt>{t("incidents.inspect.sender")}</dt><dd>{t("incidents.inspect.senderValue", { id: d.command.sender })}</dd>
    </dl>
  {:else if d.type === "warning"}
    {@const tone: Status = d.warning.severity <= 3 ? "out" : "caution"}
    <p class="kicker tone-text" style:--tone="var({STATUS[tone].tone})">
      <span aria-hidden="true">{STATUS[tone].glyph}</span> {t("incidents.inspect.event", { lane: laneName("warnings") })}
    </p>
    <h3 class="inspect-title">“{d.warning.text}”</h3>
    <dl>
      <dt>{t("incidents.inspect.at")}</dt><dd>{secs(d.warning.at_s)}</dd>
      <dt>{t("incidents.inspect.severity")}</dt>
      <dd>{label(`incidents.severity.${d.warning.severity}`, String(d.warning.severity))}</dd>
    </dl>
    <p class="faint">{t("incidents.inspect.local")}</p>
  {:else if d.type === "fix"}
    <p class="kicker">{t("incidents.inspect.event", { lane: laneName("gnss") })}</p>
    <h3 class="inspect-title">
      {sentence(label(`incidents.fix.${d.span.value}`, String(d.span.value)))}
    </h3>
    <dl>
      <dt>{t("incidents.inspect.fix")}</dt><dd class="mono">GPS.Status {d.span.value}</dd>
      <dt>{t("incidents.inspect.span")}</dt><dd>{secs(d.span.from_s)} – {secs(d.span.to_s)}</dd>
    </dl>
  {:else}
    <p class="kicker">{t("incidents.inspect.event", { lane: laneName("pilot") })}</p>
    <h3 class="inspect-title">{t("incidents.inspect.override")}</h3>
    <dl>
      <dt>{t("incidents.inspect.span")}</dt><dd>{secs(d.span.from_s)} – {secs(d.span.to_s)}</dd>
      <dt>{t("incidents.inspect.lasted")}</dt><dd>{secs(d.span.to_s - d.span.from_s)}</dd>
    </dl>
  {/if}
{/snippet}

<style>
  .about {
    margin: 0 0 10px;
  }
  .layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(260px, 340px);
    grid-template-areas:
      "banner side"
      "timeline side"
      "failsafes side"
      "near side";
    grid-template-rows: auto auto auto 1fr;
    gap: 12px;
    align-items: start;
  }
  .banner {
    grid-area: banner;
  }
  .timeline {
    grid-area: timeline;
  }
  .inspector {
    grid-area: side;
    position: sticky;
    top: 8px;
  }
  .failsafes {
    grid-area: failsafes;
  }
  .near {
    grid-area: near;
  }
  @media (max-width: 1100px) {
    .layout {
      grid-template-columns: minmax(0, 1fr);
      grid-template-areas: "banner" "timeline" "side" "failsafes" "near";
      grid-template-rows: none;
    }
    .inspector {
      position: static;
    }
  }
  .panel {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 14px 16px;
    background: var(--surface);
    border: 1px solid var(--border-soft);
    border-radius: 12px;
    min-width: 0;
  }
  .banner {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px 16px;
    border-radius: 10px;
    border: 1px solid color-mix(in srgb, var(--tone) 45%, var(--border));
    background: color-mix(in srgb, var(--tone) 8%, var(--surface));
    min-width: 0;
  }
  .banner h2 {
    color: var(--tone);
    font-size: 14px;
  }
  .banner .glyph {
    font-size: 11px;
  }
  h2 {
    margin: 0;
    font-size: 15px;
  }
  h3 {
    margin: 4px 0 0;
    font-size: 13px;
  }
  p {
    margin: 0;
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
    font-weight: normal;
  }
  .error {
    color: var(--out);
  }
  .notice {
    color: var(--caution);
    font-size: 13px;
  }
  .mono {
    font-family: var(--mono);
    font-size: 12px;
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 6px 14px;
  }
  .legend {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .swatch {
    width: 14px;
    height: 10px;
    border-radius: 2px;
    background: color-mix(in srgb, var(--text-faint) 14%, transparent);
  }
  .setting {
    white-space: nowrap;
    margin-right: 10px;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .chip {
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--text-2);
    padding: 3px 10px;
    font-size: 12px;
    cursor: pointer;
    max-width: 100%;
    text-align: left;
  }
  .chip.strong {
    color: var(--tone);
    border-color: color-mix(in srgb, var(--tone) 45%, var(--border));
    background: color-mix(in srgb, var(--tone) 12%, transparent);
    font-weight: 600;
  }
  .link {
    background: none;
    border: none;
    padding: 0;
    color: var(--text);
    cursor: pointer;
    font-size: 13px;
    text-align: left;
  }
  .link:hover {
    color: var(--link);
  }

  /* the lanes: one grid, labels on the left, every track on the same axis */
  .scroll {
    overflow-x: auto;
    padding-bottom: 4px;
  }
  .grid {
    display: grid;
    grid-template-columns: 150px minmax(0, 1fr);
    min-width: 680px;
    position: relative;
  }
  @media (max-width: 640px) {
    .grid {
      grid-template-columns: 104px minmax(0, 1fr);
      min-width: 620px;
    }
    .lane-name {
      font-size: 11px;
    }
    /* each check as a short block of facts */
    .table-scroll .facts {
      min-width: 0;
    }
    .facts,
    .facts tbody,
    .facts tr,
    .facts td,
    .facts tbody th {
      display: block;
      min-width: 0;
    }
    .facts thead {
      display: none;
    }
    .facts tr {
      padding: 8px 0;
      border-bottom: 1px solid var(--border-soft);
    }
    .facts td,
    .facts tbody th {
      border: none;
      padding: 2px 0;
      text-align: left;
      white-space: normal;
    }
    .facts tbody th {
      font-weight: 600;
    }
    .facts td:empty {
      display: none;
    }
    .facts td[data-label]::before {
      content: attr(data-label) " · ";
      color: var(--text-faint);
      font-size: 12px;
    }
  }
  .lane-name {
    grid-column: 1;
    font-size: 12px;
    line-height: 1.25;
    color: var(--text-muted);
    display: flex;
    align-items: flex-end;
    padding: 0 8px 4px 0;
    min-height: 36px;
    box-sizing: border-box;
    /* the names stay in view while a narrow screen scrolls the lanes */
    position: sticky;
    left: 0;
    z-index: 2;
    background: var(--surface);
  }
  .track {
    grid-column: 2;
    position: relative;
    min-height: 36px;
    z-index: 1;
  }
  .track::before {
    content: "";
    position: absolute;
    left: 0;
    right: 0;
    top: 25px;
    border-top: 1px solid var(--border-soft);
  }
  .overlay {
    grid-column: 2;
    position: relative;
    z-index: 0;
    pointer-events: none;
  }
  .unarmed {
    position: absolute;
    top: 0;
    bottom: 0;
    background: color-mix(in srgb, var(--text-faint) 6%, transparent);
  }
  .fault-line {
    position: absolute;
    top: 0;
    bottom: 0;
    border-left: 1px dashed var(--out);
  }
  .tone-ok {
    --tone: var(--ok);
  }
  .tone-caution {
    --tone: var(--caution);
  }
  .tone-out {
    --tone: var(--out);
  }
  .tone-info {
    --tone: var(--info);
  }
  .tone-neutral {
    --tone: var(--text-faint);
  }
  .tone-text {
    color: var(--tone);
  }
  .mark {
    position: absolute;
    top: 25px;
    transform: translate(-50%, -50%);
    background: none;
    border: none;
    padding: 0 2px;
    color: var(--tone);
    font-size: 13px;
    line-height: 1;
    cursor: pointer;
    border-radius: 3px;
  }
  .mark.span {
    transform: translateY(-50%);
    height: 6px;
    padding: 0;
    background: color-mix(in srgb, var(--tone) 75%, transparent);
  }
  .mark.mode {
    height: 16px;
    background: color-mix(in srgb, var(--tone) 22%, var(--surface-2));
    border: 1px solid color-mix(in srgb, var(--tone) 55%, var(--border));
    box-sizing: border-box;
    overflow: hidden;
    text-align: left;
  }
  .mark.mode.tone-neutral {
    background: var(--raised);
    border-color: var(--border);
  }
  .inside {
    display: block;
    padding: 0 4px;
    font-size: 10px;
    line-height: 14px;
    color: var(--text-2);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    font-family: var(--mono);
  }
  .mark[aria-pressed="true"] {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .tag {
    position: absolute;
    top: 3px;
    margin-left: 6px;
    font-size: 11px;
    line-height: 13px;
    color: var(--tone);
    white-space: nowrap;
    pointer-events: none;
  }
  .tag.flip {
    margin-left: 0;
    transform: translateX(calc(-100% - 6px));
  }
  .ticks {
    grid-column: 2;
    position: relative;
    height: 20px;
    border-top: 1px solid var(--border);
  }
  .tick {
    position: absolute;
    top: 4px;
    transform: translateX(-50%);
    font-size: 11px;
    color: var(--text-faint);
    white-space: nowrap;
  }

  /* the inspector */
  .kicker {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-muted);
  }
  .kicker.tone-text {
    color: var(--tone);
  }
  .inspect-title {
    margin: 0;
    font-size: 16px;
  }
  dl {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 6px 12px;
    margin: 0;
    font-size: 13px;
  }
  dt {
    color: var(--text-muted);
  }
  dd {
    margin: 0;
    text-align: right;
    overflow-wrap: anywhere;
  }

  /* did each failsafe work */
  .check {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-top: 10px;
    border-top: 1px solid var(--border-soft);
  }
  .check-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 14px;
  }
  .did {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    color: var(--text-muted);
  }
  .table-scroll {
    overflow-x: auto;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  .facts {
    min-width: 560px;
  }
  thead th {
    text-align: left;
    font-weight: 500;
    color: var(--text-muted);
    padding: 6px 10px 6px 0;
    border-bottom: 1px solid var(--border);
  }
  tbody th {
    text-align: left;
    font-weight: normal;
    color: var(--text-2);
    padding: 8px 10px 8px 0;
    vertical-align: top;
    white-space: nowrap;
  }
  td {
    padding: 8px 10px 8px 0;
    border-bottom: 1px solid var(--border-soft);
    vertical-align: top;
  }
  tbody th {
    border-bottom: 1px solid var(--border-soft);
  }
  .verdict {
    text-align: right;
    white-space: nowrap;
    font-weight: 600;
    font-size: 12px;
  }
  .num {
    text-align: right;
    white-space: nowrap;
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
  }
  .losses {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .losses li {
    display: flex;
    gap: 10px;
    align-items: flex-start;
    flex-wrap: wrap;
  }
  .losses .link {
    flex: 1 1 260px;
    min-width: 0;
  }

  /* near misses */
  .cards {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 10px;
  }
  .card {
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 10px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 13px;
  }
  .card-head {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    font-weight: 600;
    font-size: 12px;
  }
</style>
