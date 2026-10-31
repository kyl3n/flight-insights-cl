<script lang="ts">
  // The label every figure predicted for another propeller carries (spec: Power and
  // efficiency analysis, "Propeller and motor choice": every predicted figure is labelled
  // predicted (theory) and never presented as a maker's table; mock-up Propellers).
  import { t } from "../lib/app.svelte";
</script>

<span class="pill">{t("props.theory")}</span>

<style>
  .pill {
    display: inline-block;
    padding: 1px 7px;
    border-radius: 999px;
    border: 1px solid var(--ai);
    color: var(--ai);
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 400;
    line-height: 1.45;
    white-space: nowrap;
    vertical-align: middle;
  }
</style>
