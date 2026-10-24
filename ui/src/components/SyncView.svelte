<script lang="ts">
  // Sync (spec: Shared storage and sync). Signing in to Google, the shared drive, the
  // fleet (set up, join, approve, remove, recover), and where sync stands: each laptop
  // with its latest object and any gaps, the quarantine with its reasons, and fields
  // two laptops changed at once, with both values. Keys and tokens stay in the shell;
  // the recovery words pass through here once, at setup, and are dropped once confirmed.
  import { onMount } from "svelte";
  import { app, t } from "../lib/app.svelte";
  import { hlcTime } from "../lib/ai";
  import type { MessageKey } from "../lib/i18n";
  import { formatTakeoff } from "../lib/report";
  import { inDesktopApp } from "../lib/shell";
  import { STATUS } from "../lib/status";
  import {
    ago,
    checksMatch,
    chooseDrive,
    conflicts as loadConflicts,
    fleetApprove,
    fleetCode,
    fleetCreate,
    fleetFind,
    fleetJoin,
    fleetPreview,
    fleetRecover,
    fleetRemove,
    gapsByLaptop,
    googleDrives,
    googleSignIn,
    googleSignOut,
    laptopName,
    normalizeFingerprint,
    pickChecks,
    resolveConflict,
    showValue,
    splitWords,
    syncNow,
    syncPill,
    syncStatus,
    type Conflict,
    type SyncStatus,
  } from "../lib/sync";

  let status = $state<SyncStatus | null>(null);
  let open = $state<Conflict[]>([]);
  let note = $state("");
  let error = $state("");
  let busy = $state(false);
  let now = $state(Date.now());

  // Choosing the drive
  let drives = $state<{ id: string; name: string }[] | null>(null);
  let pickedDrive = $state("");

  // Joining, setting up, recovering
  let mode = $state<"none" | "create" | "join" | "recover">("none");
  let laptopLabel = $state("");
  let joinCode = $state<{ code: string; fingerprint: string } | null>(null);
  let approved = $state<string | null>(null);
  let typedFleet = $state("");
  let recoverWords = $state("");

  // The recovery words, shown once
  let words = $state<string[] | null>(null);
  let checks = $state<number[] | null>(null);
  let typed = $state(["", "", ""]);

  // Approving another laptop
  let approveCode = $state("");
  let preview = $state<{ name: string; fingerprint: string } | null>(null);
  let matches = $state(false);

  // After a removal: the removed laptop held copies of the service keys
  let replaceKeys = $state(false);

  async function refresh() {
    if (!inDesktopApp()) return;
    try {
      status = await syncStatus();
      open = app.shell.engine === "ready" ? await loadConflicts() : [];
    } catch (e) {
      error = String(e);
    }
  }

  onMount(() => {
    const tick = setInterval(() => (now = Date.now()), 30_000);
    return () => clearInterval(tick);
  });

  $effect(() => {
    // again whenever the worker reports a round, or the engine comes up
    void app.sync?.last_sync_ms;
    void app.sync?.state;
    void app.shell.engine;
    refresh();
  });

  async function act(what: () => Promise<void>, done?: MessageKey) {
    error = "";
    note = "";
    busy = true;
    try {
      await what();
      if (done) note = t(done);
      await refresh();
    } catch (e) {
      error = String(e);
    } finally {
      busy = false;
    }
  }

  const listDrives = () =>
    act(async () => {
      drives = await googleDrives();
      pickedDrive = drives[0]?.id ?? "";
    });

  const saveDrive = () =>
    act(async () => {
      const d = drives?.find((x) => x.id === pickedDrive);
      if (d) await chooseDrive(d.id, d.name);
      drives = null;
    }, "sync.drive.saved");

  const create = () =>
    act(async () => {
      const made = await fleetCreate(laptopLabel.trim());
      words = splitWords(made.words);
      checks = null;
      typed = ["", "", ""];
      mode = "none";
    });

  function wroteThemDown() {
    if (words) checks = pickChecks(words.length);
  }

  function confirmWords() {
    if (words && checks && checksMatch(words, checks, typed)) {
      words = null; // not kept here once confirmed
      checks = null;
      typed = ["", "", ""];
      note = t("sync.words.done");
    } else {
      error = t("sync.words.wrong");
    }
  }

  const showCode = () => act(async () => void (joinCode = await fleetCode(laptopLabel.trim())));
  const checkApproved = () => act(async () => void (approved = await fleetFind()));
  const join = () =>
    act(async () => {
      await fleetJoin(normalizeFingerprint(typedFleet) ?? "");
      mode = "none";
      joinCode = null;
      typedFleet = "";
    }, "sync.join.done");

  const recover = () =>
    act(async () => {
      const w = recoverWords;
      recoverWords = ""; // not kept here once handed over
      await fleetRecover(w, laptopLabel.trim());
      mode = "none";
    }, "sync.recover.done");

  const look = () =>
    act(async () => {
      preview = await fleetPreview(approveCode.trim());
      matches = false;
    });

  const approve = () =>
    act(async () => {
      await fleetApprove(approveCode.trim());
      approveCode = "";
      preview = null;
      matches = false;
    }, "sync.approve.done");

  function remove(id: string, name: string) {
    if (!confirm(t("sync.remove.confirm", { name }))) return;
    act(async () => {
      await fleetRemove(id);
      replaceKeys = true;
    }, "sync.remove.done");
  }

  const resolve = (id: number, keep: "kept" | "other") => act(() => resolveConflict(id, keep).then(() => {}));

  const pill = $derived(syncPill(app.sync));
  const since = $derived(ago(app.sync?.last_sync_ms ?? null, now));
  const members = $derived(status?.fleet?.members ?? []);
  const gaps = $derived(app.sync ? gapsByLaptop(app.sync, status?.engine ?? null) : new Map<string, number[]>());
  const when = (hlc: string) => {
    const d = hlcTime(hlc);
    return d ? formatTakeoff(d.getTime() * 1000, app.locale) : "";
  };
  const typedOk = $derived(normalizeFingerprint(typedFleet) !== null);
