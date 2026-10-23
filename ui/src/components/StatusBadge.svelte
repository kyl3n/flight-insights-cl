<script lang="ts">
  import { t } from "../lib/app.svelte";
  import { STATUS, type Status } from "../lib/status";

  let { status, label }: { status: Status; label?: string } = $props();
  const look = $derived(STATUS[status]);
</script>

<span class="badge" style:--tone="var({look.tone})">
  <span aria-hidden="true" class="glyph">{look.glyph}</span>{label ?? t(look.label)}
</span>

<style>
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 9px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    max-width: 100%;
    box-sizing: border-box;
    color: var(--tone);
    background: color-mix(in srgb, var(--tone) 14%, transparent);
  }
  .glyph {
    font-size: 9px;
  }
</style>
