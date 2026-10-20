<script lang="ts">
  import { appTitle } from "./lib/app";
</script>

<header>
  <h1>{appTitle}</h1>
</header>
<main>
  <p>No flights imported yet.</p>
</main>

<style>
  header {
    display: flex;
    align-items: center;
    padding: 0 16px;
    height: 48px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
  }

  h1 {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
  }

  main {
    padding: 16px;
    color: var(--text-muted);
  }
</style>
