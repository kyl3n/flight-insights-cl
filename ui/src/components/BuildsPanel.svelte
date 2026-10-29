<script lang="ts">
  // Build against build on the Range and speed screen (spec: Power and efficiency
  // analysis, "Range, endurance and speed"; mock-up Performance, "Build against build"):
  // the platform's current parts against a candidate set, from the makers' bench tables a
  // person enters here. The candidate is the airframe's own model with its motors' power
  // scaled by the tables' ratio at hover thrust; every candidate figure is an estimate.
  import { app, t } from "../lib/app.svelte";
  import { benchSave, draftOf, emptyDraft, hoverShare, parseSteps, ROLES, type BenchTable, type Builds, type Draft } from "../lib/bench";
  import type { MessageKey } from "../lib/i18n";
  import { plot, split, type Performance } from "../lib/range";
  import { formatNumber, unitText } from "../lib/report";

  let { view, onchange }: { view: Performance; onchange: () => void } = $props();

  const num = (v: number | null | undefined, digits = 1) =>
    v === null || v === undefined || !Number.isFinite(v) ? "–" : formatNumber(Math.round(v * 10 ** digits) / 10 ** digits, app.locale);
  const unit = (u: string) => unitText(u, app.locale);
  const grams = (g: number) => (g >= 1000 ? `${num(g / 1000, 2)} ${unit("kg")}` : `${num(g, 0)} ${unit("g")}`);
  const minutes = (s: number | null | undefined) => (s === null || s === undefined ? "–" : `${num(s / 60)} ${unit("min")}`);

  const b = $derived(view.builds as Builds);
  const current = $derived(b.tables.find((x) => x.role === "current") ?? null);
  const candidate = $derived(b.tables.find((x) => x.role === "candidate") ?? null);
  const kg = $derived(view.inputs?.mass_kg ?? null);
  const c = $derived(b.compare);
  const figs = $derived(b.figures);
  const share = $derived(c?.share ?? null);

  // ---- charts ----
  const F = { width: 440, height: 210, left: 52, right: 14, top: 14, bottom: 30 };
  const kw = (w: number) => w / 1000;
  const power = $derived(b.power ? plot(b.power.airspeed_ms, [...b.power.current_w.map(kw), ...b.power.candidate_w.map(kw)], F) : null);
  const top = $derived(view.curves?.top_ms ?? Infinity);
  const nowLine = $derived(power && b.power ? split(b.power.airspeed_ms, b.power.current_w.map(kw), top, power) : null);
  // the candidate is never flown: dashed all the way
  const candLine = $derived(power && b.power ? split(b.power.airspeed_ms, b.power.candidate_w.map(kw), -1, power).beyond : "");
  const flownRange = $derived((view.against_weight ?? []).map((r) => ({ kg: r.mass_kg, km: r.range_m / 1000 })));
  const candRange = $derived(
    (b.against_weight ?? []).filter((r) => r.range_m !== null).map((r) => ({ kg: r.mass_kg + (c?.delta_kg ?? 0), km: r.range_m! / 1000 })),
  );
  const range = $derived(
    flownRange.length > 1 && candRange.length > 1
      ? plot([...flownRange.map((r) => r.kg), ...candRange.map((r) => r.kg)], [...flownRange.map((r) => r.km), ...candRange.map((r) => r.km)], F, 0, null)
      : null,
  );
  const xsOf = (rows: { kg: number }[]) => rows.map((r) => r.kg);

  // ---- the side by side ----
  const facts = $derived.by(() => {
    if (!c || !current || !candidate) return [];
    const now = figs?.current, then = figs?.candidate ?? null;
    const thrustNow = ((kg ?? 0) * 1000) / b.motors;
    return [
      { n: t("builds.row.weight"), a: `${num(kg)} ${unit("kg")}`, b: `${num(c.mass_kg)} ${unit("kg")}` },
      { n: t("builds.row.bench"), a: `${num(c.current_w, 0)} ${unit("W")}`, b: `${num(c.candidate_w, 0)} ${unit("W")}` },
      { n: t("builds.row.share"), a: `${num(hoverShare(thrustNow, current) * 100, 0)}%`, b: `${num(hoverShare(c.thrust_g, candidate) * 100, 0)}%` },
      { n: t("builds.row.hover"), a: minutes(now?.hover_s), b: minutes(then?.hover_s) },
      { n: t("builds.row.endurance"), a: minutes(now?.endurance_s), b: minutes(then?.endurance_s) },
      { n: t("builds.row.range"), a: `${num((now?.range_m ?? NaN) / 1000)} ${unit("km")}`, b: `${num((then?.range_m ?? NaN) / 1000)} ${unit("km")}` },
      { n: t("builds.row.gw"), a: num(now?.efficiency.grams_per_watt, 2), b: num(then?.efficiency.grams_per_watt, 2) },
    ];
  });

  // ---- the tables and the form ----
  let editing = $state<BenchTable | null>(null);
  let draft = $state<Draft | null>(null);
  let saving = $state(false);
  let error = $state("");
  const read = $derived(draft ? parseSteps(draft.steps) : { steps: [], refused: 0 });

  function open(table: BenchTable | null) {
    editing = table;
    draft = table ? draftOf(table) : emptyDraft(current ? (candidate ? "none" : "candidate") : "current");
    error = "";
  }
  async function save() {
    if (!draft) return;
    saving = true;
    error = "";
    try {
      await benchSave(b.platform_id, draft, editing);
      draft = null;
      editing = null;
      onchange();
    } catch (e: unknown) {
      error = String(e);
    } finally {
      saving = false;
    }
  }
  function about(x: BenchTable): string {
    return t("builds.table.facts", {
      kv: x.kv !== null ? `KV${num(x.kv, 0)}` : "–",
      prop: x.prop_diameter_in !== null ? `${num(x.prop_diameter_in)}×${num(x.prop_pitch_in)}` : "–",
      volts: x.volts !== null ? `${num(x.volts)} ${unit("V")}` : "–",
      steps: x.rows.length,
      thrust: grams(x.max_thrust_g),
    });
  }
