<script lang="ts">
  // The speed build-up on the Range and speed screen (spec: Power and efficiency analysis,
  // "The speed goal: building up to maximum speed safely"; mock-up Performance, "Speed
  // build-up"): the speed goal, which a person sets; the verified speed at this weight; the
  // next step with what the models and the fast runs predict there against each limit; and
  // what holds it back. The step itself is offered in the plan, as a file to review and
  // load; nothing is written to the aircraft.
  import { app, t } from "../lib/app.svelte";
  import type { MessageKey } from "../lib/i18n";
  import { CHECK_UNITS, used, type Performance } from "../lib/range";
  import { formatValue, formatNumber, unitText } from "../lib/report";
  import { engineCall } from "../lib/shell";
  import { STATUS } from "../lib/status";
  import StatusBadge from "./StatusBadge.svelte";

  let { view, airframeId, onchange }: { view: Performance; airframeId: string; onchange: () => void } = $props();

  let saving = $state(false);
  let error = $state("");

  const num = (v: number | null | undefined, digits = 1) =>
    v === null || v === undefined || !Number.isFinite(v) ? "–" : formatNumber(Math.round(v * 10 ** digits) / 10 ** digits, app.locale);
  const goal = $derived(view.inputs?.speed_goal ?? "efficient");
  const b = $derived(view.build_up);
  const checkName = (n: string) => t(`buildup.check.${n}` as MessageKey);

  async function setGoal(value: "efficient" | "fastest") {
    if (value === goal || saving) return;
    saving = true;
    error = "";
    try {
      const af = await engineCall<{ platform: { id: string } }>("airframe.get", { id: airframeId });
      const pl = await engineCall<{ id: string; reference_build: { version: number } }>("platform.get", { id: af.platform.id });
      await engineCall("platform.save", { id: pl.id, build_version: pl.reference_build.version, values: { speed_goal: value } });
      onchange();
    } catch (e) {
      error = String(e);
    } finally {
      saving = false;
    }
  }
</script>

<section class="panel" aria-labelledby="c11-title">
  <div class="head">
    <h2 id="c11-title">{t("buildup.title")}</h2>
    <div class="chips" role="group" aria-label={t("buildup.goal")}>
      {#each ["efficient", "fastest"] as const as g (g)}
        <button type="button" aria-pressed={goal === g} disabled={saving} onclick={() => setGoal(g)}>{t(`buildup.goal.${g}`)}</button>
      {/each}
    </div>
  </div>
  <p class="faint">{t(goal === "fastest" ? "buildup.about.fastest" : "buildup.about.efficient")}</p>
  {#if error}<p class="error">{error}</p>{/if}

  {#if goal === "fastest"}
    {#if !b}
      <p class="faint">{t("buildup.none")}</p>
    {:else if b.verified_ms === null}
      <p>{t("buildup.notVerified")}</p>
    {:else}
      <div class="tiles">
        <div class="tile">
          <span class="faint">{t("buildup.verified")}</span>
          <span class="big mono">{num(b.verified_ms)} {unitText("m/s", app.locale)}</span>
          <span class="faint">{t("buildup.verified.note", { kg: num(view.inputs?.mass_kg) })}</span>
        </div>
        <div class="tile">
          <span class="faint">{t("buildup.next")}</span>
          <span class="big mono">{num(b.next_ms)} {unitText("m/s", app.locale)}</span>
          {#if b.offered}
            <StatusBadge status="ok" label={t("buildup.offered")} />
          {:else}
            <StatusBadge status="caution" label={t("buildup.held", { by: b.binding ? checkName(b.binding) : "–" })} />
          {/if}
        </div>
      </div>
      {#if b.checks.length}
        <div class="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">{t("buildup.col.check")}</th>
                <th scope="col">{t("buildup.col.predicted")}</th>
                <th scope="col">{t("buildup.col.limit")}</th>
                <th scope="col">{t("buildup.col.margin")}</th>
                <th scope="col"><span class="sr">{t("buildup.col.status")}</span></th>
              </tr>
            </thead>
            <tbody>
              {#each b.checks as c (c.name)}
                {@const share = used(c)}
                <tr>
                  <td>{checkName(c.name)} <span class="faint">· {t(`buildup.how.${c.how}`)}</span></td>
                  <td class="mono">{formatValue(c.value, CHECK_UNITS[c.name] ?? "", app.locale)}</td>
                  <td class="mono">{formatValue(c.limit, CHECK_UNITS[c.name] ?? "", app.locale)}</td>
                  <td>
                    <span class="bar" aria-hidden="true"><span class="fill" style:width="{Math.min(1, share) * 100}%" style:background="var({STATUS[c.holds ? (share > 0.85 ? "caution" : "ok") : "out"].tone})"></span></span>
                    <span class="mono faint">{num(share * 100, 0)}%</span>
                  </td>
                  <td class="end"><StatusBadge status={c.holds ? "ok" : "out"} label={t(c.holds ? "buildup.holds" : "buildup.fails")} /></td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
      {#if b.missing.length}
        <p class="faint">{t("buildup.missing", { list: b.missing.map(checkName).join(", ") })}</p>
      {/if}
      <p class="faint">{t("buildup.plan")}</p>
    {/if}
  {/if}
</section>

<style>
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
  .head {
    display: flex;
    gap: 8px 14px;
    align-items: center;
    flex-wrap: wrap;
  }
  h2 {
    margin: 0;
    font-size: 15px;
  }
  p {
    margin: 0;
    font-size: 13px;
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
  }
  .error {
    color: var(--out);
  }
  .mono {
    font-family: var(--mono);
  }
  .chips {
    display: flex;
    gap: 6px;
  }
  .chips button {
    padding: 5px 10px;
    border-radius: 7px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text-muted);
    font: inherit;
    font-size: 13px;
    cursor: pointer;
  }
  .chips button[aria-pressed="true"] {
    border-color: color-mix(in srgb, var(--accent) 50%, var(--border));
    background: color-mix(in srgb, var(--accent) 14%, transparent);
    color: var(--text);
  }
  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 10px;
  }
  .tile {
    padding: 12px 14px;
    border: 1px solid var(--border-soft);
    border-radius: 10px;
    background: var(--surface-2);
    display: flex;
    flex-direction: column;
    gap: 6px;
    align-items: flex-start;
  }
  .big {
    font-size: 20px;
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
    color: var(--text-muted);
    padding: 6px 12px 6px 0;
    white-space: nowrap;
  }
  td {
    padding: 6px 12px 6px 0;
    border-top: 1px solid var(--border-soft);
  }
  td.end {
    text-align: right;
  }
  .bar {
    position: relative;
    display: inline-block;
    width: 100px;
    height: 6px;
    border-radius: 3px;
    background: var(--border-soft);
    margin-right: 6px;
    vertical-align: middle;
  }
  .fill {
    position: absolute;
    inset: 0 auto 0 0;
    border-radius: 3px;
  }
  .sr {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
  }
</style>
