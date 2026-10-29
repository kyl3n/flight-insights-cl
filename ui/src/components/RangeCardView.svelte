<script lang="ts">
  // The printable range card (spec: Power and efficiency analysis, "Range, endurance and
  // speed"; mock-up RangeCard): for each take-off weight, how long, how far and how fast,
  // the radius for each standard trip in calm air and the rules' winds, the ground speed to
  // set into a headwind, the reserve in every figure, and what the card rests on. On a
  // white page that prints alone. No position is on it.
  import { app, t } from "../lib/app.svelte";
  import type { MessageKey } from "../lib/i18n";
  import type { Air } from "../lib/range";
  import { meetsTargets, radiusAt, rangeCard, type RangeCard } from "../lib/rangecard";
  import { formatNumber, unitText } from "../lib/report";
  import type { AirframeView } from "../lib/setup";
  import { engineCall } from "../lib/shell";

  let { airframeId }: { airframeId: string } = $props();

  let air = $state<Air>("usual");
  let card = $state<RangeCard | null>(null);
  let airframe = $state<AirframeView | null>(null);
  let error = $state("");
  let busy = $state(false);

  $effect(() => {
    const id = airframeId;
    engineCall<AirframeView>("airframe.get", { id })
      .then((a) => (airframe = a))
      .catch(() => (airframe = null));
  });
  $effect(() => {
    const id = airframeId, picked = air;
    busy = true;
    error = "";
    rangeCard(id, picked)
      .then((c) => (card = c))
      .catch((e: unknown) => (error = String(e)))
      .finally(() => (busy = false));
  });

  const num = (v: number | null | undefined, digits = 1) =>
    v === null || v === undefined || !Number.isFinite(v) ? "–" : formatNumber(Math.round(v * 10 ** digits) / 10 ** digits, app.locale);
  const unit = (u: string) => unitText(u, app.locale);
  const kg = (v: number | null | undefined) => `${num(v)} ${unit("kg")}`;
  const ms = (v: number | null | undefined) => (v === null || v === undefined ? "–" : `${num(v)} ${unit("m/s")}`);
  const mins = (v: number | null | undefined) => (v === null || v === undefined ? "–" : `${num(v, 0)} ${unit("min")}`);

  const c = $derived(card?.card);
  const rows = $derived(card?.rows ?? []);
  const flying = $derived(rows.filter((r) => r.range_km !== undefined));
  const printed = $derived(new Date().toLocaleDateString(app.locale === "uk" ? "uk-UA" : "en-GB", { day: "numeric", month: "short", year: "numeric" }));
  const ok = $derived(card ? meetsTargets(card) : null);
  const name = $derived(airframe?.name ?? "");
  const payload = (m: number) => (c?.empty_kg !== null && c?.empty_kg !== undefined ? m - c.empty_kg : null);
  const reservePct = $derived(num((card?.valid?.reserve_share ?? 0) * 100, 0));
  // A radius that rounds to nothing is a trip that cannot be flown with the reserve kept.
  const radius = (v: number | null) => (v === null || v < 0.05 ? "–" : num(v));
  // Zero means something in these parameters: the return speed follows WP_SPD, and a zero
  // failsafe threshold is switched off (ArduPilot 4.7 parameter docs).
  const OFF_AT_ZERO = new Set(["BATT_LOW_MAH", "BATT_LOW_VOLT"]);
  const paramText = (k: string, v: number | null) => {
    if (v === null) return t("card.notSet");
    if (v === 0 && k === "RTL_SPEED_MS") return `0 (${t("card.param.wpSpd")})`;
    if (v === 0 && OFF_AT_ZERO.has(k)) return `0 (${t("card.off")})`;
    return num(v, 2);
  };
</script>