</script>

<section class="panel" aria-labelledby="c10-title">
  <div class="head">
    <h2 id="c10-title">
      {t("builds.title")}{#if current && candidate}&nbsp;· {current.label} / {candidate.label}{/if}
    </h2>
    <span class="faint">{t("builds.about")}</span>
  </div>

  {#if b.flown_vs_bench}
    {@const v = b.flown_vs_bench}
    <p class="text">
      {#if v.share !== null}
        {t("builds.flown", { kg: num(kg), flown: `${num(v.flown_w, 0)} ${unit("W")}`, pct: num(v.share * 100, 0), thrust: grams(v.thrust_g) })}
        {t(v.share >= 1 ? "builds.flown.above" : "builds.flown.below")}
      {:else}
        {t("builds.flown.outside", { kg: num(kg), thrust: grams(v.thrust_g) })}
      {/if}
    </p>
  {/if}

  {#if b.missing && b.tables.length}
    <p class="notice">{t(`builds.missing.${b.missing}` as MessageKey)}</p>
  {/if}

  {#if c && current && candidate}
    {#if power && b.power}
      <div class="charts">
        <figure>
          <figcaption>
            <span class="cap">{t("builds.power", { kg: num(kg) })}</span>
            <span><span aria-hidden="true" class="key">━</span> {t("builds.current.flown", { label: current.label })}</span>
            <span><span aria-hidden="true" class="key dashed">┅</span> {t("builds.candidate.notflown", { label: candidate.label })}</span>
          </figcaption>
          <svg viewBox="0 0 {F.width} {F.height}" role="img" aria-label={t("builds.power.aria", { pct: num((share ?? NaN) * 100, 0) })}>
            {#each power.y.ticks as v (v)}
              <line class="grid" x1={F.left} x2={F.width - F.right} y1={power.py(v)} y2={power.py(v)} />
              <text class="tick" x={F.left - 6} y={power.py(v) + 4} text-anchor="end">{num(v, 1)}</text>
            {/each}
            {#each power.x.ticks as v (v)}
              <text class="tick" x={power.px(v)} y={F.height - F.bottom + 16} text-anchor="middle">{num(v, 0)}</text>
            {/each}
            <text class="tick" x={4} y={12}>{unit("kW")}</text>
            <text class="tick" x={F.width - F.right} y={F.height - 2} text-anchor="end">{unit("m/s")}</text>
            <path class="curve" d={nowLine?.solid} />
            <path class="curve beyond" d={nowLine?.beyond} />
            <path class="curve cand" d={candLine} />
          </svg>
        </figure>
        {#if range}
          <figure>
            <figcaption>
              <span class="cap">{t("builds.range")}</span>
              <span><span aria-hidden="true" class="key">━</span> {current.label}</span>
              <span><span aria-hidden="true" class="key dashed">┅</span> {t("builds.candidate.notflown", { label: candidate.label })}</span>
            </figcaption>
            <svg viewBox="0 0 {F.width} {F.height}" role="img" aria-label={t("builds.range.aria")}>
              {#each range.y.ticks as v (v)}
                <line class="grid" x1={F.left} x2={F.width - F.right} y1={range.py(v)} y2={range.py(v)} />
                <text class="tick" x={F.left - 6} y={range.py(v) + 4} text-anchor="end">{num(v, 1)}</text>
              {/each}
              {#each range.x.ticks as v (v)}
                <text class="tick" x={range.px(v)} y={F.height - F.bottom + 16} text-anchor="middle">{num(v, 0)}</text>
              {/each}
              <text class="tick" x={4} y={12}>{unit("km")}</text>
              <text class="tick" x={F.width - F.right} y={F.height - 2} text-anchor="end">{unit("kg")}</text>
              <path class="curve" d={split(xsOf(flownRange), flownRange.map((r) => r.km), Infinity, range).solid} />
              <path class="curve cand" d={split(xsOf(candRange), candRange.map((r) => r.km), -1, range).beyond} />
            </svg>
          </figure>
        {/if}
      </div>
    {/if}

    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th scope="col"><span class="sr-only">{t("builds.col.what")}</span></th>
            <th scope="col">{t("builds.current.flown", { label: current.label })}</th>
            <th scope="col">{t("builds.candidate.notflown", { label: candidate.label })}</th>
          </tr>
        </thead>
        <tbody>
          {#each facts as f (f.n)}
            <tr><td>{f.n}</td><td class="mono">{f.a}</td><td class="mono">{f.b}</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#each c.notes as n (n)}
      <p class="notice">▲ {t(`builds.note.${n}` as MessageKey)}</p>
    {/each}
    <p class="faint">{t("builds.note")}</p>
  {/if}

  <div class="tables">
    <h3>{t("builds.tables")}</h3>
    {#if !b.tables.length}<p class="faint">{t("builds.none")}</p>{/if}
    <ul>
      {#each b.tables as x (x.id)}
        <li>
          <span class="tag" class:on={x.role !== "none"}>{t(`builds.role.${x.role}` as MessageKey)}</span>
          <span class="name">{x.label}</span>
          <span class="faint">{about(x)}{#if !x.full_throttle}{" · "}{t("builds.table.short", { pct: num(x.rows.at(-1)?.throttle_pct, 0) })}{/if}</span>
          <button type="button" class="link" onclick={() => open(x)}>{t("builds.edit")}</button>
        </li>
      {/each}
    </ul>
    {#if !draft}
      <button type="button" class="link" onclick={() => open(null)}>{t("builds.add")}</button>
    {/if}
  </div>

  {#if draft}
    <form
      class="form"
      onsubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <h3>{editing ? t("builds.form.title.edit", { label: editing.label }) : t("builds.form.title.new")}</h3>
      <div class="fields">
        <label class="wide">{t("builds.form.label")}<input type="text" maxlength="80" required placeholder={t("builds.form.label.hint")} bind:value={draft.label} /></label>
        <label
          >{t("builds.form.role")}<select bind:value={draft.role}>
            {#each ROLES as r (r)}<option value={r}>{t(`builds.role.${r}` as MessageKey)}</option>{/each}
          </select></label
        >
        <label>{t("builds.form.motor")}<input type="text" maxlength="80" bind:value={draft.motor} /></label>
        <label>{t("builds.form.kv")}<input type="text" inputmode="decimal" bind:value={draft.kv} /></label>
        <label>{t("builds.form.diameter")}<input type="text" inputmode="decimal" bind:value={draft.prop_diameter_in} /></label>
        <label>{t("builds.form.pitch")}<input type="text" inputmode="decimal" bind:value={draft.prop_pitch_in} /></label>
        <label>{t("builds.form.volts")}<input type="text" inputmode="decimal" bind:value={draft.volts} /></label>
        <label>{t("builds.form.mass")}<input type="text" inputmode="decimal" bind:value={draft.motor_mass_g} /></label>
        <label class="wide">{t("builds.form.source")}<input type="text" maxlength="300" bind:value={draft.source} /></label>
        <label class="wide"
          >{t("builds.form.steps")}<textarea rows="8" spellcheck="false" bind:value={draft.steps}></textarea>
          <span class="faint">{t("builds.form.steps.hint")} {t("builds.form.read", { n: read.steps.length })}</span>
          {#if read.refused}<span class="error">{t("builds.form.refused", { n: read.refused })}</span>{/if}</label
        >
      </div>
      {#if error}<p class="error">{error}</p>{/if}
      <div class="actions">
        <button type="submit" class="primary" disabled={saving}>{t("builds.form.save")}</button>
        <button
          type="button"
          class="link"
          onclick={() => {
            draft = null;
            editing = null;
          }}>{t("builds.form.cancel")}</button
        >
      </div>
    </form>
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
    gap: 6px 12px;
    align-items: baseline;
    flex-wrap: wrap;
  }
  h2 {
    margin: 0;
    font-size: 15px;
  }
  h3 {
    margin: 0;
    font-size: 13px;
    font-weight: 600;
  }
  p {
    margin: 0;
  }
  .text {
    font-size: 13px;
    line-height: 1.4;
  }
  .faint {
    color: var(--text-faint);
    font-size: 12px;
  }
  .notice {
    font-size: 13px;
    color: var(--caution);
  }
  .mono {
    font-family: var(--mono);
  }
  .charts {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
  }
  figure {
    flex: 1 1 320px;
    margin: 0;
    min-width: 0;
  }
  figcaption {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 12px;
    font-size: 12px;
    color: var(--text-muted);
  }
  .cap {
    color: var(--text-2);
  }
  .key {
    color: var(--accent);
  }
  .key.dashed {
    color: var(--text-muted);
  }
  svg {
    display: block;
    width: 100%;
    height: auto;
  }
  .grid {
    stroke: var(--border-soft);
  }
  .tick {
    fill: var(--text-faint);
    font-size: 10px;
    font-family: var(--mono);
  }
  .curve {
    fill: none;
    stroke: var(--accent);
    stroke-width: 2.2;
  }
  .curve.beyond {
    stroke-dasharray: 5 4;
    opacity: 0.7;
  }
  .curve.cand {
    stroke: var(--text-muted);
    stroke-width: 2;
    stroke-dasharray: 5 4;
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
  }
  td {
    padding: 7px 12px 7px 0;
    border-top: 1px solid var(--border-soft);
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }
  .tables ul {
    list-style: none;
    margin: 6px 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .tables li {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 10px;
    font-size: 13px;
  }
  .name {
    font-weight: 600;
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
  .tag.on {
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    color: var(--link);
  }
  .form {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding-top: 10px;
    border-top: 1px solid var(--border-soft);
  }
  .fields {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 10px 12px;
  }
  .wide {
    grid-column: 1 / -1;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 5px;
    font-size: 13px;
    min-width: 0;
  }
  input,
  select,
  textarea {
    padding: 7px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-2);
    color: var(--text);
    font-size: 13px;
    min-width: 0;
  }
  textarea {
    font-family: var(--mono);
    resize: vertical;
  }
  .actions {
    display: flex;
    gap: 10px;
    align-items: center;
  }
  .primary {
    padding: 9px 14px;
    border: 0;
    border-radius: 8px;
    background: var(--accent);
    color: var(--accent-text);
    font-size: 13px;
    font-weight: 600;
  }
  .primary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  .link {
    align-self: flex-start;
    padding: 6px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: transparent;
    color: var(--link);
    font-size: 13px;
    cursor: pointer;
  }
  .error {
    color: var(--out);
    font-size: 13px;
  }
</style>
