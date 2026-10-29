<script lang="ts">
  // The data behind the figures, on the Range and speed screen (spec: Power and efficiency
  // analysis, "Range, endurance and speed"; mock-up Performance, "The data behind these
  // figures"): the minutes of steady flight the model was fitted on, by weight and
  // airspeed, how well it predicts each flight it was not given, and what it is still missing.
  import { app, t } from "../lib/app.svelte";
  import { minutesGrid, type Performance } from "../lib/range";
  import { formatNumber, unitText } from "../lib/report";
  import type { MessageKey } from "../lib/i18n";

  let { view }: { view: Performance } = $props();

  const num = (v: number | null | undefined, digits = 1) =>
    v === null || v === undefined || !Number.isFinite(v) ? "–" : formatNumber(Math.round(v * 10 ** digits) / 10 ** digits, app.locale);
  const unit = (u: string) => unitText(u, app.locale);

  const d = $derived(view.data);
  const g = $derived(d ? minutesGrid(d) : null);
  const most = $derived(d ? Math.max(0.01, ...d.grid.map((c) => c.minutes)) : 1);
  const medianOk = $derived(d && d.lofo_median !== null ? d.lofo_median <= d.targets.median : null);
  const p90Ok = $derived(d && d.lofo_p90 !== null ? d.lofo_p90 <= d.targets.p90 : null);
  const skipped = $derived(d ? Object.values(d.skipped) : []);
</script>

{#if d && g}
  <section class="panel" aria-labelledby="ds-title">
    <div class="head">
      <h2 id="ds-title">{t("data.title")}</h2>
      <span class="faint">{t("data.about", { flights: d.flights, rows: d.rows })}</span>
    </div>
    <div class="split">
      <div class="block grid-block">
        <span class="cap">{t("data.grid")}</span>
        <div class="scroll">
          <table aria-label={t("data.grid.aria")}>
            <thead>
              <tr>
                <th scope="col"><span class="faint">{unit("kg")}</span></th>
                {#each g.speeds as v (v)}
                  <th scope="col" class="mono">{v === 0 ? t("data.hover") : num(v, 0)}</th>
                {/each}
              </tr>
            </thead>
            <tbody>
              {#each g.rows as r (r.mass_kg)}
                <tr>
                  <th scope="row" class="mono">{r.mass_kg === null ? t("data.unweighed") : num(r.mass_kg)}</th>
                  {#each r.cells as c, i (i)}
                    <td class="mono cell" style:background={c === null ? undefined : `color-mix(in srgb, var(--accent) ${Math.round(10 + 40 * (c / most))}%, transparent)`}>
                      {c === null ? "·" : c < 0.05 ? `<${num(0.1)}` : num(c)}
                    </td>
                  {/each}
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
        <span class="faint">{t("data.grid.note", { sloped: num(d.sloped_minutes) })}</span>
      </div>
      <div class="block">
        <span class="cap">{t("data.lofo")}</span>
        <ul>
          <li>
            <span class="faint">{t("data.lofo.median")}</span>
            <span class="mono" class:bad={medianOk === false}>{num((d.lofo_median ?? NaN) * 100)}%</span>
            <span class="faint">{t("data.lofo.target", { v: num(d.targets.median * 100, 0) })}</span>
          </li>
          <li>
            <span class="faint">{t("data.lofo.p90")}</span>
            <span class="mono" class:bad={p90Ok === false}>{num((d.lofo_p90 ?? NaN) * 100)}%</span>
            <span class="faint">{t("data.lofo.target", { v: num(d.targets.p90 * 100, 0) })}</span>
          </li>
        </ul>
        <p class="faint">{t(medianOk && p90Ok ? "data.lofo.ok" : "data.lofo.short")}</p>
        {#if d.level}
          <p><span class="tag">{t(`data.level.${d.level}` as MessageKey)}</span></p>
        {/if}
        {#if d.left_out}<p class="faint">{t("data.leftOut", { n: d.left_out })}</p>{/if}
        {#if skipped.length}
          <p class="faint">{t("data.skipped", { n: skipped.length })}</p>
          <ul class="why">
            {#each [...new Set(skipped)] as why (why)}<li class="faint">{why}</li>{/each}
          </ul>
        {/if}
      </div>
    </div>
  </section>
{/if}

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
    font-size: 13px;
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
  }
  .mono {
    font-family: var(--mono);
  }
  .cap {
    color: var(--text);
    font-weight: 500;
    font-size: 12px;
  }
  .split {
    display: flex;
    flex-wrap: wrap;
    gap: 16px 28px;
    align-items: flex-start;
  }
  .block {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 0;
    flex: 1 1 260px;
  }
  .grid-block {
    flex: 3 1 420px;
  }
  .scroll {
    position: relative;
    overflow-x: auto;
  }
  table {
    border-collapse: separate;
    border-spacing: 2px;
    font-size: 12px;
  }
  th {
    font-weight: 500;
    color: var(--text-muted);
    padding: 3px 6px;
    text-align: right;
    white-space: nowrap;
  }
  td.cell {
    padding: 3px 6px;
    text-align: right;
    border-radius: 3px;
    color: var(--text-2);
    min-width: 30px;
  }
  ul {
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 13px;
  }
  li {
    display: flex;
    gap: 8px;
    align-items: baseline;
    flex-wrap: wrap;
  }
  .why {
    padding-left: 12px;
  }
  .bad {
    color: var(--caution);
  }
  .tag {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 6px;
    font-size: 12px;
    background: var(--surface-2);
    color: var(--text-2);
    border: 1px solid var(--border-soft);
  }
</style>