<main class="rangecard">
  <div class="head no-print">
    <h1>{t("card.title", { airframe: name })}</h1>
    <p class="faint">{t("card.subtitle")}</p>
  </div>
  {#if error}<p class="error no-print">{error}</p>{/if}
  {#if busy && !card}<p class="faint no-print">{t("range.working")}</p>{/if}

  {#if card}
    <div class="layout">
      <article class="paper print-card" aria-label={t("card.title", { airframe: name })}>
        <header>
          <h1>{t("card.title", { airframe: name })}</h1>
          <span class="sub">{airframe ? `${airframe.platform.name} · ${t("card.build", { n: airframe.build.number })} · ` : ""}{t("card.printed", { date: printed })}</span>
          <span class="sub">{t("card.valid")}</span>
        </header>

        {#if Object.keys(card.missing).length && !rows.length}
          <ul class="missing">
            {#each Object.entries(card.missing) as [k, v] (k)}<li>{v}</li>{/each}
          </ul>
        {/if}

        {#if c}
          <div class="facts">
            <span><span class="muted">{t("card.pack")}</span> {card.sources.pack ?? "–"}</span>
            <span><span class="muted">{t("card.air")}</span> {c.air === "standard" ? t("card.air.standard") : t("card.air.usual", { ratio: num(c.density / 1.225, 2) })}</span>
            <span><span class="muted">{t("card.cruise")}</span> {t(c.priority === "endurance" ? "card.cruise.endurance" : "card.cruise.range")}</span>
            {#if c.empty_kg !== null}<span><span class="muted">{t("card.empty")}</span> {kg(c.empty_kg)}</span>{/if}
          </div>
        {/if}

        <section>
          <h2>{t("card.times")}</h2>
          <div class="scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("card.col.weight")}</th>
                  <th scope="col">{t("card.col.hover")}</th>
                  <th scope="col">{t("card.col.longest")}</th>
                  <th scope="col">{t("card.col.cruise")}</th>
                  <th scope="col">{t("card.col.range")}</th>
                  <th scope="col">{t("card.col.max")}</th>
                  <th scope="col">{t("card.col.verified")}</th>
                  <th scope="col">{t("card.col.from")}</th>
                </tr>
              </thead>
              <tbody>
                {#each rows as r (r.mass_kg)}
                  <tr>
                    <th scope="row">
                      {kg(r.mass_kg)}
                      {#if payload(r.mass_kg) !== null}<br /><span class="muted small">{t("card.payload", { kg: num(payload(r.mass_kg)) })}</span>{/if}
                    </th>
                    {#if r.hovers}
                      <td>{mins(r.hover_min)}</td>
                      <td>{mins(r.endurance_min)}{#if r.endurance_ms !== undefined}<br /><span class="muted small">{ms(r.endurance_ms)}</span>{/if}</td>
                      <td>{ms(r.cruise_ms)}</td>
                      <td>{r.range_km !== undefined ? `${num(r.range_km)} ${unit("km")}` : "–"}</td>
                      <td>
                        {ms(r.top_speed_ms)}
                        {#if r.top_speed_by}<br /><span class="muted small">{t(`range.tile.top.by.${r.top_speed_by}` as MessageKey)}</span>{/if}
                      </td>
                      <td>{r.verified_ms ? ms(r.verified_ms) : t("card.notVerified")}</td>
                      <td>{t(r.measured ? "card.measured" : "card.estimate")}</td>
                    {:else}
                      <td colspan="7" class="muted">{t("card.noHover")}</td>
                    {/if}
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
          <p class="note">{t("card.times.note")}</p>
        </section>

        {#if c && flying.length}
          <section>
            <h2>{t("card.radius")}</h2>
            <div class="scroll">
              <table>
                <thead>
                  <tr>
                    <th scope="col" rowspan="2">{t("card.col.weight")}</th>
                    {#each c.trips as trip (trip)}<th scope="colgroup" colspan={c.winds.length}>{t(`range.trip.${trip}` as MessageKey)}</th>{/each}
                  </tr>
                  <tr>
                    {#each c.trips as trip (trip)}
                      {#each c.winds as w (w)}<th scope="col" class="wind">{w === 0 ? t("card.calm") : `${num(w, 0)} ${unit("m/s")}`}</th>{/each}
                    {/each}
                  </tr>
                </thead>
                <tbody>
                  {#each flying as r (r.mass_kg)}
                    <tr>
                      <th scope="row">{kg(r.mass_kg)}</th>
                      {#each c.trips as trip (trip)}
                        {#each c.winds as w (w)}<td>{radius(radiusAt(r, trip, w))}</td>{/each}
                      {/each}
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
            <p class="note">{t("card.radius.note")} {t("card.cannot")}</p>
          </section>

          <div class="two">
            <section>
              <h2>{t("card.headwind")}</h2>
              <table>
                <thead>
                  <tr>
                    <th scope="col">{t("card.col.weight")}</th>
                    {#each c.winds as w (w)}<th scope="col">{w === 0 ? t("card.calm") : `${num(w, 0)} ${unit("m/s")}`}</th>{/each}
                  </tr>
                </thead>
                <tbody>
                  {#each flying as r (r.mass_kg)}
                    <tr>
                      <th scope="row">{kg(r.mass_kg)}</th>
                      {#each r.headwind ?? [] as h (h.wind_ms)}
                        <td>{num(h.ground)}<br /><span class="muted small">{t("card.through", { v: num(h.airspeed) })}</span></td>
                      {/each}
                    </tr>
                  {/each}
                </tbody>
              </table>
              <p class="note">{t("card.headwind.note")}</p>
            </section>

            <section>
              <h2>{t("card.reserve")}</h2>
              <p class="text">
                {t("card.reserve.text", {
                  pct: reservePct,
                  mah: c.failsafe.low_mah !== null ? `${num(c.failsafe.low_mah, 0)} ${unit("mAh")}` : t("card.off"),
                  volt: c.failsafe.low_volt !== null ? `${num(c.failsafe.low_volt)} ${unit("V")}` : t("card.off"),
                })}
              </p>
              <table>
                <tbody>
                  {#each flying as r (r.mass_kg)}
                    <tr><th scope="row">{t("card.plan", { kg: num(r.mass_kg) })}</th><td>{num(r.plan_wh, 0)} {unit("Wh")}</td></tr>
                  {/each}
                </tbody>
              </table>
            </section>
          </div>
        {/if}

        {#if c}
          <div class="two">
            <section>
              <h2>{t("card.params")}</h2>
              <table>
                <tbody>
                  {#each Object.entries(c.params) as [k, v] (k)}
                    <tr><td class="mono">{k}</td><td class="mono">{paramText(k, v)}</td></tr>
                  {/each}
                </tbody>
              </table>
              <p class="note">{t("card.params.note")}</p>
            </section>
            <section>
              <h2>{t(ok ? "card.meetsTargets" : "card.estimateOnly")}</h2>
              <ul>
                {#if card.valid?.flown_kg}<li>{t("card.flown", { lo: num(card.valid.flown_kg[0]), hi: num(card.valid.flown_kg[1]), top: num(card.valid.top_ms) })}</li>{/if}
                <li>
                  {t("card.model", {
                    level: c.model.level ?? "–",
                    flights: c.model.flights,
                    median: num((c.model.lofo_median ?? NaN) * 100, 1),
                    p90: num((c.model.lofo_p90 ?? NaN) * 100, 1),
                  })}
                </li>
                <li>{t("card.targets", { median: num(c.model.targets.median * 100, 0), p90: num(c.model.targets.p90 * 100, 0) })}</li>
              </ul>
            </section>
          </div>
        {/if}

        <footer>
          <span>{t("card.private")}</span>
          <span>{t("card.footer", { airframe: name })}</span>
        </footer>
      </article>

      <aside class="panel no-print" aria-labelledby="print-title">
        <h2 id="print-title">{t("recovery.print.title")}</h2>
        <div class="control">
          <span class="muted">{t("range.air")}</span>
          <div class="chips" role="group" aria-label={t("range.air")}>
            {#each ["usual", "standard"] as const as a (a)}
              <button type="button" aria-pressed={air === a} onclick={() => (air = a)}>{t(`range.air.${a}`)}</button>
            {/each}
          </div>
        </div>
        <div class="kv">
          <span class="muted">{t("recovery.print.paper")}</span><span>{t("card.paper")}</span>
          <span class="muted">{t("card.from")}</span><span>{t("card.from.value", { flights: c?.model.flights ?? 0 })}</span>
        </div>
        <p class="faint">{t("card.print.about")}</p>
        <div class="actions">
          <button type="button" class="primary" onclick={() => window.print()}>{t("recovery.print")}</button>
          <button type="button" onclick={() => (app.rangeCard = null)}>{t("recovery.back")}</button>
        </div>
      </aside>
    </div>
  {/if}
</main>

<style>
  main {
    display: flex;
    flex-direction: column;
    gap: 14px;
    padding: 20px 24px 40px;
    min-width: 0;
  }
  h1 {
    margin: 0;
    font-size: 20px;
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
    margin: 0;
  }
  .muted {
    color: #4a535d;
  }
  .error {
    color: var(--out);
  }
  .layout {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    align-items: flex-start;
  }
  .paper {
    flex: 1 1 720px;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 14px;
    padding: 24px 28px;
    border-radius: 12px;
    background: #ffffff;
    color: #0e1114;
    font-size: 13px;
  }
  .paper header {
    display: flex;
    flex-direction: column;
    gap: 2px;
    border-bottom: 2px solid #0e1114;
    padding-bottom: 8px;
  }
  .paper h1 {
    font-size: 20px;
  }
  .paper h2 {
    margin: 0 0 6px;
    font-size: 14px;
  }
  .sub {
    font-size: 12px;
    color: #4a535d;
  }
  .facts {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 20px;
  }
  .scroll {
    position: relative;
    overflow-x: auto;
  }
  table {
    border-collapse: collapse;
    width: 100%;
  }
  th,
  td {
    text-align: left;
    padding: 5px 10px 5px 0;
    border-bottom: 1px solid #d5dbe1;
    vertical-align: top;
    font-variant-numeric: tabular-nums;
  }
  thead th {
    font-weight: 600;
    font-size: 12px;
    border-bottom: 1.5px solid #0e1114;
  }
  .wind {
    font-weight: 400;
    color: #4a535d;
  }
  .small {
    font-size: 11px;
  }
  .mono {
    font-family: var(--mono);
  }
  .note {
    margin: 6px 0 0;
    font-size: 11px;
    color: #4a535d;
  }
  .text {
    margin: 0 0 6px;
    line-height: 1.45;
  }
  .two {
    display: flex;
    flex-wrap: wrap;
    gap: 14px 24px;
  }
  .two > section {
    flex: 1 1 300px;
    min-width: 0;
  }
  ul {
    margin: 0;
    padding-left: 16px;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .missing {
    color: #8a5300;
  }
  .paper footer {
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-size: 11px;
    color: #4a535d;
    border-top: 1px solid #d5dbe1;
    padding-top: 8px;
  }
  .panel {
    flex: 0 1 280px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 16px;
    background: var(--surface);
    border: 1px solid var(--border-soft);
    border-radius: 12px;
    min-width: 0;
  }
  .panel h2 {
    margin: 0;
    font-size: 15px;
  }
  .panel .muted {
    color: var(--text-faint);
  }
  .control {
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 13px;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .chips button {
    padding: 5px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: transparent;
    color: var(--text-2);
    font-size: 13px;
  }
  .chips button[aria-pressed="true"] {
    background: color-mix(in srgb, var(--accent) 16%, transparent);
    border-color: var(--accent);
    color: var(--text);
  }
  .kv {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 4px 12px;
    font-size: 13px;
  }
  .actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .actions button {
    padding: 8px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: transparent;
    color: var(--link);
    font-size: 13px;
  }
  .actions .primary {
    background: var(--accent);
    color: var(--accent-text);
    border: 0;
    font-weight: 600;
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
      font-size: 11px;
    }
    .no-print {
      display: none;
    }
  }
</style>