</script>

<main>
  <h1>{t("sync.title")}</h1>

  {#if !inDesktopApp()}
    <p class="faint">{t("sync.state.browser")}</p>
  {/if}

  {#if words}
    <!-- The recovery words: shown once, then gone. -->
    <section class="card words" aria-labelledby="words-title">
      <h2 id="words-title">{t("sync.words.title")}</h2>
      <p>{t("sync.words.about")}</p>
      {#if !checks}
        <ol class="mono">
          {#each words as w, i (i)}
            <li><span class="faint">{i + 1}</span> {w}</li>
          {/each}
        </ol>
        <button type="button" class="primary" onclick={wroteThemDown}>{t("sync.words.written")}</button>
      {:else}
        <form class="row" onsubmit={(e) => { e.preventDefault(); confirmWords(); }}>
          {#each checks as pos, k (pos)}
            <label>
              <span>{t("sync.words.word", { n: pos + 1 })}</span>
              <input autocomplete="off" spellcheck="false" bind:value={typed[k]} />
            </label>
          {/each}
          <button type="submit" class="primary">{t("sync.words.confirm")}</button>
          <button type="button" onclick={() => (checks = null)}>{t("sync.words.again")}</button>
        </form>
      {/if}
    </section>
  {/if}

  {#if replaceKeys}
    <section class="card warn" role="alert">
      <p>{t("sync.remove.keys")}</p>
      <div class="row">
        <button type="button" class="primary" onclick={() => (app.view = "settings")}>{t("sync.remove.keys.go")}</button>
        <button type="button" onclick={() => (replaceKeys = false)}>{t("sync.remove.keys.later")}</button>
      </div>
    </section>
  {/if}

  <section class="card" aria-labelledby="state-title">
    <h2 id="state-title">{t("sync.status")}</h2>
    <div class="line">
      <span class="pill" style:--tone="var({STATUS[pill.status].tone})">
        <span aria-hidden="true">{STATUS[pill.status].glyph}</span>{t(pill.label)}
      </span>
      <span class="faint">{t("sync.last", { when: t(since.key, { n: since.n }) })}</span>
      {#if app.sync}
        <span class="faint">{t("sync.waiting", { count: status?.engine?.waiting_to_send ?? app.sync.waiting })}</span>
        <span class="faint">{t("sync.quarantined", { count: status?.engine?.quarantine.length ?? app.sync.quarantined })}</span>
      {/if}
      <button type="button" disabled={busy || !status?.fleet} onclick={() => act(syncNow, "sync.now.done")}>{t("sync.now")}</button>
    </div>
    <!-- the shell's own words on what went wrong, as a detail under the state -->
    {#if app.sync?.why && ["offline", "error", "sign_in"].includes(app.sync.state)}<p class="faint">{app.sync.why}</p>{/if}
    {#if app.sync?.sources_moved}<p class="caution">{t("sync.moved", { count: app.sync.sources_moved })}</p>{/if}
    {#if note}<p class="ok" role="status">{note}</p>{/if}
    {#if error}<p class="error" role="alert">{error}</p>{/if}
  </section>

  {#if status}
    <section class="card" aria-labelledby="google-title">
      <h2 id="google-title">{t("sync.google")}</h2>
      {#if !status.client}
        <p>{t("sync.google.no_client")}</p>
      {:else if !status.signed_in}
        <p class="faint">{t("sync.google.about")}</p>
        <div class="row">
          <button type="button" class="primary" disabled={busy} onclick={() => act(googleSignIn, "sync.google.signed_in")}>{t("sync.google.sign_in")}</button>
        </div>
        {#if busy}<p class="faint">{t("sync.google.browser")}</p>{/if}
      {:else}
        <p>{t("sync.google.signed_in")}</p>
        <p>
          {#if status.drive}{t("sync.drive.current", { name: status.drive.name ?? status.drive.id })}{:else}{t("sync.drive.none")}{/if}
        </p>
        <div class="row">
          {#if !status.fleet}
            <button type="button" disabled={busy} onclick={listDrives}>{t("sync.drive.choose")}</button>
          {/if}
          <button type="button" disabled={busy} onclick={() => act(googleSignOut)}>{t("sync.google.sign_out")}</button>
        </div>
        {#if drives}
          {#if drives.length === 0}
            <p class="faint">{t("sync.drive.empty")}</p>
          {:else}
            <form class="row" onsubmit={(e) => { e.preventDefault(); saveDrive(); }}>
              <label>
                <span>{t("sync.drive")}</span>
                <select bind:value={pickedDrive}>
                  {#each drives as d (d.id)}
                    <option value={d.id}>{d.name}</option>
                  {/each}
                </select>
              </label>
              <button type="submit" class="primary">{t("sync.drive.use")}</button>
            </form>
          {/if}
        {/if}
      {/if}
    </section>

    <section class="card" aria-labelledby="fleet-title">
      <h2 id="fleet-title">{t("sync.fleet")}</h2>
      {#if status.fleet}
        <p>{t("sync.fleet.fingerprint")} <strong class="mono">{status.fleet.fingerprint}</strong></p>
        <ul class="list">
          {#each members as m (m.id)}
            <li class:revoked={m.revoked}>
              <strong>{m.name}</strong>
              <span class="mono faint">{m.fingerprint}</span>
              {#if m.me}<span class="tag">{t("sync.fleet.me")}</span>{/if}
              {#if m.revoked}<span class="faint">{t("sync.fleet.removed")}</span>{/if}
              {#if !m.me && !m.revoked}
                <button type="button" class="small" disabled={busy} onclick={() => remove(m.id, m.name)}>{t("sync.remove")}</button>
              {/if}
            </li>
          {/each}
        </ul>
        <h3>{t("sync.approve")}</h3>
        <p class="faint">{t("sync.approve.about")}</p>
        <form class="row" onsubmit={(e) => { e.preventDefault(); look(); }}>
          <label class="wide">
            <span>{t("sync.approve.code")}</span>
            <textarea rows="2" spellcheck="false" autocomplete="off" bind:value={approveCode}></textarea>
          </label>
          <button type="submit" disabled={busy || !approveCode.trim()}>{t("sync.approve.look")}</button>
        </form>
        {#if preview}
          <p>{t("sync.approve.preview", { name: preview.name })} <strong class="mono">{preview.fingerprint}</strong></p>
          <label class="check">
            <input type="checkbox" bind:checked={matches} />
            <span>{t("sync.approve.matches")}</span>
          </label>
          <div class="row">
            <button type="button" class="primary" disabled={busy || !matches} onclick={approve}>{t("sync.approve.go")}</button>
          </div>
        {/if}
      {:else if status.signed_in && status.drive}
        <p class="faint">{t("sync.fleet.none")}</p>
        <div class="row">
          <button type="button" class:primary={mode === "create"} onclick={() => (mode = "create")}>{t("sync.create")}</button>
          <button type="button" class:primary={mode === "join"} onclick={() => (mode = "join")}>{t("sync.join")}</button>
          <button type="button" class:primary={mode === "recover"} onclick={() => (mode = "recover")}>{t("sync.recover")}</button>
        </div>
        {#if mode !== "none"}
          <label>
            <span>{t("sync.laptop_name")}</span>
            <input maxlength="60" bind:value={laptopLabel} />
          </label>
        {/if}
        {#if mode === "create"}
          <p class="faint">{t("sync.create.about")}</p>
          <div class="row">
            <button type="button" class="primary" disabled={busy || !laptopLabel.trim()} onclick={create}>{t("sync.create.go")}</button>
          </div>
        {:else if mode === "join"}
          <p class="faint">{t("sync.join.about")}</p>
          {#if !joinCode}
            <div class="row">
              <button type="button" class="primary" disabled={busy || !laptopLabel.trim()} onclick={showCode}>{t("sync.join.code")}</button>
            </div>
          {:else}
            <pre class="mono code">{joinCode.code}</pre>
            <p>{t("sync.join.fingerprint")} <strong class="mono">{joinCode.fingerprint}</strong></p>
            <div class="row">
              <button type="button" disabled={busy} onclick={checkApproved}>{t("sync.join.check")}</button>
              {#if approved === null}<span class="faint">{t("sync.join.waiting")}</span>{/if}
            </div>
            {#if approved}
              <form class="row" onsubmit={(e) => { e.preventDefault(); join(); }}>
                <label>
                  <span>{t("sync.join.fleet")}</span>
                  <input class="mono" autocomplete="off" spellcheck="false" placeholder="XXXX-XXXX-XXXX" bind:value={typedFleet} />
                </label>
                <button type="submit" class="primary" disabled={busy || !typedOk}>{t("sync.join.go")}</button>
              </form>
            {/if}
          {/if}
        {:else if mode === "recover"}
          <p class="faint">{t("sync.recover.about")}</p>
          <form class="row" onsubmit={(e) => { e.preventDefault(); recover(); }}>
            <label class="wide">
              <span>{t("sync.recover.words")}</span>
              <textarea rows="3" spellcheck="false" autocomplete="off" bind:value={recoverWords}></textarea>
            </label>
            <button type="submit" class="primary" disabled={busy || !laptopLabel.trim() || splitWords(recoverWords).length !== 24}>{t("sync.recover.go")}</button>
          </form>
        {/if}
      {:else}
        <p class="faint">{t("sync.fleet.first")}</p>
      {/if}
    </section>

    {#if status.fleet}
      <section class="card" aria-labelledby="laptops-title">
        <h2 id="laptops-title">{t("sync.laptops")}</h2>
        {#if (status.engine?.laptops.length ?? 0) === 0 && gaps.size === 0}
          <p class="faint">{t("sync.laptops.none")}</p>
        {/if}
        <ul class="list">
          {#each status.engine?.laptops ?? [] as l (l.laptop)}
            {@const missing = gaps.get(l.laptop) ?? []}
            <li>
              <strong>{laptopName(l.laptop, members)}</strong>
              <span class="faint">{t("sync.laptops.latest", { n: l.latest, count: l.objects })}</span>
              {#if missing.length}
                <span class="caution">{t("sync.laptops.gaps", { list: missing.slice(0, 8).join(", ") })}</span>
              {/if}
            </li>
          {/each}
          {#each [...gaps].filter(([id]) => !status?.engine?.laptops.some((l) => l.laptop === id)) as [id, missing] (id)}
            <li>
              <strong>{laptopName(id, members)}</strong>
              <span class="caution">{t("sync.laptops.gaps", { list: missing.slice(0, 8).join(", ") })}</span>
            </li>
          {/each}
        </ul>
      </section>

      <section class="card" aria-labelledby="quarantine-title">
        <h2 id="quarantine-title">{t("sync.quarantine")}</h2>
        <p class="faint">{t("sync.quarantine.about")}</p>
        {#if (status.engine?.quarantine.length ?? 0) === 0}
          <p class="faint">{t("sync.quarantine.none")}</p>
        {/if}
        <ul class="list">
          {#each status.engine?.quarantine ?? [] as q (q.object_key)}
            <li>
              <strong>{laptopName(q.laptop, members)}</strong>
              {#if q.sequence !== null}<span class="mono faint">#{q.sequence}</span>{/if}
              <span>{q.reason}</span>
              <span class="faint">{when(q.at)}</span>
            </li>
          {/each}
        </ul>
      </section>

      <section class="card" aria-labelledby="conflicts-title">
        <h2 id="conflicts-title">{t("sync.conflicts")}</h2>
        <p class="faint">{t("sync.conflicts.about")}</p>
        {#if open.length === 0}
          <p class="faint">{t("sync.conflicts.none")}</p>
        {/if}
        <ul class="list conflicts">
          {#each open as c (c.id)}
            <li>
              <div class="line">
                <strong class="mono">{c.tbl}.{c.field}</strong>
                <span class="faint">{when(c.at_hlc)}</span>
              </div>
              <div class="sides">
                <div>
                  <span class="faint">{t("sync.conflicts.kept")}</span>
                  <span class="mono">{showValue(c.kept)}</span>
                  <button type="button" class="small" disabled={busy} onclick={() => resolve(c.id, "kept")}>{t("sync.conflicts.keep")}</button>
                </div>
                <div>
                  <span class="faint">{t("sync.conflicts.other")}</span>
                  <span class="mono">{showValue(c.other)}</span>
                  <button type="button" class="small" disabled={busy} onclick={() => resolve(c.id, "other")}>{t("sync.conflicts.use")}</button>
                </div>
              </div>
            </li>
          {/each}
        </ul>
      </section>
    {/if}
  {/if}
</main>

<style>
  main {
    padding: 16px 24px 32px;
    display: flex;
    flex-direction: column;
    gap: 16px;
    max-width: 980px;
  }
  h1 {
    margin: 0;
    font-size: 20px;
  }
  h2 {
    margin: 0;
    font-size: 15px;
  }
  h3 {
    margin: 6px 0 0;
    font-size: 13px;
  }
  .card {
    padding: 16px 18px;
    border-radius: 12px;
    background: var(--surface);
    border: 1px solid var(--border-soft);
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .warn,
  .words {
    border-color: var(--caution);
  }
  p {
    margin: 0;
  }
  .row,
  .line {
    display: flex;
    gap: 8px;
    align-items: flex-end;
    flex-wrap: wrap;
  }
  .line {
    align-items: center;
    font-size: 13px;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
    color: var(--text-muted);
  }
  label.wide {
    flex: 1 1 320px;
  }
  label.check {
    flex-direction: row;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    color: var(--text);
  }
  input:not([type="checkbox"]),
  select,
  textarea {
    min-width: 220px;
    padding: 7px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-2);
    color: var(--text);
    font-size: 13px;
    font-family: inherit;
  }
  button {
    padding: 8px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface);
    color: var(--text);
    font-size: 13px;
    cursor: pointer;
  }
  button.small {
    padding: 4px 8px;
    font-size: 12px;
  }
  .primary {
    border: 0;
    background: var(--accent);
    color: var(--accent-text);
    font-weight: 600;
  }
  button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  .pill {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 5px 10px;
    border-radius: 999px;
    border: 1px solid var(--border-soft);
    font-size: 12px;
  }
  .pill > span[aria-hidden] {
    color: var(--tone);
    font-size: 9px;
  }
  .faint {
    font-size: 12px;
    color: var(--text-faint);
  }
  .caution {
    font-size: 12px;
    color: var(--caution);
  }
  ol {
    margin: 0;
    padding: 0;
    list-style: none;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 6px 16px;
    font-size: 14px;
  }
  .list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .list li {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    align-items: center;
    border: 1px solid var(--border-soft);
    border-radius: 8px;
    padding: 8px 10px;
    font-size: 13px;
  }
  .conflicts li {
    flex-direction: column;
    align-items: stretch;
  }
  .sides {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 8px;
  }
  .sides > div {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }
  .revoked {
    opacity: 0.6;
  }
  .tag {
    font-size: 11px;
    padding: 1px 6px;
    border-radius: 6px;
    background: var(--surface-2);
    color: var(--text-muted);
  }
  pre.code {
    margin: 0;
    padding: 8px;
    border-radius: 6px;
    background: var(--surface-2);
    font-size: 11px;
    white-space: pre-wrap;
    word-break: break-all;
    user-select: all;
  }
  .ok {
    color: var(--ok, var(--text));
  }
  .error {
    color: var(--out, #e5484d);
  }
</style>
