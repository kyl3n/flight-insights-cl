<script lang="ts">
  // Try a propeller (spec: Power and efficiency analysis, "Propeller and motor choice",
  // "What is predicted for a candidate"; mock-up Propellers): any propeller, and
  // optionally another motor, predicted against the one flown by `airframe.prop_candidate`,
  // side by side with the checks it is held to. The form starts on the best the search
  // found, else the propeller flown. Every candidate figure is theory, labelled so.
  import { untrack } from "svelte";
  import { app, t } from "../lib/app.svelte";
  import type { Family } from "../lib/bench";
  import type { MessageKey } from "../lib/i18n";
  import {
    candidateValues,
    CHECKS,
    checkKey,
    checkLook,
    compareRows,
    failedKey,
    fillTry,
    fixed,
    LIMIT_DIGITS,
    PREDICTION_NOTES,
    propCandidate,
    propName,
    RESPONSE_NOTES,
    spreadShown,
    type Check,
    type CompareRow,
    type PropCandidate,
    type Propellers,
    type PropSearch,
    type TryDraft,
    type TryField,
  } from "../lib/props";
  import { unitText } from "../lib/report";
  import TheoryPill from "./TheoryPill.svelte";

  let {
    airframeId,
    view,
    found,
    families,
    tag,
  }: { airframeId: string; view: Propellers; found: PropSearch | null; families: Family[]; tag: (id: string) => string } = $props();

  let draft = $state<TryDraft | null>(null);
  let result = $state<PropCandidate | null>(null);
  let busy = $state(false);
  let error = $state("");
  // Until a person types, the form follows what the screen learns: the propeller flown,
  // then the best the search found (predicted at once). Plain variables: nothing reads them
  // in the markup.
  let touched = false;
  let filled: "none" | "flown" | "search" = "none";

  $effect(() => {
    const flown = view.prop;
    const s = found;
    untrack(() => {
      if (touched || !flown) return;
      if (s && filled !== "search") {
        filled = "search";
        const best = s.search?.best ?? null;
        draft = fillTry(best ?? flown, s.inputs?.mass_kg);
        if (best) void predict();
      } else if (!s && filled === "none") {
        filled = "flown";
        draft = fillTry(flown, null);
      }
    });
  });

  const FIELDS: Record<TryField, MessageKey> = {
    diameter_in: "props.try.diameter",
    pitch_in: "props.try.pitch",
    blades: "props.try.blades",
    kv: "props.try.kv",
    resistance_mohm: "props.try.resistance",
    no_load_a: "props.try.noLoad",
    mass_g: "props.try.mass",
    mass_kg: "props.try.takeoff",
  };

  async function predict() {
    if (!draft) return;
    const read = candidateValues(draft);
    if (!read.ok) {
      error = t("props.try.number", { field: t(FIELDS[read.field]) });
      result = null;
      return;
    }
    busy = true;
    error = "";
    try {
      result = await propCandidate(airframeId, read.candidate, read.massKg);
    } catch (e: unknown) {
      error = String(e);
      result = null;
    } finally {
      busy = false;
    }
  }

  const motor = $derived(view.measured.motor);
  const options = $derived(
    families.length ? families.map((f) => ({ id: f.id, label: f.label[app.locale] })) : draft ? [{ id: draft.family, label: draft.family }] : [],
  );
  const c = $derived(result?.candidate ?? null);
  const rows = $derived(c ? compareRows(c, result?.figures) : []);

  const n = (v: number | null | undefined, digits: number) => fixed(v, digits, app.locale);
  const u = (unit: string) => unitText(unit, app.locale);
  const withUnit = (v: number | null | undefined, digits: number, unit: string) => (v === null || v === undefined ? "–" : `${n(v, digits)} ${u(unit)}`);

  function cell(r: CompareRow, side: "flown" | "candidate"): string {
    const [a = null, b = null, z = null] = r[side];
    const spread = r.spread[side];
    const pm = spreadShown(spread, 0) ? ` ± ${n(spread * 100, 0)}%` : "";
    switch (r.id) {
      case "rpm":
        return `${withUnit(a, 0, "rpm")}${pm}`;
      case "shaft":
      case "electric":
        return a === null ? "–" : `${withUnit(a, 0, "W")}${pm}`;
      case "pack":
      case "packMost":
        return withUnit(a, 0, "A");
      case "esc":
        return withUnit(a, 0, "degC");
      case "hz":
        return withUnit(a, 0, "Hz");
      case "tip":
        return [a, b].filter((x) => x !== null).map((x) => n(x, 2)).join(" · ") || "–";
      case "most":
        return a === null ? "–" : `${withUnit(a, 1, "kg")} · ${n(b, 1)}`;
      case "clearance":
        return withUnit(a, 0, "mm");
      case "response":
        return withUnit(a, 0, "ms");
      case "figures":
        return a === null ? "–" : `${n(a, 1)} · ${withUnit(b, 1, "min")} · ${withUnit(z, 1, "km")}`;
    }
  }

  const limitText = (x: Check) => t(checkKey(x.check), { limit: n(x.limit, LIMIT_DIGITS[x.check]) });

  // What the figures rest on, in the order the mock-up says it: the anchor, how the
  // prediction is calibrated, any check it fails, the response, and what is missing.
  const notes = $derived.by(() => {
    const r = result;
    const k = r?.candidate;
    if (!r || !k) return [];
    const prop = propName(k.candidate_prop, app.locale);
    const out: string[] = [k.anchor ? t("props.anchor", { flight: tag(k.anchor.flight_id), kg: n(k.anchor.mass_kg, 1) }) : t("props.note.not_anchored")];
    for (const note of k.notes) if (note !== "not_anchored" && PREDICTION_NOTES.includes(note)) out.push(t(`props.note.${note}`, { prop }));
    const failed = k.checks.filter((x) => !x.ok && CHECKS.includes(x.check));
    if (failed.length) out.push(t("props.try.fails", { checks: failed.map((x) => t(failedKey(x.check))).join(", ") }));
    if (k.response) {
      out.push(t("props.response.measured", { flight: tag(k.response.candidate.flight_id), ms: n(k.response.flown?.response_ms, 0) }));
      for (const note of k.response.candidate.notes) if (RESPONSE_NOTES.includes(note)) out.push(t(`props.response.${note}`));
    }
    if (!r.motor) out.push(t("props.try.noMotor"));
    if (r.missing.figures) out.push(t("props.try.figuresMissing"));
    return out;
  });
