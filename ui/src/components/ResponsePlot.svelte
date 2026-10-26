<script lang="ts">
  // A response of the rate loop (uPlot, as the spec sets for plots): curves of two
  // flights on one axis, this flight's 95% interval as a band, a reference level drawn
  // dashed (1 for a step, −3 dB for a frequency response), lines at chosen x values
  // (the bandwidths) and, shaded, the range the stick inputs did not measure.
  import { onMount } from "svelte";
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import { app } from "../lib/app.svelte";
  import { formatNumber } from "../lib/report";
  import { alignCurves, withAlpha, type Curve } from "../lib/tune";

  let {
    curves,
    logX = false,
    xUnit,
    yUnit = "",
    level = null,
    marks = [],
    unmeasuredFrom = null,
    label,
    height = 220,
  }: {
    curves: Curve[];
    logX?: boolean;
    xUnit: string;
    yUnit?: string;
    level?: number | null;
    marks?: { x: number; tone: string }[];
    unmeasuredFrom?: number | null;
    label: string;
    height?: number;
  } = $props();

  let host: HTMLDivElement | undefined = $state();
  let plot: uPlot | null = null;

  const css = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";

  // Each curve's line, then for one with an interval its high and low rows.
  function data(): uPlot.AlignedData {
    const rows: { x: number[]; y: (number | null)[] }[] = [];
    for (const c of curves) {
      rows.push({ x: c.x, y: c.y });
      if (c.low && c.high) rows.push({ x: c.x, y: c.high }, { x: c.x, y: c.low });
    }
    return alignCurves(rows) as uPlot.AlignedData;
  }

  function background(u: uPlot) {
    const ctx = u.ctx;
    const { left, top, width, height: h } = u.bbox;
    const x = (v: number) => u.valToPos(v, "x", true);
    ctx.save();
    ctx.beginPath();
    ctx.rect(left, top, width, h);
    ctx.clip();
    if (unmeasuredFrom !== null) {
      ctx.fillStyle = css("--text-faint");
      ctx.globalAlpha = 0.12;
      const from = Math.max(x(unmeasuredFrom), left);
      ctx.fillRect(from, top, left + width - from, h);
      ctx.globalAlpha = 1;
    }
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    if (level !== null) {
      const y = u.valToPos(level, "y", true);
      ctx.strokeStyle = css("--text-faint");
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(left + width, y);
      ctx.stroke();
    }
    for (const m of marks) {
      ctx.strokeStyle = css(m.tone);
      ctx.beginPath();
      ctx.moveTo(x(m.x), top);
      ctx.lineTo(x(m.x), top + h);
      ctx.stroke();
    }
    ctx.restore();
  }

  function options(width: number): uPlot.Options {
    const grid = { stroke: css("--border-soft"), width: 1 };
    const axisStyle = { stroke: css("--text-faint"), grid, ticks: grid };
    const series: uPlot.Series[] = [{}];
    const bands: uPlot.Band[] = [];
    for (const c of curves) {
      series.push({ label: c.label, stroke: css(c.tone), width: 1.5, dash: c.dash, points: { show: false } });
      if (c.low && c.high) {
        const hi = series.length;
        series.push({ stroke: "transparent", points: { show: false } }, { stroke: "transparent", points: { show: false } });
        bands.push({ series: [hi, hi + 1], fill: withAlpha(css(c.tone), 0.22) });
      }
    }
    const tick = (unit: string) => (_: uPlot, values: number[]) =>
      values.map((v) => (v === null ? "" : `${formatNumber(v, app.locale)}${unit ? ` ${unit}` : ""}`));
    return {
      width,
      height,
      legend: { show: false },
      cursor: { drag: { x: false, y: false } },
      scales: { x: { time: false, ...(logX ? { distr: 3 } : {}) } },
      axes: [
        { ...axisStyle, values: tick(xUnit) },
        { ...axisStyle, values: tick(yUnit), size: 58 },
      ],
      series,
      bands,
      hooks: { drawClear: [background] },
    };
  }

  function build() {
    if (!host) return;
    plot?.destroy();
    plot = new uPlot(options(host.clientWidth || 420), data(), host);
  }

  onMount(() => {
    build();
    const resize = new ResizeObserver(() => host && plot?.setSize({ width: host.clientWidth, height }));
    if (host) resize.observe(host);
    return () => {
      resize.disconnect();
      plot?.destroy();
      plot = null;
    };
  });

  // New curves, marks, theme or language: rebuilt, since the series can change.
  $effect(() => {
    void curves;
    void marks;
    void level;
    void unmeasuredFrom;
    void app.theme;
    void app.locale;
    if (plot) build();
  });
</script>

<div bind:this={host} class="plot" role="img" aria-label={label} style:min-height="{height}px"></div>

<style>
  .plot {
    width: 100%;
  }
</style>
