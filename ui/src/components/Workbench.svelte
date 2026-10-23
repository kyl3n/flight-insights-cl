<script lang="ts">
  // The filter workbench for one flight (spec: User interface, "Filter workbench";
  // mock-up Workbench): the segment and data the analysis used, candidate filter
  // values, and what they would do, predicted on this flight's own gyro data before
  // anything is flown. Changes made here are tried, not stored: the plan holds what the
  // analysis recommends.
  import { app, t } from "../lib/app.svelte";
  import type { MessageKey } from "../lib/i18n";
  import { formatNumber, unitText } from "../lib/report";
  import { engineCall } from "../lib/shell";
  import { AXES, changes, delta, type Axis, type Bench } from "../lib/workbench";
  import SpectrogramView from "./SpectrogramView.svelte";
  import SpectrumPlot from "./SpectrumPlot.svelte";

  let { flightId }: { flightId: string } = $props();

  let bench = $state<Bench | null>(null);
  let error = $state("");
  let axis = $state<Axis>("roll");
  let edited = $state<Record<string, string>>({});
  let timer: ReturnType<typeof setTimeout> | undefined;

  const MODES: Record<number, MessageKey> = {
    0: "bench.mode.0", 1: "bench.mode.1", 2: "bench.mode.2", 3: "bench.mode.3", 4: "bench.mode.4", 5: "bench.mode.5",
  };

  async function fetchBench(candidate: Record<string, number>) {
    try {
      bench = await engineCall<Bench>("filter.workbench", { flight_id: flightId, candidate });
      error = "";
    } catch (e) {
      error = String(e);
    }
  }

  $effect(() => {
    void flightId;
    edited = {};
    bench = null;
    fetchBench({});
  });

  function edit(name: string, value: string) {
    edited = { ...edited, [name]: value };
    clearTimeout(timer);
    const flown = bench?.params ?? {};
    timer = setTimeout(() => fetchBench(changes(flown, edited)), 250); // a prediction per pause
  }

  function reset() {
    edited = {};
    fetchBench({});
  }

  const d = $derived(bench ? delta(bench) : null);
  // What the right-hand figures describe: the candidate when it could be evaluated.
  const e = $derived(bench?.candidate?.available ? bench.candidate : bench?.current);
  const tried = $derived(bench ? changes(bench.params, edited) : {});
  const n = (v: number, digits = 1) => formatNumber(Math.round(v * 10 ** digits) / 10 ** digits, app.locale);
  const u = (unit: string) => unitText(unit, app.locale);
  const signed = (v: number) => `${v > 0 ? "+" : ""}${n(v)}`;
</script>

