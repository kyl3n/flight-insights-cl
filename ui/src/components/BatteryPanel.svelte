<script lang="ts">
  // Batteries on the Range and speed screen (spec: Power and efficiency analysis, "The
  // battery's usable energy"; mock-up Performance, "Batteries"): the pack's energy down to
  // the reserve against a steady load, with the loads the figures fly at; and each of the
  // airframe's packs' capacity and resistance across the flights it flew alone.
  import { app, t } from "../lib/app.svelte";
  import { packShares, plot, split, type Performance } from "../lib/range";
  import { formatNumber, unitText } from "../lib/report";
  import type { MessageKey } from "../lib/i18n";

  let { view }: { view: Performance } = $props();

  const num = (v: number | null | undefined, digits = 1) =>
    v === null || v === undefined || !Number.isFinite(v) ? "–" : formatNumber(Math.round(v * 10 ** digits) / 10 ** digits, app.locale);
  const unit = (u: string) => unitText(u, app.locale);
  const TONES = ["--accent", "--ai", "--info", "--link"];
  const tone = (i: number) => `var(${TONES[i % TONES.length]})`;

  const F = { width: 640, height: 230, left: 50, right: 16, top: 16, bottom: 32 };
  const S = { width: 300, height: 160, left: 44, right: 10, top: 12, bottom: 28 };
  const b = $derived(view.battery);
  const kw = $derived((b?.usable ?? []).map((u) => u.power_w / 1000));
  const wh = $derived((b?.usable ?? []).map((u) => u.energy_wh));
  const p = $derived(plot(kw, wh, F));
  const line = $derived(split(kw, wh, Infinity, p).solid);
  // where the pack's energy stops being the reserve's: something ends it sooner
  const early = $derived((b?.usable ?? []).find((u) => u.ends !== "reserve") ?? null);
  const loads = $derived(
    b ? (["hover", "endurance", "range"] as const).map((k) => ({ k, kw: b.loads[k] / 1000, wh: atLoad(b.loads[k] / 1000) })) : [],
  );
  function atLoad(x: number): number | null {
    for (let i = 1; i < kw.length; i++) {
      if (x <= kw[i]!) {
        const f = (x - kw[i - 1]!) / (kw[i]! - kw[i - 1]!);
        return wh[i - 1]! + f * (wh[i]! - wh[i - 1]!);
      }
    }
    return null;
  }

  const shares = $derived((b?.packs ?? []).map((pk) => ({ pack: pk, ...packShares(pk) })));
  const most = $derived(Math.max(2, ...shares.map((s) => s.pack.flights.length)));
  const flightsAxis = $derived(Array.from({ length: most }, (_, i) => i + 1));
  const capValues = $derived(shares.flatMap((s) => s.capacity.filter((v): v is number => v !== null).map((v) => v * 100)));
  const capPlot = $derived(plot(flightsAxis, [...capValues, 100, (b?.aged_capacity ?? 0.8) * 100 - 5], S, null));
  const resPlot = $derived(plot(flightsAxis, [...shares.flatMap((s) => s.resistance.map((v) => v * 100)), 100, (b?.aged_resistance ?? 2) * 100], S, null));
  const anyCapacity = $derived(capValues.length > 0);
  const worn = $derived(shares.filter((s) => {
    const c = s.capacity.filter((v): v is number => v !== null);
    const last = c[c.length - 1];
    const r = s.resistance[s.resistance.length - 1];
    return (last !== undefined && last < (b?.aged_capacity ?? 0.8)) || (r !== undefined && s.resistance.length > 1 && r >= (b?.aged_resistance ?? 2));
  }));
</script>