</script>

<section class="panel" aria-labelledby="props-try-title">
  <div class="head">
    <h2 id="props-try-title">{t("props.try.title")}</h2>
    <span class="faint">{t("props.try.about")}</span>
  </div>

  {#if draft}
    <form
      class="form"
      aria-label={t("props.try.title")}
      oninput={() => (touched = true)}
      onsubmit={(e) => {
        e.preventDefault();
        touched = true;
        void predict();
      }}
    >
      <label class="family"
        >{t("props.try.family")}<select bind:value={draft.family}>
          {#each options as f (f.id)}<option value={f.id}>{f.label}</option>{/each}
        </select></label
      >
      <label class="short">{t("props.try.diameter")}<input type="text" inputmode="decimal" bind:value={draft.diameter_in} /></label>
      <label class="short">{t("props.try.pitch")}<input type="text" inputmode="decimal" bind:value={draft.pitch_in} /></label>
      <label class="short">{t("props.try.blades")}<input type="text" inputmode="numeric" bind:value={draft.blades} /></label>
      <label class="motor"
        >{t("props.try.motor")}<select bind:value={draft.motor}>
          <option value="flown">{motor ? t("props.try.motor.flown", { kv: n(motor.kv, 0) }) : t("props.try.motor.flownNone")}</option>
          <option value="other">{t("props.try.motor.other")}</option>
        </select></label
      >
      {#if draft.motor === "other"}
        <label class="short">{t("props.try.kv")}<input type="text" inputmode="decimal" bind:value={draft.kv} /></label>
        <label class="short">{t("props.try.resistance")}<input type="text" inputmode="decimal" bind:value={draft.resistance_mohm} /></label>
        <label class="opt"
          >{t("props.try.noLoad")}<input type="text" inputmode="decimal" placeholder={t("props.try.optional")} bind:value={draft.no_load_a} /></label
        >
      {/if}
      <label class="opt">{t("props.try.mass")}<input type="text" inputmode="decimal" placeholder={t("props.try.optional")} bind:value={draft.mass_g} /></label>
      <label class="short">{t("props.try.takeoff")}<input type="text" inputmode="decimal" bind:value={draft.mass_kg} /></label>
      <button type="submit" class="primary" disabled={busy}>{busy ? t("props.try.working") : t("props.try.predict")}</button>
    </form>
  {/if}

  {#if error}<p class="error" role="alert">{error}</p>{/if}

  {#if result && !c}
    <ul>
      {#each Object.entries(result.missing) as [k, why] (k)}<li>{k === "prop" ? t("props.missing.prop") : why}</li>{/each}
    </ul>
  {:else if result && c}
    <div class="scroll">
      <table aria-label={t("props.try.table")}>
        <thead>
          <tr>
            <th scope="col">{t("props.try.at", { kg: n(c.at.mass_kg, 1), air: t(result.inputs?.air === "standard" ? "props.air.standard" : "props.air.usual") })}</th>
            <th scope="col" class="num">{t("props.try.flown", { prop: propName(c.flown_prop, app.locale) })}{#if !c.at.flown.anchored}&nbsp;<TheoryPill />{/if}</th>
            <th scope="col" class="num">{propName(c.candidate_prop, app.locale)}&nbsp;<TheoryPill /></th>
            <th scope="col">{t("props.try.check")}</th>
          </tr>
        </thead>
        <tbody>
          {#each rows as r (r.id)}
            <tr>
              <td>{t(`props.row.${r.id}`)}</td>
              <td class="num mono">{cell(r, "flown")}</td>
              <td class="num mono">{cell(r, "candidate")}</td>
              <td class="mono check">
                {#if r.check}
                  {@const look = checkLook(r.check)}
                  <span class="mark" style:--tone="var({look.tone})" aria-hidden="true">{look.glyph}</span>
                  <span class="sr-only">{t(r.check.ok ? "props.check.ok" : "props.check.fails")}:</span>
                  {limitText(r.check)}
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    <p class="faint">{notes.join(" ")}</p>
  {/if}
</section>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 14px 16px;
    background: var(--surface);
    border: 1px solid var(--border-soft);
    border-radius: 12px;
    min-width: 0;
  }
  .head {
    display: flex;
    gap: 6px 12px;
    align-items: baseline;
    flex-wrap: wrap;
  }
  h2 {
    margin: 0;
    font-size: 15px;
  }
  p {
    margin: 0;
  }
  ul {
    margin: 0;
    padding-left: 18px;
    font-size: 13px;
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
  }
  .mono {
    font-family: var(--mono);
  }
  .error {
    color: var(--out);
    font-size: 13px;
  }
  .form {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 10px 12px;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 5px;
    font-size: 12px;
    color: var(--text-muted);
    min-width: 0;
  }
  .family {
    flex: 1 1 220px;
    max-width: 260px;
  }
  .motor {
    flex: 1 1 220px;
    max-width: 280px;
  }
  .short {
    flex: 0 1 110px;
  }
  .opt {
    flex: 0 1 140px;
  }
  input::placeholder {
    font-family: var(--font);
    color: var(--text-faint);
  }
  input,
  select {
    padding: 7px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-2);
    color: var(--text);
    font-size: 13px;
    font-family: inherit;
    min-width: 0;
  }
  input {
    font-family: var(--mono);
  }
  .primary {
    padding: 8px 14px;
    border: 0;
    border-radius: 8px;
    background: var(--accent);
    color: var(--accent-text);
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
  }
  .primary:disabled {
    opacity: 0.6;
    cursor: progress;
  }
  .scroll {
    position: relative;
    overflow-x: auto;
  }
  table {
    border-collapse: collapse;
    font-size: 13px;
    width: 100%;
  }
  th {
    text-align: left;
    font-weight: 500;
    color: var(--text-faint);
    padding: 6px 10px;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  td {
    padding: 7px 10px;
    border-bottom: 1px solid var(--border-soft);
  }
  .num {
    text-align: right;
    white-space: nowrap;
  }
  .check {
    white-space: nowrap;
  }
  .mark {
    display: inline-block;
    min-width: 1.4em;
    padding: 0 4px;
    margin-right: 4px;
    border-radius: 999px;
    border: 1px solid var(--tone);
    color: var(--tone);
    font-size: 11px;
    text-align: center;
    box-sizing: border-box;
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }
</style>
