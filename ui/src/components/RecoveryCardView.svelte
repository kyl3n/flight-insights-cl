<script lang="ts">
  // The airframe's recovery card (spec: Navigation and GNSS, "Recovery card"; mock-up
  // RecoveryCard): what the aircraft will do and what the operator does for each loss, as
  // the engine words it, on a white page that prints alone. A section the engine marks
  // as an alert is drawn in red, with a word as well as the colour.
  import { app, t } from "../lib/app.svelte";
  import { look, ordered, outages, recoveryCard, shortSet, wide, type RecoveryCard } from "../lib/recovery";

  let { airframeId }: { airframeId: string } = $props();

  let card: RecoveryCard | null = $state(null);
  let failed: string | null = $state(null);

  $effect(() => {
    const id = airframeId;
    card = null;
    failed = null;
    recoveryCard(id).then(
      (c) => (card = c),
      (e: unknown) => (failed = e instanceof Error ? e.message : String(e)),
    );
  });

  const printed = $derived(
    new Date().toLocaleDateString(app.locale === "uk" ? "uk-UA" : "en-GB", {
      day: "numeric",
      month: "short",
      year: "numeric",
    }),
  );
</script>

<main class="recovery">
  <div class="head no-print">
    <h1>{card ? card.title[app.locale] : t("fleet.recovery")}</h1>
    <p class="muted">{t("recovery.about")}</p>
  </div>

  {#if failed !== null}
    <section class="panel no-print">
      <p>{t("recovery.none")}</p>
      <p class="faint">{failed}</p>
      <button type="button" onclick={() => (app.recoveryCard = null)}>{t("recovery.back")}</button>
    </section>
  {:else if card === null}
    <p class="muted no-print">{t("recovery.loading")}</p>
  {:else}
    <div class="body">
      <article class="paper print-card" aria-label={card.title[app.locale]}>
        <header>
          <h2>{card.title[app.locale]}</h2>
          <span class="sub">{t("recovery.printed", { date: printed })}</span>
          {#if card.valid}<span class="valid mono">{card.valid[app.locale]}</span>{/if}
        </header>
        <div class="lead">
          <span class="strong">{card.home_heading[app.locale]}</span>
          <span><span class="soft">{t("recovery.fallback")}</span> <strong>{card.fallback_mode ?? t("recovery.fallback.none")}</strong></span>
        </div>
        <div class="sections">
          {#each ordered(card) as s (s.id)}
            {@const kind = look(s)}
            <section class="card-section {kind}" class:wide={wide(s)} aria-labelledby="sec-{s.id}">
              <h3 id="sec-{s.id}">
                {s.title[app.locale]}
                {#if kind === "alert"}<span class="alert-tag">▲ {t("recovery.alert")}</span>{/if}
              </h3>
              {#if s.will.length || s.facts.length}
                <dl>
                  {#if s.will.length}
                    <dt>{card.will_label[app.locale]}</dt>
                    <dd><ul>{#each s.will as p, i (i)}<li>{p.text[app.locale]}</li>{/each}</ul></dd>
                  {/if}
                  {#if s.facts.length}
                    <dt>{t("recovery.measured")}</dt>
                    <dd><ul>{#each s.facts as p, i (i)}<li>{p.text[app.locale]}</li>{/each}</ul></dd>
                  {/if}
                </dl>
              {/if}
              {#if s.steps.length}
                {#if s.will.length || s.facts.length}<h4>{t("recovery.operator")}</h4>{/if}
                <ol>{#each s.steps as p, i (i)}<li>{p.text[app.locale]}</li>{/each}</ol>
              {/if}
            </section>
          {/each}
        </div>
        <footer>{card.footer[app.locale]}</footer>
      </article>

      <aside class="panel no-print" aria-labelledby="print-title">
        <h2 id="print-title">{t("recovery.print.title")}</h2>
        <div class="kv">
          <span class="muted">{t("recovery.print.paper")}</span><span>{t("recovery.print.paper.value")}</span>
          {#if card.param_set}
            <span class="muted">{t("recovery.print.version")}</span>
            <span class="mono">{t("recovery.print.version.value", { set: shortSet(card.param_set) ?? "" })}</span>
          {/if}
          <span class="muted">{t("recovery.print.from")}</span>
          <span>{t("recovery.print.from.value", { count: outages(card) })}</span>
        </div>
        <p class="faint">{t("recovery.print.about")}</p>
        <div class="buttons">
          <button type="button" class="primary" onclick={() => window.print()}>{t("recovery.print")}</button>
          <button type="button" onclick={() => (app.recoveryCard = null)}>{t("recovery.back")}</button>
        </div>
      </aside>
    </div>
  {/if}
</main>

<style>
  .recovery {
    display: flex;
    flex-direction: column;
    gap: 16px;
    padding: 18px 24px 24px;
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 8px 16px;
  }
  .head h1 {
    margin: 0;
    font-size: 22px;
    font-weight: 600;
  }
  .head p {
    margin: 0;
    font-size: 13px;
  }
  .muted {
    color: var(--text-muted);
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
    line-height: 1.5;
    margin: 0;
  }
  .mono {
    font-family: var(--mono);
  }
  .body {
    display: flex;
    flex-wrap: wrap;
    gap: 20px;
    align-items: flex-start;
  }
  .panel {
    flex: 1 1 260px;
    max-width: 340px;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 14px;
    padding: 18px;
    border-radius: 12px;
    background: var(--surface-2);
    border: 1px solid var(--border);
  }
  .panel h2 {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
  }
  .kv {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 6px 12px;
    font-size: 12px;
  }
  .kv > :nth-child(even) {
    text-align: right;
  }
  .buttons {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  button {
    min-height: 40px;
    padding: 0 14px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: transparent;
    color: var(--text-2);
    font: inherit;
    font-size: 13px;
    cursor: pointer;
  }
  button.primary {
    border: 0;
    background: var(--accent);
    color: var(--accent-text);
    font-weight: 600;
  }

  /* The card itself: black on white in the app and on paper alike. */
  .paper {
    flex: 0 1 820px;
    min-width: 0;
    box-sizing: border-box;
    padding: 28px 30px;
    border-radius: 6px;
    background: #ffffff;
    color: #14181c;
    display: flex;
    flex-direction: column;
    gap: 14px;
    font-family: var(--font);
  }
  .paper header {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 6px 16px;
    padding-bottom: 10px;
    border-bottom: 2px solid #14181c;
  }
  .paper h2 {
    margin: 0;
    font-size: 22px;
    font-weight: 600;
  }
  .sub,
  .valid,
  .soft {
    font-size: 12px;
    color: #4a535d;
  }
  .valid {
    margin-left: auto;
  }
  .lead {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 22px;
    font-size: 13px;
  }
  .strong {
    font-weight: 500;
  }
  .sections {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 12px;
  }
  .card-section {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 14px 16px;
    border: 1.5px solid #2f6e9e;
    border-radius: 8px;
    break-inside: avoid;
  }
  .card-section.wide {
    grid-column: 1 / -1;
  }
  .card-section.alert {
    border-color: #b23b38;
  }
  .card-section.spoofing {
    border: 1.5px dashed #b07a1f;
  }
  .card-section h3 {
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 10px;
    font-size: 16px;
    font-weight: 600;
  }
  .alert-tag {
    font-size: 12px;
    font-weight: 600;
    color: #b23b38;
  }
  .card-section h4 {
    margin: 4px 0 0;
    font-size: 13px;
    font-weight: 600;
  }
  dl {
    margin: 0;
    display: grid;
    grid-template-columns: minmax(90px, 130px) 1fr;
    gap: 6px 8px;
    font-size: 13px;
    line-height: 1.45;
  }
  dt {
    color: #4a535d;
  }
  dd {
    margin: 0;
  }
  ul {
    margin: 0;
    padding-left: 16px;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  ol {
    margin: 0;
    padding-left: 20px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 13px;
    line-height: 1.45;
  }
  .paper footer {
    font-size: 11px;
    color: #4a535d;
  }

  /* Printing prints the card alone. */
  @media print {
    :global(body *) {
      visibility: hidden;
    }
    .print-card,
    .print-card :global(*) {
      visibility: visible;
    }
    .print-card {
      position: absolute;
      inset: 0 0 auto 0;
      padding: 0;
      border-radius: 0;
    }
    .no-print {
      display: none;
    }
  }
</style>
