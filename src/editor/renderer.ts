/** EditorEngine: katman tuvaleri, viewport ve overlay çizimi.

 * Render mimarisi (araştırma kararı — bkz. types.ts başlığı):
 *   - Her katman native çözünürlükte ayrı <canvas>; DOM sırası = çizim sırası.
 *     Opaklık → style.opacity, harmanlama → style.mix-blend-mode, stack
 *     kapsayıcısı isolation:isolate (blend'ler uygulama arka planına sızmaz).
 *   - Pan/zoom tek CSS transform güncellemesi; tarayıcı compositor'ı halleder,
 *     JS piksel işi yok (browser-rendering.com compositor rehberi).
 *   - Piksel işlemleri (Faz 3 fırça vb.) dirty-rect ile küçük bbox'ta kalır;
 *     getImageData yalnızca eylem sınırlarında çağrılır (GPU flush riski).
 *   - Metin katmanları Region verisinden client-side üretilir (backend ile
 *     aynı font: ComicNeue-Bold; kontur kalınlığı PIL stroke_width ile eşleşir).
 */

import type { EditorDocument } from "./doc";
import { textLayerSize } from "./doc";
import type { LayerMeta } from "./types";
import type { Region } from "../viewer";

/** Region stil varsayılanlarının yerel kopyası (viewer.ts'e bağımlılık
 * kurulmasın; backend RegionStyleDefaults ile aynı değerler). */
const STYLE_DEFAULTS = {
  font_weight: "bold" as "bold" | "normal",
  color: null as string | null,
  font_size_override: null as number | null,
  align: "center" as "left" | "center" | "right",
};

const MIN_FONT = 9;
const MAX_FONT = 36;
const LINE_HEIGHT = 1.18;

export interface EngineDeps {
  /** Tauri asset protokolüne çevrilmiş görsel URL'i üretici. */
  resolveUrl: (path: string) => string;
}

let comicNeueReady = false;

/** Paket içindeki ComicNeue-Bold'u FontFace olarak kaydeder; başarısızsa
 * sessizce sistem bold'una düşer (metin düzenleme çalışmaya devam eder). */
