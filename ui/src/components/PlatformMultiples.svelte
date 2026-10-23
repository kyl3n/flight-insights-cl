<script lang="ts">
  // The same metric for every airframe of a platform as small multiples on shared axes
  // (spec: Performance trends, "Platform view"): each airframe's flights against the
  // metric's zones, the one in view outlined, and any airframe whose latest value is
  // away from its siblings' marked.
  import { app, t } from "../lib/app.svelte";
  import { formatValue, type Zone } from "../lib/report";
  import { multiples, type Series } from "../lib/trends";

  let { rows, current, names }: { rows: Series[]; current: string; names: Record<string, string> } = $props();

  const W = 200;
  const H = 56;
  const ZONE_VAR: Record<Zone, string> = { target: "--ok", caution: "--caution", out: "--out" };
  const shared = $derived(multiples(rows, W, H));
</script>

<div class="grid">
  {#each shared.rows as r (r.airframe)}
    {@const last = r.series.points.at(-1)}
    <figure class:current={r.airframe === current} class:apart={r.series.apart}>
      <figcaption>
        <span class="mono">{names[r.airframe] ?? r.airframe}</span>
        {#if last}<span class="mono">{formatValue(Math.round(last.value * 100) / 100, r.series.unit, app.locale)}</span>{/if}
      </figcaption>
      <svg viewBox="0 0 {W} {H}" width="100%" height={H} preserveAspectRatio="none" aria-hidden="true">
        {#each shared.bands as b, i (i)}
          <rect x="0" y={b.y} width={W} height={b.h} fill="var({ZONE_VAR[b.zone]})" opacity={b.zone === "target" ? 0.07 : 0.12} />
        {/each}
        <path d={r.path} fill="none" stroke={r.series.apart ? "var(--caution)" : "var(--text-muted)"} stroke-width="1.5" vector-effect="non-scaling-stroke" />
      </svg>
      {#if r.series.apart}<span class="note">▲ {t("trends.apart")}</span>{/if}
    </figure>
  {/each}
</div>

<style>
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 8px;
  }
  figure {
    margin: 0;
    padding: 8px 10px;
    border: 1px solid var(--border-soft);
    border-radius: 10px;
    background: var(--surface-2);
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .current {
    border-color: var(--accent);
  }
  .apart {
    border-color: var(--caution);
  }
  figcaption {
    display: flex;
    justify-content: space-between;
    font-size: 12px;
  }
  .note {
    font-size: 11px;
    color: var(--caution);
  }
</style>
