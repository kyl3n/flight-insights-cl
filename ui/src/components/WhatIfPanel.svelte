<script lang="ts">
  // What if, on the Range and speed screen (spec: Power and efficiency analysis, "Range,
  // endurance and speed"; mock-up Performance, "What if"): each lever alone at the weight
  // picked, and what it does to range and flight time, measured or the model's alone.
  import { app, t } from "../lib/app.svelte";
  import { whatIfShares, type Performance, type WhatIf } from "../lib/range";
  import { formatNumber, unitText } from "../lib/report";
  import type { MessageKey } from "../lib/i18n";

  let { view }: { view: Performance } = $props();

  const num = (v: number | null | undefined, digits = 1) =>
    v === null || v === undefined || !Number.isFinite(v) ? "–" : formatNumber(Math.round(v * 10 ** digits) / 10 ** digits, app.locale);
  const signed = (v: number, digits = 1) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${num(Math.abs(v), digits)}`;
  const unit = (u: string) => unitText(u, app.locale);
  const BAR = 0.1; // bars run to 10%

  const rows = $derived(view.figures && view.what_ifs ? view.what_ifs.map((w) => ({ w, s: whatIfShares(w, view.figures!) })) : []);

  function label(w: WhatIf): string {
    const pct = num(Math.abs((w.share ?? 0) * 100), 0);
    switch (w.lever) {
      case "mass":
        return t(w.change < 0 ? "whatif.mass.less" : "whatif.mass.more", { kg: num(Math.abs(w.change), 1), pct });
      case "drag":
        return t(w.change < 0 ? "whatif.drag.less" : "whatif.drag.more", { pct });
      case "avionics":
        return t("whatif.avionics", { w: num(Math.abs(w.change), 0) });
      case "capacity":
        return t("whatif.capacity", { ah: num(w.change, 1), pct });
    }
  }
  const width = (v: number) => `${Math.min(1, Math.abs(v) / BAR) * 100}%`;
</script>

{#if rows.length}
  <section class="panel" aria-labelledby="c8-title">
    <div class="head">
      <h2 id="c8-title">{t("whatif.title")}</h2>
      <span class="faint">{t("whatif.about", { kg: num(view.inputs?.mass_kg) })}</span>
    </div>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th scope="col">{t("whatif.col.change")}</th>
            <th scope="col">{t("whatif.col.range")}</th>
            <th scope="col">{t("whatif.col.time")}</th>
            <th scope="col" class="end">{t("whatif.col.from")}</th>
          </tr>
        </thead>
        <tbody>
          {#each rows as { w, s } (w.lever)}
            <tr>
              <td>
                {label(w)}<br /><span class="faint">{t(`whatif.why.${w.lever}.${w.basis}` as MessageKey)}</span>
              </td>
              <td class="cell">
                <span class="bar" aria-hidden="true"><span class="fill" class:down={s.range < 0} style:width={width(s.range)}></span></span>
                <span class="mono">{signed(s.range * 100)}%</span>
                <span class="mono faint">{signed(w.range_m / 1000, 2)} {unit("km")}</span>
              </td>
              <td class="cell">
                <span class="bar" aria-hidden="true"><span class="fill" class:down={s.endurance < 0} style:width={width(s.endurance)}></span></span>
                <span class="mono">{signed(s.endurance * 100)}%</span>
                <span class="mono faint">{Math.abs(w.endurance_s) < 60 ? `${signed(w.endurance_s, 0)} ${unit("s")}` : `${signed(w.endurance_s / 60)} ${unit("min")}`}</span>
              </td>
              <td class="end"><span class="tag" class:measured={w.basis === "measured"}>{t(`whatif.basis.${w.basis}` as MessageKey)}</span></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    <p class="faint">{t("whatif.note")}</p>
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
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
  }
  .mono {
    font-family: var(--mono);
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
    padding: 8px 12px 8px 0;
    border-top: 1px solid var(--border-soft);
    vertical-align: top;
  }
  .cell {
    white-space: nowrap;
  }
  .end {
    text-align: right;
  }
  .bar {
    position: relative;
    display: inline-block;
    width: 70px;
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
    background: var(--accent);
  }
  .fill.down {
    background: var(--caution);
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
  .tag.measured {
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    color: var(--link);
  }
</style>
