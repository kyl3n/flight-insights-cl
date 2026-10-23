<script lang="ts">
  // The whole flight's gyro spectrogram (WebGL, as the spec sets for spectrograms;
  // plain canvas where WebGL is missing), with the motor frequency from ESC RPM and the
  // logged notch centre drawn over it, and the analysed segment marked on the time axis.
  import { onMount } from "svelte";
  import { app, t } from "../lib/app.svelte";
  import { formatNumber, unitText } from "../lib/report";
  import { pixels, type Spectrogram } from "../lib/workbench";

  let { data, spans }: { data: Spectrogram; spans: [number, number][] } = $props();

  let image: HTMLCanvasElement | undefined = $state();
  let overlay: HTMLCanvasElement | undefined = $state();
  let cursor = $state<{ t: number; hz: number } | null>(null);

  const top = $derived(data.freqs.at(-1) ?? 0);
  const end = $derived(data.t_s.at(-1) ?? 0);
  const start = $derived(data.t_s[0] ?? 0);

  function drawGL(canvas: HTMLCanvasElement): boolean {
    const gl = canvas.getContext("webgl");
    if (!gl) return false;
    const img = pixels(data);
    canvas.width = img.width;
    canvas.height = img.height;
    gl.viewport(0, 0, img.width, img.height);
    const vs = gl.createShader(gl.VERTEX_SHADER)!;
    gl.shaderSource(vs, "attribute vec2 p; varying vec2 uv; void main(){ uv=(p+1.0)/2.0; uv.y=1.0-uv.y; gl_Position=vec4(p,0,1); }");
    gl.compileShader(vs);
    const fs = gl.createShader(gl.FRAGMENT_SHADER)!;
    gl.shaderSource(fs, "precision mediump float; varying vec2 uv; uniform sampler2D tex; void main(){ gl_FragColor=texture2D(tex, uv); }");
    gl.compileShader(fs);
    const prog = gl.createProgram()!;
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return false;
    gl.useProgram(prog);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(prog, "p");
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, img.width, img.height, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array(img.data.buffer));
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    return true;
  }

  function draw2D(canvas: HTMLCanvasElement) {
    const img = pixels(data);
    canvas.width = img.width;
    canvas.height = img.height;
    canvas.getContext("2d")?.putImageData(new ImageData(img.data, img.width, img.height), 0, 0);
  }

  function drawOverlay(canvas: HTMLCanvasElement) {
    const w = (canvas.width = canvas.clientWidth);
    const h = (canvas.height = canvas.clientHeight);
    const ctx = canvas.getContext("2d");
    if (!ctx || !top) return;
    const x = (s: number) => ((s - start) / Math.max(end - start, 1e-9)) * w;
    const y = (hz: number) => h - (hz / top) * h;
    ctx.fillStyle = "rgba(90,176,232,0.18)";
    for (const [a, b] of spans) ctx.fillRect(x(a), h - 6, Math.max(x(b) - x(a), 2), 6);
    const line = (values: (number | null)[] | undefined, colour: string, dash: number[], multiple = 1) => {
      if (!values) return;
      ctx.strokeStyle = colour;
      ctx.lineWidth = 1.5;
      ctx.setLineDash(dash);
      ctx.beginPath();
      let drawing = false;
      values.forEach((v, i) => {
        const s = data.t_s[i];
        if (v === null || s === undefined) {
          drawing = false;
          return;
        }
        const px = x(s);
        const py = y(v * multiple);
        if (drawing) ctx.lineTo(px, py);
        else ctx.moveTo(px, py);
        drawing = true;
      });
      ctx.stroke();
      ctx.setLineDash([]);
    };
    line(data.motor_hz, "rgba(255,255,255,0.75)", []);
    line(data.motor_hz, "rgba(255,255,255,0.45)", [], 2);
    line(data.notch_hz, "rgba(90,176,232,0.9)", [5, 4]);
  }

  onMount(() => {
    if (image && !drawGL(image)) draw2D(image);
    if (overlay) drawOverlay(overlay);
    const resize = new ResizeObserver(() => overlay && drawOverlay(overlay));
    if (overlay) resize.observe(overlay);
    return () => resize.disconnect();
  });

  function move(e: MouseEvent) {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const fx = (e.clientX - r.left) / r.width;
    const fy = 1 - (e.clientY - r.top) / r.height;
    cursor = { t: start + fx * (end - start), hz: fy * top };
  }
</script>

<figure>
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="stack" onmousemove={move} onmouseleave={() => (cursor = null)}>
    <canvas bind:this={image} class="image" aria-hidden="true"></canvas>
    <canvas bind:this={overlay} class="overlay" aria-hidden="true"></canvas>
    <span class="axis top">{Math.round(top)} {unitText("Hz", app.locale)}</span>
    <span class="axis bottom">0 {unitText("Hz", app.locale)}</span>
  </div>
  <figcaption>
    <span>{t("bench.spectrogram.caption")}</span>
    {#if cursor}<span class="mono">{t("bench.cursor", { t: formatNumber(Math.round(cursor.t * 10) / 10, app.locale), hz: Math.round(cursor.hz) })}</span>{/if}
  </figcaption>
</figure>

<style>
  figure {
    margin: 0;
  }
  .stack {
    position: relative;
    height: 220px;
    border-radius: 8px;
    overflow: hidden;
    background: #0c1220;
  }
  canvas {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
  }
  .image {
    image-rendering: auto;
  }
  .axis {
    position: absolute;
    left: 6px;
    font-size: 11px;
    color: rgba(255, 255, 255, 0.75);
  }
  .top {
    top: 4px;
  }
  .bottom {
    bottom: 8px;
  }
  figcaption {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    font-size: 12px;
    color: var(--text-muted);
    margin-top: 6px;
  }
</style>
