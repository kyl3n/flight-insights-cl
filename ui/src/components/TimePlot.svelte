<script lang="ts">
  // One plot over a flight's time (uPlot, as the spec sets for time series): each trace
  // as the band between its bins' lowest and highest samples, so a spike shows even
  // though the trace is reduced for display. Behind it, the hover windows are shaded,
  // the time outside the armed span dimmed, mode changes drawn as lines and logged
  // errors as red ticks; an optional level is drawn dashed. Spans of time can be shaded in
  // their own colours (the flight phases) and outlined dashed (the fast run). Every plot of a flight
  // shares one cursor, so the same moment lines up across them.
  import { onMount } from "svelte";
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import { app } from "../lib/app.svelte";
  import { align, type Trace } from "../lib/flightviews";
  import { formatNumber, unitText } from "../lib/report";

  let {
    traces,
    unit,
    range,
    armed,
    hover = [],
    modes = [],
    errors = [],
    level = null,
    spans = [],
    outlined = [],
    label,
    height = 170,
  }: {
    traces: Trace[];
    unit: string;
    range: [number, number];
    armed: [number, number];
    hover?: [number, number][];
    modes?: { t: number; mode: string }[];
    errors?: number[];
    level?: number | null;
    spans?: { start: number; end: number; colour: string }[];
    outlined?: [number, number][];
    label: string;
    height?: number;
  } = $props();

  let host: HTMLDivElement | undefined = $state();
  let plot: uPlot | null = null;

  const css = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";

  function background(u: uPlot) {
    const ctx = u.ctx;
    const { left, top, width, height: h } = u.bbox;
    const x = (s: number) => u.valToPos(s, "x", true);
    ctx.save();
    ctx.beginPath();
    ctx.rect(left, top, width, h);
    ctx.clip();
    ctx.fillStyle = css("--text-faint");
    ctx.globalAlpha = 0.08;
    ctx.fillRect(left, top, x(armed[0]) - left, h);
    ctx.fillRect(x(armed[1]), top, left + width - x(armed[1]), h);
    ctx.fillStyle = css("--accent");
    ctx.globalAlpha = 0.1;
    for (const [a, b] of hover) ctx.fillRect(x(a), top, x(b) - x(a), h);
    ctx.globalAlpha = 0.22;
    for (const s of spans) {
      ctx.fillStyle = s.colour;
      ctx.fillRect(x(s.start), top, x(s.end) - x(s.start), h);
    }
    ctx.globalAlpha = 1;
    const dpr0 = devicePixelRatio || 1;
    ctx.strokeStyle = css("--accent");
    ctx.lineWidth = dpr0;
    ctx.setLineDash([3 * dpr0, 3 * dpr0]);
    for (const [a, b] of outlined) ctx.strokeRect(x(a), top + dpr0, x(b) - x(a), h - 2 * dpr0);
    ctx.setLineDash([]);
    const dpr = devicePixelRatio || 1;
    ctx.font = `${11 * dpr}px sans-serif`;
    for (const m of modes) {
      ctx.strokeStyle = css("--border");
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x(m.t), top);
      ctx.lineTo(x(m.t), top + h);
      ctx.stroke();
      ctx.fillStyle = css("--text-faint");
      ctx.fillText(m.mode, x(m.t) + 3 * dpr, top + 11 * dpr);
    }
    ctx.strokeStyle = css("--out");
    ctx.lineWidth = 2 * dpr;
    for (const t of errors) {
      ctx.beginPath();
      ctx.moveTo(x(t), top + h - 8 * dpr);
      ctx.lineTo(x(t), top + h);
      ctx.stroke();
    }
    if (level !== null) {
      const y = u.valToPos(level, "y", true);
      ctx.strokeStyle = css("--caution");
      ctx.lineWidth = 1;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(left + width, y);
      ctx.stroke();
    }
    ctx.restore();
  }

  function padded(lo: number, hi: number): [number, number] {
    // A flat trace gets room around it, so its axis still reads.
    const pad = hi > lo ? (hi - lo) * 0.08 : (Math.abs(hi) || 1) * 0.5;
    return [lo - pad, hi + pad];
  }

  function options(width: number): uPlot.Options {
    const grid = { stroke: css("--border-soft"), width: 1 };
    const axis = { stroke: css("--text-faint"), grid, ticks: grid };
    const series: uPlot.Series[] = [{}];
    const bands: uPlot.Band[] = [];
    traces.forEach((tr, i) => {
      series.push({ label: tr.label, stroke: tr.colour, width: 1, spanGaps: true, points: { show: false } });
      series.push({ label: tr.label, stroke: tr.colour, width: 1, spanGaps: true, points: { show: false } });
      bands.push({ series: [2 + 2 * i, 1 + 2 * i], fill: `${tr.colour}33` });
    });
    return {
      width,
      height,
      legend: { show: false },
      cursor: { sync: { key: "flight" }, drag: { x: true, y: false }, points: { show: false } },
      scales: {
        x: { time: false, range: () => range },
        // The level is part of the picture: a limit the data never reaches still shows.
        y: { range: (_u, lo, hi) => padded(Math.min(lo, level ?? lo), Math.max(hi, level ?? hi)) },
      },
      axes: [
        {
          ...axis,
          label: unitText("s", app.locale),
          values: (_u, splits) => splits.map((v) => formatNumber(Math.round(v), app.locale)),
        },
        {
          ...axis,
          size: 60,
          label: unitText(unit, app.locale),
          values: (_u, splits) => splits.map((v) => formatNumber(Number(v.toPrecision(4)), app.locale)),
        },
      ],
      series,
      bands,
      hooks: { drawClear: [background] },
    };
  }

  function build() {
    if (!host) return;
    plot?.destroy();
    plot = new uPlot(options(host.clientWidth || 600), align(traces) as uPlot.AlignedData, host);
  }

  onMount(() => {
    const resize = new ResizeObserver(() => host && plot?.setSize({ width: host.clientWidth, height }));
    if (host) resize.observe(host);
    return () => {
      resize.disconnect();
      plot?.destroy();
      plot = null;
    };
  });

  $effect(() => {
    void [traces, range, level, spans, outlined, app.theme, app.locale];
    build();
  });
</script>

<div bind:this={host} class="plot" role="img" aria-label={label}></div>

<style>
  .plot {
    width: 100%;
    min-height: 120px;
  }
</style>
