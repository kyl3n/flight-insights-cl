<script lang="ts">
  // A procedure, in the order a person does it (spec: Recommendation engine,
  // "Procedures"). With a storage key, the work steps can be ticked off as they go; the
  // ticks stay on this laptop.
  import { app, t } from "../lib/app.svelte";
  import { loadTicks, saveTicks, stepLabel, tickable, type ProcedureStep } from "../lib/plan";

  let { steps, tickKey = null }: { steps: ProcedureStep[]; tickKey?: string | null } = $props();

  let ticks = $state<Set<number>>(new Set());
  $effect(() => {
    ticks = tickKey ? loadTicks(tickKey) : new Set();
  });

  function toggle(i: number) {
    const next = new Set(ticks);
    if (next.has(i)) next.delete(i);
    else next.add(i);
    ticks = next;
    if (tickKey) saveTicks(tickKey, next);
  }
</script>

<ol class="steps">
  {#each steps as s, i (i)}
    <li class={s.kind} class:done={ticks.has(i)}>
      {#if i === 0 || steps[i - 1]?.kind !== s.kind}<span class="kind">{t(stepLabel(s.kind))}</span>{/if}
      <span class="text">
        {#if tickKey && tickable(s)}
          <label>
            <input type="checkbox" checked={ticks.has(i)} onchange={() => toggle(i)} />
            <span>{s.text[app.locale]}</span>
          </label>
        {:else}
          {s.text[app.locale]}
        {/if}
      </span>
    </li>
  {/each}
</ol>

<style>
  .steps {
    margin: 0;
    padding-left: 22px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 13px;
  }
  li {
    display: flex;
    flex-direction: column;
    gap: 1px;
  }
  .kind {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
  }
  .abort .kind {
    color: var(--out);
  }
  .temporary .kind,
  .revert .kind {
    color: var(--caution);
  }
  label {
    display: flex;
    gap: 6px;
    align-items: flex-start;
    cursor: pointer;
  }
  input {
    margin-top: 2px;
  }
  .done .text {
    color: var(--text-faint);
    text-decoration: line-through;
  }
  @media print {
    .kind {
      color: #000;
    }
    input {
      appearance: auto;
    }
  }
</style>
