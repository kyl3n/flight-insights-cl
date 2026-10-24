<script lang="ts">
  // The flight report's written summary (spec: AI layer; mock-up FlightReport,
  // "Summary"): what Claude made of the findings, tinted violet and labelled, each
  // claim linked to the finding it cites. Only a reply the engine accepted is shown;
  // otherwise the section says why there is none. The measured report never waits.
  import { app, t } from "../lib/app.svelte";
  import {
    aiSendNow,
    aiStatus,
    findingFor,
    onAiProgress,
    sectionState,
    verdictCounts,
    type AiStatus,
    type Exchange,
    type Progress,
  } from "../lib/ai";
  import type { MessageKey } from "../lib/i18n";
  import { engineCall, inDesktopApp } from "../lib/shell";

  let { flightId, onSelect }: { flightId: string; onSelect: (findingId: string) => void } = $props();

  let status = $state<AiStatus | null>(null);
  let exchange = $state<Exchange | null>(null);
  let progress = $state<Progress | null>(null);
  let asked = $state(false);

  async function load() {
    if (!inDesktopApp()) return;
    try {
      status = await aiStatus();
      exchange = await engineCall<Exchange | null>("ai.results", { flight_id: flightId });
    } catch {
      // the measured report stands without this section
    }
  }

  $effect(() => {
    void flightId;
    progress = null;
    asked = false;
    load();
    let stop: (() => void) | undefined;
    onAiProgress((p) => {
      if (p.flight_id !== flightId) return;
      progress = p;
      if (["done", "failed", "held"].includes(p.state)) load();
    }).then((unlisten) => (stop = unlisten));
    return () => stop?.();
  });

  const view = $derived(sectionState(status, exchange, progress));

  async function sendNow() {
    asked = true;
    progress = { flight_id: flightId, state: "sending", detail: null };
    await aiSendNow(flightId);
  }

  function problemText(x: Exchange): string {
    if (x.outcome === "declined") return t("ai.problem.declined");
    if (x.outcome === "invalid") return t("ai.problem.invalid");
    return t("ai.problem.failed", { reason: x.reasons[0] ?? "" });
  }
</script>

{#if view.kind !== "off" || view.why !== "browser"}
  <section class="ai" aria-labelledby="ai-title" aria-busy={view.kind === "pending"}>
    <div class="head">
      <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z" />
      </svg>
      <h2 id="ai-title">{t("ai.title")}</h2>
      <span class="tag">{t("ai.label")}</span>
      <span class="byline">{t("ai.byline")}</span>
    </div>

    {#if view.kind === "off"}
      <p class="quiet">{t(`ai.off.${view.why}` as MessageKey)}</p>
    {:else if view.kind === "pending"}
      <p class="quiet">{t("ai.reading")}</p>
    {:else if view.kind === "held" || view.kind === "problem"}
      <p class="quiet">{view.kind === "held" ? t("ai.held") : problemText(view.exchange)}</p>
      <button type="button" class="link" disabled={asked} onclick={sendNow}>{t("ai.sendNow")}</button>
    {:else}
      {@const x = view.exchange}
      {@const reply = x.reply!}
      <p class="summary">{reply.summary}</p>
      {#snippet refs(list: string[])}
        {#each list as ref (ref)}
          {@const finding = findingFor(x, ref)}
          {#if finding}
            <button type="button" class="ref" onclick={() => onSelect(finding)}>{ref}</button>
          {:else}
            <span class="ref">{ref}</span>
          {/if}
        {/each}
      {/snippet}
      {#if reply.root_causes.length}
        <h3>{t("ai.causes")}</h3>
        <ul>
          {#each reply.root_causes as c, i (i)}
            <li><strong>{c.title}</strong> {c.explanation} {@render refs(c.refs)}</li>
          {/each}
        </ul>
      {/if}
      {#if reply.disagreements.length}
        <h3>{t("ai.disagree")}</h3>
        <ul>
          {#each reply.disagreements as d, i (i)}
            <li>{d.view} {@render refs([d.ref])}</li>
          {/each}
        </ul>
      {/if}
      {#if reply.questions.length}
        <h3>{t("ai.questions")}</h3>
        <ul>
          {#each reply.questions as q, i (i)}
            <li>{q.question} <span class="quiet">{q.why}</span></li>
          {/each}
        </ul>
      {/if}
      {#if reply.tests_to_fly.length}
        <h3>{t("ai.tests")}</h3>
        <ul>
          {#each reply.tests_to_fly as f, i (i)}
            <li>{f.test} <span class="quiet">{t("ai.lookFor", { what: f.look_for })}</span></li>
          {/each}
        </ul>
      {/if}
      {#if x.recommendations.length}
        {@const n = verdictCounts(x.recommendations)}
        <p class="quiet">{t("ai.changes", { ...n })}</p>
      {/if}
      <p class="quiet small">{x.model} · {new Intl.NumberFormat(app.locale, { style: "currency", currency: "USD" }).format(x.cost_usd)}</p>
    {/if}
  </section>
{/if}

<style>
  .ai {
    padding: 14px 18px;
    border-radius: 12px;
    background: color-mix(in srgb, var(--ai) 8%, var(--surface));
    border: 1px solid color-mix(in srgb, var(--ai) 35%, var(--border));
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .head {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    color: var(--ai);
  }
  h2 {
    margin: 0;
    font-size: 14px;
    color: var(--text);
  }
  h3 {
    margin: 6px 0 0;
    font-size: 12px;
    color: var(--ai);
  }
  .tag {
    font-size: 11px;
    font-weight: 600;
    padding: 1px 6px;
    border-radius: 4px;
    border: 1px solid currentColor;
  }
  .byline,
  .quiet {
    font-size: 12px;
    color: color-mix(in srgb, var(--ai) 70%, var(--text-muted));
  }
  .summary {
    margin: 0;
    font-size: 14px;
    line-height: 1.6;
  }
  ul {
    margin: 0;
    padding-left: 18px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 13px;
    line-height: 1.5;
  }
  p {
    margin: 0;
  }
  .small {
    font-size: 11px;
  }
  .ref {
    font: inherit;
    font-size: 11px;
    padding: 0 5px;
    margin-left: 2px;
    border-radius: 4px;
    border: 1px solid color-mix(in srgb, var(--ai) 50%, transparent);
    background: transparent;
    color: var(--ai);
  }
  button.ref {
    cursor: pointer;
  }
  .link {
    align-self: flex-start;
    background: none;
    border: 0;
    padding: 0;
    color: var(--ai);
    text-decoration: underline;
    cursor: pointer;
    font: inherit;
    font-size: 13px;
  }
</style>
