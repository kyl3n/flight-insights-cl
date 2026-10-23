<script lang="ts">
  // A metric's zones on one bar with the value marked, under a value or in its cards.
  import { scale, type MetricInfo } from "../lib/report";
  import { zoneLook } from "../lib/report";
  import { STATUS } from "../lib/status";

  let { metric, value }: { metric: MetricInfo; value: number } = $props();
  const laid = $derived(scale(metric, value));
</script>

{#if laid}
  <div class="bar" aria-hidden="true">
    {#each laid.bands as band, i (i)}
      <span
        class="band"
        style:left="{band.from * 100}%"
        style:width="{(band.to - band.from) * 100}%"
        style:--tone="var({STATUS[zoneLook(band.zone).status].tone})"
      ></span>
    {/each}
    <span class="marker" style:left="{Math.min(Math.max(laid.at, 0), 1) * 100}%"></span>
  </div>
{/if}

<style>
  .bar {
    position: relative;
    height: 8px;
    border-radius: 4px;
    overflow: visible;
    background: var(--surface-2);
  }
  .band {
    position: absolute;
    top: 0;
    bottom: 0;
    background: color-mix(in srgb, var(--tone) 45%, transparent);
  }
  .band:first-child {
    border-radius: 4px 0 0 4px;
  }
  .band:last-child {
    border-radius: 0 4px 4px 0;
  }
  .marker {
    position: absolute;
    top: -3px;
    width: 3px;
    height: 14px;
    margin-left: -1.5px;
    border-radius: 2px;
    background: var(--text);
  }
</style>