export async function ensureEditorFonts(): Promise<void> {
  if (comicNeueReady || typeof document === "undefined") return;
  try {
    const face = new FontFace("Comic Neue", "url('fonts/ComicNeue-Bold.ttf')", {
      weight: "bold",
    });
    await face.load();
    document.fonts.add(face);
    comicNeueReady = true;
  } catch {
    comicNeueReady = true; // tekrar deneme spam'ini engelle
  }
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

export class EditorEngine {
  readonly doc: EditorDocument;
  private deps: EngineDeps;

  readonly stage: HTMLDivElement;
  private stack: HTMLDivElement;
  private overlay: HTMLCanvasElement;
  private overlayCtx: CanvasRenderingContext2D | null;

  zoom = 1;
  panX = 0;
  panY = 0;

  private canvases = new Map<string, HTMLCanvasElement>();
  private images = new Map<string, HTMLImageElement>();
  private thumbHooks = new Set<(id: string) => void>();
  private overlayQueued = false;
  private destroyed = false;

  constructor(host: HTMLElement, doc: EditorDocument, deps: EngineDeps) {
    this.doc = doc;
    this.deps = deps;

    this.stage = el("div", "ed-stage");
    this.stage.appendChild(el("div", "ed-checker"));

    this.stack = el("div", "ed-stack");
    this.stack.style.isolation = "isolate";
    this.stack.style.width = `${doc.width}px`;
    this.stack.style.height = `${doc.height}px`;
    this.stage.appendChild(this.stack);

    this.overlay = el("canvas", "ed-overlay");
    this.overlayCtx = this.overlay.getContext("2d");
    this.stage.appendChild(this.overlay);

    host.appendChild(this.stage);

    this.syncDom();
    this.applyViewportTransform();

    this.stage.addEventListener("wheel", this.onWheel, { passive: false });
    this.stage.addEventListener("pointerdown", this.onPointerDown);
    window.addEventListener("resize", this.onWindowResize);
    window.addEventListener("keydown", this.onKeyDown);
    window.addEventListener("keyup", this.onKeyUp);
    this.resizeOverlay();
    this.buildZoomControls();
  }

  /* --------------------------------------------------- zoom kontrolleri */

  private zoomLabel: HTMLSpanElement | null = null;

  private buildZoomControls(): void {
    const pill = el("div", "ed-zoom-pill");
    const minus = el("button", "ed-zoom-btn", "−");
    minus.type = "button";
    minus.title = "Uzaklaştır (Ctrl+tekerlek)";
    minus.addEventListener("click", () => this.setZoom(this.zoom / 1.25));
    this.zoomLabel = el("span", "ed-zoom-label", "100%");
    this.zoomLabel.title = "Yakınlaştırma seviyesi";
    const plus = el("button", "ed-zoom-btn", "+");
    plus.type = "button";
    plus.title = "Yakınlaştır (Ctrl+tekerlek)";
    plus.addEventListener("click", () => this.setZoom(this.zoom * 1.25));
    const fit = el("button", "ed-zoom-btn", "Sığdır");
    fit.type = "button";
    fit.title = "Sayfayı görünüme sığdır";
    fit.addEventListener("click", () => this.fit());
    const one = el("button", "ed-zoom-btn", "1:1");
    one.type = "button";
    one.title = "Gerçek boyut (%100)";
    one.addEventListener("click", () => this.setZoom(1));
    pill.append(minus, this.zoomLabel, plus, fit, one);
    this.stage.appendChild(pill);
  }

  private updateZoomLabel(): void {
    if (this.zoomLabel) this.zoomLabel.textContent = `${Math.round(this.zoom * 100)}%`;
  }

  /* --------------------------------------------------------- pan (taşıma) */

  private spaceDown = false;
  private panDrag: { x: number; y: number; px: number; py: number } | null = null;

  private onKeyDown = (ev: KeyboardEvent): void => {
    if (ev.code !== "Space") return;
    const t = ev.target as HTMLElement | null;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    this.spaceDown = true;
    this.stage.classList.add("pan-ready");
  };

  private onKeyUp = (ev: KeyboardEvent): void => {
    if (ev.code !== "Space") return;
    this.spaceDown = false;
    this.stage.classList.remove("pan-ready");
  };

  private onPointerDown = (ev: PointerEvent): void => {
    // Orta tuş veya Space basılıyken sol tuş = pan (araçlara bırakılmaz).
    if (ev.button !== 1 && !(ev.button === 0 && this.spaceDown)) return;
    ev.preventDefault();
    this.panDrag = { x: ev.clientX, y: ev.clientY, px: this.panX, py: this.panY };
    this.stage.setPointerCapture(ev.pointerId);
    this.stage.classList.add("panning");
    const move = (e: PointerEvent): void => {
      if (!this.panDrag) return;
      this.panX = this.panDrag.px + (e.clientX - this.panDrag.x);
      this.panY = this.panDrag.py + (e.clientY - this.panDrag.y);
      this.applyViewportTransform();
    };
    const up = (): void => {
      this.panDrag = null;
      this.stage.classList.remove("panning");
      this.stage.removeEventListener("pointermove", move);
      this.stage.removeEventListener("pointerup", up);
      this.stage.removeEventListener("pointercancel", up);
    };
    this.stage.addEventListener("pointermove", move);
    this.stage.addEventListener("pointerup", up);
    this.stage.addEventListener("pointercancel", up);
  };

  /* ------------------------------------------------------------ katmanlar */

  /** Doc'taki katmanlara göre DOM çocuklarını eşitler.
   *  Doc index 0 = en üstte görünen; DOM'da en son eklenen üstte olduğundan
   *  TERST sırada eklenir. */
  private syncDom(): void {
    const seen = new Set<string>();
    for (let i = this.doc.layers.length - 1; i >= 0; i--) {
      const l = this.doc.layers[i];
      seen.add(l.id);
      let c = this.canvases.get(l.id);
      if (!c) {
        c = this.createCanvasFor(l);
        this.canvases.set(l.id, c);
      }
      const cs = c.style;
      const cw = l.contentW ?? this.doc.width;
      const chh = l.contentH ?? this.doc.height;
      cs.width = `${cw}px`;
      cs.height = `${chh}px`;
      cs.opacity = String(clamp(l.opacity, 0, 100) / 100);
      cs.mixBlendMode = l.blend === "normal" ? "normal" : l.blend;
      cs.display = l.visible ? "" : "none";
      // Katman yerleşimi: translate(x,y) + merkez pivotlu rotate/scale.
      // flatten() ile aynı matematik (CSS origin: %50 %50 = içerik merkezi).
      const t = l.transform;
      cs.left = `${t.x}px`;
      cs.top = `${t.y}px`;
      cs.transformOrigin = "50% 50%";
      cs.transform =
        t.rotation || t.scaleX !== 1 || t.scaleY !== 1
          ? `rotate(${t.rotation}deg) scale(${t.scaleX}, ${t.scaleY})`
          : "";
    }
    // Silinmiş katmanların tuvallerini kaldır ve sırayı yeniden kur.
    for (const [id, c] of [...this.canvases]) {
      if (!seen.has(id)) {
        c.remove();
        this.canvases.delete(id);
        continue;
      }
      // Doğru z sırası: doc'taki konumuna göre yeniden bağla (append = üste).
      this.stack.appendChild(c);
    }
    this.invalidateOverlay();
  }

  private createCanvasFor(l: LayerMeta): HTMLCanvasElement {
    const c = el("canvas", "ed-layer-canvas");
    const cw = l.contentW ?? this.doc.width;
    const chh = l.contentH ?? this.doc.height;
    c.width = cw;
    c.height = chh;
    return c;
  }

  /** Katman bitmap'ini (varsa) diskten/bağlı kaynaktan yükler.
   * background → src dosyası; text → Region'dan üretim; raster → src varsa dosya. */
  async loadLayer(l: LayerMeta): Promise<void> {
    const c = this.canvases.get(l.id);
    if (!c) return;
    const ctx = c.getContext("2d");
    if (!ctx) return;
    if (l.kind === "text") {
      await this.renderTextLayer(l, ctx);
      return;
    }
    const src = l.src;
    if (!src) return;
    try {
      const img = await this.loadImage(src);
      ctx.clearRect(0, 0, c.width, c.height);
      ctx.drawImage(img, 0, 0, c.width, c.height);
      this.notifyBitmap(l.id);
    } catch {
      /* görsel yüklenemedi: katman boş kalır */
    }
  }

  async loadAll(): Promise<void> {
    await ensureEditorFonts();
    // Alttan üste yükle: arka plan en kritik görseldir, önce görünsün.
    for (let i = this.doc.layers.length - 1; i >= 0; i--) {
      await this.loadLayer(this.doc.layers[i]);
    }
    this.invalidateOverlay();
  }

  private loadImage(absOrRel: string): Promise<HTMLImageElement> {
    const cached = this.images.get(absOrRel);
    if (cached) return Promise.resolve(cached);
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        this.images.set(absOrRel, img);
        resolve(img);
      };
      img.onerror = () => reject(new Error(`görsel yüklenemedi: ${absOrRel}`));
      img.src = this.deps.resolveUrl(absOrRel);
    });
  }

  /** Metin katmanı bitmap'ini Region (çeviri+stil+bbox) verisinden üretir. */
  async renderTextLayer(l: LayerMeta, ctx: CanvasRenderingContext2D): Promise<void> {
    const rid = l.regionId;
    const region = rid != null ? this.docRegions.find((r) => r.id === rid) : undefined;
    const { w, h } = textLayerSize(l);
    ctx.clearRect(0, 0, w, h);
    this.notifyBitmap(l.id);
    if (!region || !region.translation.trim()) return;
    const st = { ...STYLE_DEFAULTS, ...(region.style ?? {}) };
    const autoSize =
      st.font_size_override ??
      region.font_size ??
      clamp(Math.round(h * 0.3), MIN_FONT, MAX_FONT);

    await (document.fonts?.load(`${st.font_weight === "bold" ? "700" : "400"} ${Math.max(12, Math.round(h * 0.3))}px "Comic Neue"`) ?? Promise.resolve());
    const fitted = fitText(ctx, region.translation.trim(), Math.max(4, w), Math.max(4, h), autoSize, st.font_weight === "bold");
    const size = fitted.size;
    ctx.font = `${st.font_weight === "bold" ? "700" : "400"} ${size}px "Comic Neue", sans-serif`;
    ctx.textBaseline = "alphabetic";
    const lh = size * LINE_HEIGHT;
    const strokeW = Math.max(2, Math.round(size * 0.10));
    const fill = st.color ?? "#000000";
    const strokeFill = st.color
      ? luma(st.color) >= 140
        ? "#ffffff"
        : "#000000"
      : "#ffffff";
    ctx.lineJoin = "round";
    ctx.strokeStyle = strokeFill;
    ctx.lineWidth = strokeW * 2; // PIL stroke_width dışa taşar; canvas ortalanır
    ctx.fillStyle = fill;
    const totalH = fitted.lines.length * lh;
    let y = (h - totalH) / 2 + size * 0.86; // ilk satır tabanına yaklaştır
    for (const line of fitted.lines) {
      const tw = ctx.measureText(line).width;
      const x =
        st.align === "left" ? 0 : st.align === "right" ? w - tw : (w - tw) / 2;
      if (strokeW > 0) ctx.strokeText(line, x, y);
      ctx.fillText(line, x, y);
      y += lh;
    }
    this.notifyBitmap(l.id);
  }

  /** Index.ts tarafından bağlanır: metin render için Region kaynakları. */
  docRegions: Region[] = [];

  /** Bitmap değişim aboneliği (panel thumbnail yenilemesi). */
  onBitmapChanged(cb: (id: string) => void): () => void {
    this.thumbHooks.add(cb);
    return () => this.thumbHooks.delete(cb);
  }

  private notifyBitmap(id: string): void {
    for (const cb of this.thumbHooks) cb(id);
  }

  layerCanvas(id: string): HTMLCanvasElement | null {
    return this.canvases.get(id) ?? null;
  }

  /** Doc olaylarına tepki: DOM sırası/stil tazele. */
  refreshDom(): void {
    this.syncDom();
  }

  /** Katmanın tuvalini içerik boyutuna göre yeniden oluşturur (metin
   * bbox değişimi vb.) ve içeriği yeniden çizer. */
  async rebuildLayer(l: LayerMeta): Promise<void> {
    const old = this.canvases.get(l.id);
    if (old) {
      old.remove();
      this.canvases.delete(l.id);
    }
    this.syncDom();
    await this.loadLayer(l);
  }

  /* ------------------------------------------------------------- viewport */

  private applyViewportTransform(): void {
    this.stack.style.transformOrigin = "0 0";
    this.stack.style.transform = `translate(${this.panX}px, ${this.panY}px) scale(${this.zoom})`;
    this.invalidateOverlay();
  }

  private onWindowResize = (): void => {
    this.resizeOverlay();
    this.invalidateOverlay();
  };

  private resizeOverlay(): void {
    const r = this.stage.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.overlay.width = Math.max(1, Math.round(r.width * dpr));
    this.overlay.height = Math.max(1, Math.round(r.height * dpr));
    if (this.overlayCtx) this.overlayCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  /** Ekran (client) koordinatını doküman pikseline çevirir. getBoundingClientRect
   * CSS transform'u kapsadığı için matris takibi gerekmez. */
  screenToDoc(clientX: number, clientY: number): { x: number; y: number } {
    const r = this.stack.getBoundingClientRect();
    return {
      x: ((clientX - r.left) / r.width) * this.doc.width,
      y: ((clientY - r.top) / r.height) * this.doc.height,
    };
  }

  setZoom(z: number, anchorX?: number, anchorY?: number): void {
    const nz = clamp(z, 0.05, 8);
    if (nz === this.zoom) return;
    const sr = this.stack.getBoundingClientRect();
    const ax = anchorX ?? sr.left + sr.width / 2;
    const ay = anchorY ?? sr.top + sr.height / 2;
    const dx = (ax - sr.left) / this.zoom;
    const dy = (ay - sr.top) / this.zoom;
    this.zoom = nz;
    this.applyViewportTransform();
    const nr = this.stack.getBoundingClientRect();
    this.panX += ax - nr.left - dx * nz;
    this.panY += ay - nr.top - dy * nz;
    this.applyViewportTransform();
    this.updateSmoothing();
    this.updateZoomLabel();
  }

  /** %100 üstünde keskin piksel (manga çizgi netliği), altında yumuşatma. */
  private updateSmoothing(): void {
    this.stack.classList.toggle("pixelated", this.zoom > 1.01);
  }

  fit(): void {
    const pad = 28;
    const r = this.stage.getBoundingClientRect();
    const z = Math.min(
      (r.width - pad * 2) / this.doc.width,
      (r.height - pad * 2) / this.doc.height,
      1,
    );
    this.zoom = clamp(Math.max(z, 0.02), 0.02, 8);
    this.panX = (r.width - this.doc.width * this.zoom) / 2;
    this.panY = (r.height - this.doc.height * this.zoom) / 2;
    this.applyViewportTransform();
    this.updateSmoothing();
    this.updateZoomLabel();
  }

  panBy(dxPx: number, dyPx: number): void {
    this.panX += dxPx;
    this.panY += dyPx;
    this.applyViewportTransform();
  }

  private onWheel = (ev: WheelEvent): void => {
    ev.preventDefault();
    if (ev.ctrlKey || ev.metaKey) {
      const factor = ev.deltaY < 0 ? 1.1 : 1 / 1.1;
      this.setZoom(this.zoom * factor, ev.clientX, ev.clientY);
    } else {
      this.panBy(-ev.deltaX, -ev.deltaY);
    }
  };

  /* -------------------------------------------------------------- overlay */

  /** Seçili katman çerçevesi vb. UI çizimi (screen-space, DPR keskin). */
  invalidateOverlay(): void {
    if (this.overlayQueued || this.destroyed) return;
    this.overlayQueued = true;
    requestAnimationFrame(() => {
      this.overlayQueued = false;
      this.drawOverlay();
    });
  }

  private drawOverlay(): void {
    const ctx = this.overlayCtx;
    if (!ctx) return;
    const r = this.stage.getBoundingClientRect();
    ctx.clearRect(0, 0, r.width, r.height);
    const active = this.doc.active;
    if (!active || !active.visible) return;
    const c = this.canvases.get(active.id);
    if (!c) return;
    const sr = this.stack.getBoundingClientRect();
    const sx = sr.left - r.left;
    const sy = sr.top - r.top;
    const k = this.zoom;
    const t = active.transform;
    ctx.save();
    ctx.strokeStyle = "#d92525";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([5, 4]);
    ctx.strokeRect(sx + t.x * k, sy + t.y * k, c.width * t.scaleX * k, c.height * t.scaleY * k);
    ctx.restore();
  }

  /* ------------------------------------------------------------ yardımcı */

  /** Panel önizlemesi: katmanı hedef tuvale contain-fit çizer. */
  drawThumbnail(target: HTMLCanvasElement, id: string): void {
    const src = this.canvases.get(id);
    const ctx = target.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, target.width, target.height);
    if (!src) return;
    const k = Math.min(target.width / src.width, target.height / src.height);
    const w = src.width * k;
    const h = src.height * k;
    ctx.imageSmoothingQuality = "medium";
    ctx.drawImage(src, (target.width - w) / 2, (target.height - h) / 2, w, h);
  }

  /** Tüm görünür katmanları tek tuvale birleştirir (export/flatten, Faz 5).
   * Alttan üste çizer; harmanlama canvas globalCompositeOperation ile
   * CSS mix-blend-mode ile aynı isimlendirilir. */
  flatten(): HTMLCanvasElement {
    const out = el("canvas");
    out.width = this.doc.width;
    out.height = this.doc.height;
    const ctx = out.getContext("2d");
    if (ctx) {
      for (let i = this.doc.layers.length - 1; i >= 0; i--) {
        const l = this.doc.layers[i];
        if (!l.visible || l.opacity <= 0) continue;
        const c = this.canvases.get(l.id);
        if (!c) continue;
        ctx.save();
        ctx.globalAlpha = clamp(l.opacity, 0, 100) / 100;
        ctx.globalCompositeOperation =
          l.blend === "normal" ? "source-over" : (l.blend as GlobalCompositeOperation);
        const t = l.transform;
        if (
          t.rotation !== 0 ||
          t.scaleX !== 1 ||
          t.scaleY !== 1 ||
          t.x !== 0 ||
          t.y !== 0
        ) {
          const cx = t.x + (c.width * t.scaleX) / 2;
          const cy = t.y + (c.height * t.scaleY) / 2;
          ctx.translate(cx, cy);
          if (t.rotation) ctx.rotate((t.rotation * Math.PI) / 180);
          ctx.scale(t.scaleX, t.scaleY);
          ctx.drawImage(c, -c.width / 2, -c.height / 2);
        } else {
          ctx.drawImage(c, 0, 0);
        }
        ctx.restore();
      }
    }
    return out;
  }

  destroy(): void {
    this.destroyed = true;
    this.stage.removeEventListener("wheel", this.onWheel);
    this.stage.removeEventListener("pointerdown", this.onPointerDown);
    window.removeEventListener("resize", this.onWindowResize);
    window.removeEventListener("keydown", this.onKeyDown);
    window.removeEventListener("keyup", this.onKeyUp);
    this.stage.parentElement?.removeChild(this.stage);
    this.canvases.clear();
    this.images.clear();
    this.thumbHooks.clear();
  }
}

