<script lang="ts">
  // One axis of the gyro spectrum: before the filters, as flown and with the
  // candidate, with each notch's band and the low-pass cutoff marked (uPlot, as the
  // spec sets for plots). Colours come from the theme, so both themes read.
  import { onMount } from "svelte";
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import { app, t } from "../lib/app.svelte";
  import { unitText } from "../lib/report";
  import { plotData, type Axis, type Bench } from "../lib/workbench";

  let { bench, axis }: { bench: Bench; axis: Axis } = $props();

  let host: HTMLDivElement | undefined = $state();
  let plot: uPlot | null = null;

  function css(name: string): string {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
  }

  // Notch bands and the low-pass cutoff, drawn under the lines.
  function marks(u: uPlot) {
    const ctx = u.ctx;
    const top = u.bbox.top;
    const height = u.bbox.height;
    const band = (centre: number, width: number, colour: string) => {
      const x0 = u.valToPos(centre - width / 2, "x", true);
      const x1 = u.valToPos(centre + width / 2, "x", true);
      ctx.fillStyle = colour;
      ctx.fillRect(x0, top, Math.max(x1 - x0, 1), height);
    };
    const own = bench.current;
    for (const c of own.centres_hz) band(c, own.bandwidth_hz ?? 0, "rgba(90,176,232,0.12)");
    const cand = bench.candidate;
    if (cand?.available) for (const c of cand.centres_hz) band(c, cand.bandwidth_hz ?? 0, "rgba(232,163,61,0.14)");
    for (const [cutoff, colour] of [
      [own.lowpass_hz, css("--text-faint")],
      ...(cand?.available ? [[cand.lowpass_hz, css("--caution")] as const] : []),
    ] as const) {
      if (!cutoff) continue;
      const x = u.valToPos(cutoff, "x", true);
      ctx.strokeStyle = colour;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, top + height);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  function options(width: number): uPlot.Options {
    const grid = { stroke: css("--border-soft"), width: 1 };
    const axisStyle = { stroke: css("--text-faint"), grid, ticks: grid };
    return {
      width,
      height: 300,
      legend: { show: false },
      cursor: { drag: { x: true, y: false } },
      scales: { x: { time: false } },
      axes: [
        { ...axisStyle, label: unitText("Hz", app.locale) },
        { ...axisStyle, label: unitText("dB", app.locale), size: 50 },
      ],
      series: [
        {},
        { label: t("bench.before"), stroke: css("--text-faint"), width: 1 },
        { label: t("bench.flown"), stroke: css("--accent"), width: 1.5 },
        { label: t("bench.predicted"), stroke: css("--caution"), width: 1.5, dash: [6, 4] },
      ],
      hooks: { drawClear: [marks] },
    };
  }

  onMount(() => {
    if (!host) return;
    plot = new uPlot(options(host.clientWidth || 600), plotData(bench, axis) as uPlot.AlignedData, host);
    const resize = new ResizeObserver(() => host && plot?.setSize({ width: host.clientWidth, height: 300 }));
    resize.observe(host);
    return () => {
      resize.disconnect();
      plot?.destroy();
      plot = null;
    };
  });

  // New data or a new axis redraws in place; a theme or language change rebuilds.
  $effect(() => {
    const data = plotData(bench, axis) as uPlot.AlignedData;
    plot?.setData(data);
  });
  $effect(() => {
    void app.theme;
    void app.locale;
    if (!host || !plot) return;
    plot.destroy();
    plot = new uPlot(options(host.clientWidth || 600), plotData(bench, axis) as uPlot.AlignedData, host);
  });
</script>

<figure>
  <div bind:this={host} class="plot" role="img" aria-label={t("bench.plotLabel", { axis: t(`bench.axis.${axis}`) })}></div>
  <figcaption>
    <span class="key before">━ {t("bench.before")}</span>
    <span class="key flown">━ {bench.flown_from === "model" ? t("bench.flownModel") : t("bench.flown")}</span>
    <span class="key predicted">┅ {t("bench.predicted")}</span>
    <span class="key notch">▮ {t("bench.notchBand")}</span>
  </figcaption>
</figure>

<style>
  figure {
    margin: 0;
  }
  .plot {
    width: 100%;
    min-height: 300px;
  }
  figcaption {
    display: flex;
    flex-wrap: wrap;
    gap: 14px;
    font-size: 12px;
    color: var(--text-muted);
    margin-top: 6px;
  }
  .before {
    color: var(--text-faint);
  }
  .flown {
    color: var(--accent);
  }
  .predicted {
    color: var(--caution);
  }
  .notch {
    color: color-mix(in srgb, var(--accent) 50%, transparent);
  }
</style>
