<script lang="ts">
  // The airframe the Plan and Configuration review views work on: the one in scope,
  // chosen here when the scope names none.
  import { app, t } from "../lib/app.svelte";
  import type { AirframeView } from "../lib/setup";
  import { engineCall } from "../lib/shell";

  let airframes = $state<AirframeView[]>([]);
  let loaded = $state(false);

  $effect(() => {
    if (app.shell.engine !== "ready") return;
    engineCall<AirframeView[]>("airframe.list", { platform_id: app.scope.platform })
      .then((rows) => {
        airframes = rows;
        loaded = true;
        if (app.scope.airframe === null && rows.length === 1) app.scope.airframe = rows[0]?.id ?? null;
      })
      .catch(() => (loaded = true));
  });
</script>

{#if loaded && airframes.length === 0}
  <p class="faint">{t("airframe.none")}</p>
{:else}
  <label class="picker">
    {t("airframe.pick")}
    <select bind:value={app.scope.airframe}>
      <option value={null}>{t("airframe.pickPrompt")}</option>
      {#each airframes as a (a.id)}
        <option value={a.id}>{a.platform.name} · {a.name}</option>
      {/each}
    </select>
  </label>
{/if}

<style>
  .picker {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    color: var(--text-muted);
  }
  select {
    background: var(--surface-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 8px;
    font-family: inherit;
  }
  .faint {
    margin: 0;
    color: var(--text-faint);
    font-size: 13px;
  }
</style>
