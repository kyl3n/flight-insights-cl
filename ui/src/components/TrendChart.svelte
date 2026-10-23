<script lang="ts">
  // One metric for one airframe, one point per flight (uPlot, as the spec sets for time
  // series): the metric's zones as bands, the airframe's own baseline, each point in
  // its zone's colour, and drift marked where it starts. Clicking a point picks that
  // flight.
  import { onMount } from "svelte";
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import { app, t } from "../lib/app.svelte";
  import { formatNumber, unitText, type Zone } from "../lib/report";
  import { chartData, yRange, type Series } from "../lib/trends";

  let {
    series,
    picked = null,
    label,
    onpick,
    onlayout,
  }: {
    series: Series;
    picked?: number | null;
    label: string;
    onpick?: (index: number) => void;
    onlayout?: (box: { left: number; width: number; count: number }) => void;
  } = $props();

  let host: HTMLDivElement | undefined = $state();
  let plot: uPlot | null = null;

  const css = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
  const ZONE_VAR: Record<Zone, string> = { target: "--ok", caution: "--caution", out: "--out" };

  // Zone bands and the baseline under everything, drawn across the plot.
  function bands(u: uPlot) {
    const ctx = u.ctx;
    const { left, top, width, height } = u.bbox;
    const y = (v: number) => u.valToPos(v, "y", true);
    ctx.save();
    ctx.beginPath();
    ctx.rect(left, top, width, height);
    ctx.clip();
    for (const [zone, intervals] of Object.entries(series.zones) as [Zone, [number | null, number | null][]][]) {
      ctx.fillStyle = css(ZONE_VAR[zone]);
      ctx.globalAlpha = zone === "target" ? 0.06 : 0.1;
      for (const [lo, hi] of intervals) {
        const y0 = hi === null ? top : y(hi);
        const y1 = lo === null ? top + height : y(lo);
        ctx.fillRect(left, Math.min(y0, y1), width, Math.abs(y1 - y0));
      }
    }
    ctx.globalAlpha = 1;
    const b = series.baseline;
    if (b) {
      ctx.strokeStyle = css("--text-muted");
      ctx.setLineDash([5, 4]);
      ctx.lineWidth = 1;
      ctx.strokeRect(left, y(b.high), width, y(b.low) - y(b.high));
      ctx.setLineDash([]);
    }
    const since = series.drift ? series.points.findIndex((p) => p.flight === series.drift?.since) : -1;
    if (since >= 0) {
      const x = u.valToPos(since - 0.5, "x", true);
      ctx.strokeStyle = css("--caution");
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, top + height);
      ctx.stroke();
    }
    ctx.restore();
  }

  // Each flight's point in its zone's colour, the picked one ringed.
  function points(u: uPlot) {
    const ctx = u.ctx;
    const dpr = devicePixelRatio || 1;
    series.points.forEach((p, i) => {
      const x = u.valToPos(i, "x", true);
      const yy = u.valToPos(p.value, "y", true);
      ctx.fillStyle = p.zone ? css(ZONE_VAR[p.zone]) : css("--text");
      ctx.beginPath();
      ctx.arc(x, yy, 4 * dpr, 0, Math.PI * 2);
      ctx.fill();
      if (!p.good) {
        ctx.strokeStyle = css("--surface");
        ctx.lineWidth = 1.5 * dpr;
        ctx.stroke();
      }
      if (i === picked) {
        ctx.strokeStyle = css("--accent");
        ctx.lineWidth = 2 * dpr;
        ctx.beginPath();
        ctx.arc(x, yy, 8 * dpr, 0, Math.PI * 2);
        ctx.stroke();
      }
    });
  }

  function report(u: uPlot) {
    const dpr = devicePixelRatio || 1;
    onlayout?.({ left: u.bbox.left / dpr, width: u.bbox.width / dpr, count: series.points.length });
  }

  function options(width: number): uPlot.Options {
    const grid = { stroke: css("--border-soft"), width: 1 };
    const axis = { stroke: css("--text-faint"), grid, ticks: grid };
    const n = Math.max(series.points.length, 1);
    return {
      width,
      height: 280,
      legend: { show: false },
      cursor: { drag: { x: false, y: false }, points: { show: false } },
      scales: {
        x: { time: false, range: () => [-0.5, n - 0.5] },
        y: { range: () => yRange(series) },
      },
      axes: [
        {
          ...axis,
          incrs: [1, 2, 5, 10, 20, 50, 100],
          values: (_u, splits) => splits.map((v) => (Number.isInteger(v) ? String(v + 1) : "")),
        },
        {
          ...axis,
          size: 56,
          label: unitText(series.unit, app.locale),
          values: (_u, splits) => splits.map((v) => formatNumber(Math.round(v * 100) / 100, app.locale)),
        },
      ],
      series: [
        {},
        { stroke: css("--text-muted"), width: 1.5, points: { show: false } },
      ],
      hooks: { drawClear: [bands], draw: [points], ready: [report], setSize: [report] },
    };
  }

  function build() {
    if (!host) return;
    plot?.destroy();
    plot = new uPlot(options(host.clientWidth || 600), chartData(series) as uPlot.AlignedData, host);
    plot.over.addEventListener("click", () => {
      const i = plot?.cursor.idx;
      if (i !== null && i !== undefined && i < series.points.length) onpick?.(i);
    });
  }

  onMount(() => {
    const resize = new ResizeObserver(() => host && plot?.setSize({ width: host.clientWidth, height: 280 }));
    if (host) resize.observe(host);
    return () => {
      resize.disconnect();
      plot?.destroy();
      plot = null;
    };
  });

  // New data, a theme or a language rebuilds the chart; a new pick only redraws it.
  $effect(() => {
    void [series, app.theme, app.locale];
    build();
  });
  $effect(() => {
    void picked;
    plot?.redraw(false);
  });
</script>

<div bind:this={host} class="plot" role="img" aria-label={label}></div>

<style>
  .plot {
    width: 100%;
    min-height: 280px;
    cursor: pointer;
  }
</style>