{#if error}<p class="error">{error}</p>{/if}
{#if !bench && !error}<p class="faint">{t("report.analysing")}</p>{/if}

{#if bench}
  <div class="bench">
    <aside>
      <section class="panel">
        <h2>{t("bench.setup")}</h2>
        <dl>
          <dt>{t("bench.segment")}</dt>
          <dd>
            {t(`segment.${bench.segment}` as MessageKey)}
            {bench.spans_s.map(([a, b]) => `${n(a)}–${n(b)} ${u("s")}`).join(", ")}
          </dd>
          <dt>{t("bench.data")}</dt>
          <dd>{t("bench.data.value", { rate: Math.round(bench.rate_hz), seconds: n(bench.seconds) })}</dd>
          <dt>{t("bench.tracking")}</dt>
          <dd>
            {#each bench.tracking as tr (tr.set)}
              <span class="mono">{tr.set.replace(/_$/, "")}</span> {t(MODES[tr.mode] ?? "bench.mode.other")}
              {tr.model_centre_hz !== null ? `· ${n(tr.model_centre_hz)} ${u("Hz")}` : ""}<br />
            {:else}
              {t("bench.tracking.none")}
            {/each}
          </dd>
        </dl>
      </section>

      <section class="panel">
        <h2>{t("bench.candidate")}</h2>
        <p class="faint">{t("bench.candidate.about")}</p>
        {#each Object.entries(bench.params) as [name, flown] (name)}
          <label class="param">
            <span class="mono">{name}</span>
            <input
              type="text"
              inputmode="decimal"
              value={edited[name] ?? ""}
              placeholder={formatNumber(flown, app.locale)}
              oninput={(e) => edit(name, e.currentTarget.value)}
            />
            <span class="faint">{t("bench.flownValue", { value: formatNumber(flown, app.locale) })}</span>
          </label>
        {/each}
        <button type="button" class="link" disabled={!Object.keys(tried).length} onclick={reset}>{t("bench.reset")}</button>
      </section>

      <section class="panel">
        <h2>{Object.keys(tried).length ? t("bench.predictedWith") : t("bench.asFlown")}</h2>
        {#if bench.candidate && !bench.candidate.available}
          <p class="caution">{t("bench.unavailable")}</p>
        {/if}
        {#if e}
        <dl>
          <dt>{t("bench.noise", { low: bench.noise_band_hz[0], high: bench.noise_band_hz[1] })}</dt>
          <dd class="mono">{n(e.noise_db)} {u("dB")}{#if d} <span class:better={d.noise_db < 0} class:worse={d.noise_db > 0}>({signed(d.noise_db)})</span>{/if}</dd>
          <dt>{t("bench.lag", { hz: bench.lag_hz })}</dt>
          <dd class="mono">{n(e.lag_deg)}°{#if d} <span class:better={d.lag_deg < 0} class:worse={d.lag_deg > 0}>({signed(d.lag_deg)}°)</span>{/if}</dd>
          <dt>{t("bench.notches")}</dt>
          <dd class="mono" class:worse={!e.within_budget}>
            {e.notches}{bench.notch_cap !== null ? ` / ${bench.notch_cap}` : ""}
            {#if !e.within_budget}· {t("bench.overBudget")}{/if}
          </dd>
        </dl>
        {/if}
        <p class="faint">{t("bench.tradeoff")}</p>
      </section>
    </aside>

    <div class="plots">
      <section class="panel">
        <div class="row">
          <h2>{t("bench.spectrum", { axis: t(`bench.axis.${axis}`), segment: t(`segment.${bench.segment}` as MessageKey) })}</h2>
          <div role="group" aria-label={t("bench.axisPick")} class="axes">
            {#each AXES as a (a)}
              <button type="button" aria-pressed={axis === a} onclick={() => (axis = a)}>{t(`bench.axis.${a}`)}</button>
            {/each}
          </div>
        </div>
        <SpectrumPlot {bench} {axis} />
      </section>
      {#if bench.spectrogram}
        <section class="panel">
          <h2>{t("bench.spectrogram")}</h2>
          <SpectrogramView data={bench.spectrogram} spans={bench.spans_s} />
        </section>
      {/if}
    </div>
  </div>
{/if}

<style>
  .bench {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    align-items: flex-start;
  }
  aside {
    flex: 1 1 280px;
    max-width: 380px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .plots {
    flex: 3 1 520px;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .panel {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 14px 16px;
    background: var(--surface);
    border: 1px solid var(--border-soft);
    border-radius: 12px;
  }
  h2 {
    margin: 0;
    font-size: 14px;
  }
  p {
    margin: 0;
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
  }
  .error {
    color: var(--out);
  }
  .caution {
    color: var(--caution);
    font-size: 13px;
  }
  dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 4px 12px;
    margin: 0;
    font-size: 13px;
  }
  dt {
    color: var(--text-muted);
  }
  dd {
    margin: 0;
  }
  .better {
    color: var(--ok);
  }
  .worse {
    color: var(--out);
  }
  .param {
    display: grid;
    grid-template-columns: 1fr 90px;
    gap: 2px 8px;
    align-items: center;
    font-size: 13px;
  }
  .param .faint {
    grid-column: 1 / -1;
  }
  input {
    background: var(--surface-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 4px 6px;
    font-family: var(--mono);
    width: 100%;
    box-sizing: border-box;
  }
  .row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .axes {
    display: flex;
    gap: 4px;
  }
  .axes button {
    background: none;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text-2);
    padding: 3px 10px;
    cursor: pointer;
    font-size: 12px;
  }
  .axes button[aria-pressed="true"] {
    border-color: var(--accent);
    color: var(--text);
  }
  .link {
    align-self: flex-start;
    background: none;
    border: none;
    padding: 0;
    color: var(--link);
    cursor: pointer;
    font-size: 13px;
  }
  .link:disabled {
    color: var(--text-faint);
    cursor: default;
  }
</style>
