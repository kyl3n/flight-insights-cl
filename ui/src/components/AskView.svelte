<script lang="ts">
  // Ask (spec: AI layer, "Chat follow-ups"): questions about one flight, answered by
  // Claude from that flight's measurements. What a person types is sent, so the view
  // says not to name places, and every question is shown exactly as it will leave
  // (the whole packet, checked by both checks) before the person sends it.
  import { app, t } from "../lib/app.svelte";
  import {
    aiChatCancel,
    aiChatPrepare,
    aiChatSend,
    prettyPacket,
    QUESTION_MAX,
    type ChatTurn,
    type Prepared,
  } from "../lib/ai";
  import { latestFlight } from "../lib/data";
  import { formatTakeoff, type FlightRow } from "../lib/report";
  import { engineCall, inDesktopApp } from "../lib/shell";

  let flight = $state<FlightRow | null>(null);
  let turns = $state<ChatTurn[]>([]);
  let question = $state("");
  let review = $state<Extract<Prepared, { state: "review" }> | null>(null);
  let busy = $state(false);
  let error = $state("");

  async function load() {
    error = "";
    if (!inDesktopApp() || app.shell.engine !== "ready") return;
    try {
      if (app.scope.flight) {
        const rows = await engineCall<FlightRow[]>("flight.list", { flight_id: app.scope.flight });
        flight = rows[0] ?? null;
      } else if (app.scope.airframe) {
        flight = await latestFlight(app.scope.airframe);
      } else {
        const rows = await engineCall<FlightRow[]>("flight.list", {});
        flight = rows[0] ?? null;
      }
      turns = flight ? await engineCall<ChatTurn[]>("ai.chat_history", { flight_id: flight.id }) : [];
    } catch (e) {
      error = String(e);
    }
  }

  $effect(() => {
    void app.scope.flight;
    void app.scope.airframe;
    void app.shell.engine;
    load();
  });

  async function prepare() {
    if (!flight || !question.trim()) return;
    busy = true;
    error = "";
    try {
      const got = await aiChatPrepare(flight.id, question.trim());
      if (got.state === "review") review = got;
      else if (got.state === "refused") {
        error = [t("ask.refused", { by: got.by }), ...got.reasons.map((r) => `${r.path} · ${r.rule}`)].join("\n");
      } else error = t("ai.off.blocked");
    } catch (e) {
      error = String(e);
    } finally {
      busy = false;
    }
  }

  async function send() {
    if (!review) return;
    const id = review.packet_id;
    review = null;
    busy = true;
    try {
      const got = await aiChatSend(id);
      if (got.state === "done" && got.exchange.outcome === "accepted") {
        question = "";
        await load();
      } else if (got.state === "done") {
        error = t("ask.problem", { reason: got.exchange.reasons[0] ?? got.exchange.outcome });
      } else if (got.state === "refused") {
        error = t("ask.refused", { by: got.by });
      } else {
        error = t("ai.off.blocked");
      }
    } catch (e) {
      error = String(e);
    } finally {
      busy = false;
    }
  }

  async function cancel() {
    if (review) await aiChatCancel(review.packet_id);
    review = null;
  }
</script>

<main>
  <h1>{t("ask.title")}</h1>
  {#if !flight}
    <p class="faint">{inDesktopApp() ? t("ask.noFlight") : t("ai.off.browser")}</p>
  {:else}
    <p class="faint">{t("ask.about", { when: `${flight.airframe} · ${formatTakeoff(flight.utc_us, app.locale) ?? ""}` })}</p>

    <section class="turns" aria-live="polite">
      {#if turns.length === 0}<p class="faint">{t("ask.empty")}</p>{/if}
      {#each turns as turn (turn.exchange_id)}
        <div class="q"><span class="who">{t("ask.you")}</span><p>{turn.question}</p></div>
        <div class="a">
          <span class="who">Claude <span class="tag">{t("ai.label")}</span></span>
          <p>{turn.answer}</p>
          {#if turn.refs.length}<p class="refs mono">{turn.refs.join(" · ")}</p>{/if}
        </div>
      {/each}
      {#if busy && !review}<p class="faint">{t("ask.sending")}</p>{/if}
    </section>

    {#if review}
      <section class="review" aria-labelledby="review-title">
        <h2 id="review-title">{t("ask.reviewTitle")}</h2>
        <p class="asked">{question.trim()}</p>
        <p class="faint">{t("ask.reviewWith")}</p>
        <pre class="mono">{prettyPacket(review.packet_json)}</pre>
        <div class="row">
          <button type="button" class="primary" onclick={send}>{t("ask.send")}</button>
          <button type="button" onclick={cancel}>{t("ask.cancel")}</button>
        </div>
      </section>
    {:else}
      <form onsubmit={(e) => { e.preventDefault(); prepare(); }}>
        <p class="warn">{t("ask.warn")}</p>
        <textarea rows="3" maxlength={QUESTION_MAX} placeholder={t("ask.placeholder")} bind:value={question}></textarea>
        <div class="row">
          <span class="faint">{t("ask.chars", { count: question.length, max: QUESTION_MAX })}</span>
          <button type="submit" class="primary" disabled={busy || !question.trim()}>{t("ask.review")}</button>
        </div>
      </form>
    {/if}
    {#if error}<p class="error" role="alert">{error}</p>{/if}
  {/if}
</main>

<style>
  main {
    padding: 16px 24px 32px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    max-width: 900px;
  }
  h1 {
    margin: 0;
    font-size: 20px;
  }
  h2 {
    margin: 0;
    font-size: 14px;
  }
  p {
    margin: 0;
  }
  .turns {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .q,
  .a {
    padding: 10px 14px;
    border-radius: 10px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    line-height: 1.5;
  }
  .q {
    background: var(--surface);
    border: 1px solid var(--border-soft);
    align-self: flex-end;
    max-width: 80%;
  }
  .a {
    background: color-mix(in srgb, var(--ai) 8%, var(--surface));
    border: 1px solid color-mix(in srgb, var(--ai) 35%, var(--border));
  }
  .who {
    font-size: 12px;
    color: var(--text-muted);
  }
  .a .who,
  .refs {
    color: var(--ai);
    font-size: 12px;
  }
  .tag {
    font-size: 10px;
    font-weight: 600;
    padding: 0 5px;
    border-radius: 4px;
    border: 1px solid currentColor;
  }
  form,
  .review {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .review {
    padding: 12px 14px;
    border-radius: 10px;
    border: 1px solid var(--caution);
  }
  .asked {
    padding: 8px 10px;
    border-left: 3px solid var(--caution);
    background: var(--surface);
  }
  .warn {
    font-size: 12px;
    color: var(--caution);
  }
  textarea {
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-2);
    color: var(--text);
    font: inherit;
    resize: vertical;
  }
  .row {
    display: flex;
    gap: 8px;
    align-items: center;
    justify-content: space-between;
  }
  pre {
    max-height: 320px;
    overflow: auto;
    font-size: 11px;
    background: var(--surface-2);
    padding: 8px;
    border-radius: 6px;
    margin: 0;
  }
  button {
    padding: 8px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface);
    color: var(--text);
    font-size: 13px;
    cursor: pointer;
  }
  .primary {
    border: 0;
    background: var(--accent);
    color: var(--accent-text);
    font-weight: 600;
  }
  button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  .faint {
    font-size: 12px;
    color: var(--text-faint);
  }
  .error {
    color: var(--out);
    font-size: 13px;
    white-space: pre-line;
  }
</style>