/* --------------------------------------------------------------- metin uyumu */

interface FittedText {
  size: number;
  lines: string[];
}

/** Çeviriyi verilen kutuya sığdırmak için punto küçülterek satırlara böler
 * (backend auto-fit davranışının ön yüz karşılığı). */
function fitText(
  ctx: CanvasRenderingContext2D,
  text: string,
  maxW: number,
  maxH: number,
  startSize: number,
  bold: boolean,
): FittedText {
  let size = clamp(startSize, MIN_FONT, MAX_FONT);
  let lines = wrapLines(ctx, text, maxW, size, bold);
  while (size > MIN_FONT && lines.length * size * LINE_HEIGHT > maxH * 1.06) {
    size -= 1;
    ctx.font = `${bold ? "700" : "400"} ${size}px "Comic Neue", sans-serif`;
    lines = wrapLines(ctx, text, maxW, size, bold);
  }
  return { size, lines };
}

function wrapLines(
  ctx: CanvasRenderingContext2D,
  text: string,
  maxW: number,
  size: number,
  bold: boolean,
): string[] {
  ctx.font = `${bold ? "700" : "400"} ${size}px "Comic Neue", sans-serif`;
  const words = text.split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  const lines: string[] = [];
  let cur = "";
  for (const word of words) {
    const cand = cur ? `${cur} ${word}` : word;
    if (ctx.measureText(cand).width <= maxW || !cur) cur = cand;
    else {
      lines.push(cur);
      cur = word;
    }
  }
  if (cur) lines.push(cur);
  return lines;
}

function luma(hex: string): number {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!m) return 0;
  const n = parseInt(m[1], 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