{#if b}
  <section class="panel" aria-labelledby="c6-title">
    <div class="head">
      <h2 id="c6-title">{t("battery.title")}</h2>
      <span class="faint">{t("battery.about")}</span>
    </div>
    <div class="split">
      <figure class="main">
        <figcaption>
          <span class="cap">{t("battery.usable")}</span>
          <span><span aria-hidden="true" class="pack">━</span> {t("battery.usable.line")}</span>
          <span><span aria-hidden="true" class="load">┆</span> {t("battery.usable.loads")}</span>
        </figcaption>
        <svg viewBox="0 0 {F.width} {F.height}" role="img" aria-label={t("battery.usable.aria", { hover: num(loads[0]?.wh, 0) })}>
          {#each p.y.ticks as v (v)}
            <line class="grid" x1={F.left} x2={F.width - F.right} y1={p.py(v)} y2={p.py(v)} />
            <text class="tick" x={F.left - 8} y={p.py(v) + 4} text-anchor="end">{num(v, 0)}</text>
          {/each}
          {#each p.x.ticks as v (v)}
            <text class="tick" x={p.px(v)} y={F.height - F.bottom + 16} text-anchor="middle">{num(v, 1)}</text>
          {/each}
          <text class="tick" x={4} y={10}>{unit("W.h")}</text>
          <text class="tick" x={F.width - F.right} y={F.height - 2} text-anchor="end">{unit("kW")}</text>
          {#each loads as l (l.k)}
            <line class="loadline" x1={p.px(l.kw)} x2={p.px(l.kw)} y1={F.top} y2={F.height - F.bottom} />
          {/each}
          <path class="line packline" d={line} />
          {#each loads as l (l.k)}
            {#if l.wh !== null}<circle class="dot" cx={p.px(l.kw)} cy={p.py(l.wh)} r="3.5" />{/if}
          {/each}
        </svg>
        <ul class="loads">
          {#each loads as l (l.k)}
            <li>
              <span class="faint">{t(`battery.load.${l.k}` as MessageKey)}</span>
              <span class="mono">{num(l.kw, 2)} {unit("kW")} · {num(l.wh, 0)} {unit("W.h")}</span>
            </li>
          {/each}
        </ul>
        <p class="faint">
          {#if early && early === b.usable[0] && (early.ends === "capacity_failsafe" || early.ends === "voltage_failsafe")}
            {t("battery.usable.early.all", { why: t(`range.ends.${early.ends}` as MessageKey) })}
          {:else if early}
            {t("battery.usable.early", { kw: num(early.power_w / 1000, 1), why: t(`range.ends.${early.ends}` as MessageKey) })}
          {:else}
            {t("battery.usable.note")}
          {/if}
        </p>
      </figure>
    </div>

    {#if shares.length}
      <div class="split">
        {#if anyCapacity}
          <figure class="small">
            <figcaption><span class="cap">{t("battery.capacity")}</span></figcaption>
            <svg viewBox="0 0 {S.width} {S.height}" role="img" aria-label={t("battery.capacity.aria")}>
              {#each capPlot.y.ticks as v (v)}
                <line class="grid" x1={S.left} x2={S.width - S.right} y1={capPlot.py(v)} y2={capPlot.py(v)} />
                <text class="tick" x={S.left - 6} y={capPlot.py(v) + 4} text-anchor="end">{num(v, 0)}%</text>
              {/each}
              {#each capPlot.x.ticks as v (v)}
                <text class="tick" x={capPlot.px(v)} y={S.height - S.bottom + 16} text-anchor="middle">{num(v, 0)}</text>
              {/each}
              <line class="limit" x1={S.left} x2={S.width - S.right} y1={capPlot.py(b.aged_capacity * 100)} y2={capPlot.py(b.aged_capacity * 100)} />
              {#each shares as s, i (s.pack.code)}
                <path class="line" style:stroke={tone(i)} d={split(flightsAxis.slice(0, s.capacity.length), s.capacity.map((v) => (v === null ? null : v * 100)), Infinity, capPlot).solid} />
                {#each s.capacity as v, j (j)}
                  {#if v !== null}<circle cx={capPlot.px(j + 1)} cy={capPlot.py(v * 100)} r="3" style:fill={tone(i)} class="mark" />{/if}
                {/each}
              {/each}
            </svg>
          </figure>
        {/if}
        <figure class="small">
          <figcaption><span class="cap">{t("battery.resistance")}</span></figcaption>
          <svg viewBox="0 0 {S.width} {S.height}" role="img" aria-label={t("battery.resistance.aria")}>
            {#each resPlot.y.ticks as v (v)}
              <line class="grid" x1={S.left} x2={S.width - S.right} y1={resPlot.py(v)} y2={resPlot.py(v)} />
              <text class="tick" x={S.left - 6} y={resPlot.py(v) + 4} text-anchor="end">{num(v, 0)}%</text>
            {/each}
            {#each resPlot.x.ticks as v (v)}
              <text class="tick" x={resPlot.px(v)} y={S.height - S.bottom + 16} text-anchor="middle">{num(v, 0)}</text>
            {/each}
            <line class="limit" x1={S.left} x2={S.width - S.right} y1={resPlot.py(b.aged_resistance * 100)} y2={resPlot.py(b.aged_resistance * 100)} />
            {#each shares as s, i (s.pack.code)}
              <path class="line" style:stroke={tone(i)} d={split(flightsAxis.slice(0, s.resistance.length), s.resistance.map((v) => v * 100), Infinity, resPlot).solid} />
              {#each s.resistance as v, j (j)}
                <circle cx={resPlot.px(j + 1)} cy={resPlot.py(v * 100)} r="3" style:fill={tone(i)} class="mark" />
              {/each}
            {/each}
          </svg>
        </figure>
        <div class="legend">
          <h3>{t("battery.packs")}</h3>
          <ul>
            {#each shares as s, i (s.pack.code)}
              <li>
                <span aria-hidden="true" style:color={tone(i)}>━</span>
                <span class="mono">{s.pack.code}</span>
                <span class="faint">{t("battery.pack.flights", { n: s.pack.flights.length, mah: num(s.pack.rated_mah, 0) })}</span>
              </li>
            {/each}
          </ul>
          <p class="faint">
            {t("battery.limits", { cap: num(b.aged_capacity * 100, 0), res: num(b.aged_resistance * 100, 0) })}
            {#if !anyCapacity}{t("battery.noCapacity")}{/if}
          </p>
          {#if worn.length}
            <p class="worn">{t("battery.worn", { packs: worn.map((s) => s.pack.code).join(", ") })}</p>
          {/if}
        </div>
      </div>
    {:else}
      <p class="faint">{t("battery.noPacks")}</p>
    {/if}
  </section>
{/if}

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
  h3 {
    margin: 0 0 6px;
    font-size: 13px;
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
  .split {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    align-items: flex-start;
  }
  figure {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
  }
  .main {
    flex: 1 1 400px;
    max-width: 720px;
  }
  .small {
    flex: 1 1 240px;
    max-width: 360px;
  }
  .legend {
    flex: 1 1 200px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 13px;
  }
  ul {
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .loads {
    flex-direction: row;
    flex-wrap: wrap;
    gap: 4px 18px;
    font-size: 13px;
  }
  .loads li,
  .legend li {
    display: flex;
    gap: 6px;
    align-items: baseline;
    flex-wrap: wrap;
  }
  figcaption {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 14px;
    font-size: 12px;
    color: var(--text-muted);
  }
  .cap {
    color: var(--text);
    font-weight: 500;
  }
  svg {
    width: 100%;
    height: auto;
    display: block;
  }
  .grid {
    stroke: var(--border-soft);
  }
  .tick {
    fill: var(--text-faint);
    font-family: var(--mono);
    font-size: 11px;
  }
  .line {
    fill: none;
    stroke-width: 2;
  }
  .packline,
  .pack {
    stroke: var(--accent);
    color: var(--accent);
  }
  .loadline {
    stroke: var(--text-faint);
    stroke-dasharray: 2 3;
  }
  .load {
    color: var(--text-faint);
  }
  .dot {
    fill: var(--surface);
    stroke: var(--text);
    stroke-width: 1.6;
  }
  .mark {
    stroke: var(--surface);
    stroke-width: 1;
  }
  .limit {
    stroke: var(--caution);
    stroke-dasharray: 4 4;
    stroke-opacity: 0.8;
  }
  .worn {
    color: var(--caution);
    font-size: 13px;
  }
</style>
