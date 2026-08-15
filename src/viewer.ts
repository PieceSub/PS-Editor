/** Sonuç görüntüleyici: kaydırmalı before/after karşılaştırıcı + yan yana mod.
 *
 * Araştırma notu: bağımlılıksız before/after slider için yerleşik desen, iki
 * görseli üst üste koyup üst katmanı tek bir `clip-path: inset(...)` ile
 * kırpmaktır; sürükleme ise üste yayılan native `<input type="range">` ile
 * çözülür (klavye/touch erişilebilirliği de bedavaya gelir).
 * Kaynaklar: dev.to/dev48v (clip-path + setPos deseni), cloudfour.com/thinks
 * (er işilebilir web component), CodeFronts (range-input --pos deseni).
 */

import { convertFileSrc } from "@tauri-apps/api/core";

export interface Region {
  id: number;
  index: number;
  label_name: string;
  bbox: number[];
  original: string;
  translation: string;
  font_size: number;
  lines: number;
  overflow: boolean;
}

export interface PageResult {
  job_id: string;
  image: string;
  target_language?: string;
  mode_decision?: { decision?: string; chosen_backend?: string; reason?: string };
  provider?: { name?: string; model?: string };
  regions: Region[];
  warnings: string[];
  timings_ms?: Record<string, number>;
  outputs: {
    translated: string;
    cleaned?: string | null;
    ocr_regions?: string | null;
    before_after?: string | null;
  };
}

export type ViewMode = "compare" | "side";

let resizeHandler: (() => void) | null = null;

window.addEventListener("resize", () => {
  resizeHandler?.();
});

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

function overflowRects(regions: Region[], imgW: number, imgH: number): HTMLElement {
  const layer = el("div", "ov-layer");
  for (const r of regions) {
    if (!r.overflow || r.bbox.length < 4) continue;
    const [x, y, w, h] = r.bbox;
    const box = el("div", "ov-rect");
    box.style.left = `${(x / imgW) * 100}%`;
    box.style.top = `${(y / imgH) * 100}%`;
    box.style.width = `${(w / imgW) * 100}%`;
    box.style.height = `${(h / imgH) * 100}%`;
    box.title = `${r.label_name || "Bölge"}: metin balon dışına taşıyor`;
    layer.appendChild(box);
  }
  return layer;
}

function imgFor(path: string, alt: string): HTMLImageElement {
  const img = el("img");
  img.src = convertFileSrc(path);
  img.alt = alt;
  img.draggable = false;
  return img;
}

/** Dikey manga sayfaları pencere yüksekliğine sığacak şekilde kırpılır. */
function fitStage(stage: HTMLElement, naturalW: number, naturalH: number): void {
  const maxW = Math.max(200, stage.parentElement?.clientWidth ?? 640);
  const maxH = Math.max(320, window.innerHeight - 320);
  let w = maxW;
  let h = (w * naturalH) / naturalW;
  if (h > maxH) {
    h = maxH;
    w = (h * naturalW) / naturalH;
  }
  stage.style.width = `${Math.round(w)}px`;
  stage.style.height = `${Math.round(h)}px`;
}

function buildCompareStage(
  beforePath: string,
  afterPath: string,
  regions: Region[],
  showOverflow: boolean,
): HTMLElement {
  const stage = el("div", "compare-stage");
  stage.style.setProperty("--pos", "50%");

  const after = imgFor(afterPath, "Çevrilmiş sayfa");
  after.className = "layer-img";
  const beforeClip = el("div", "before-clip");
  const before = imgFor(beforePath, "Orijinal sayfa");
  before.className = "layer-img";
  beforeClip.appendChild(before);

  const range = el("input");
  range.type = "range";
  range.min = "0";
  range.max = "100";
  range.value = "50";
  range.className = "compare-range";
  range.setAttribute("aria-label", "Karşılaştırma konumu");
  range.addEventListener("input", () => {
    stage.style.setProperty("--pos", `${range.value}%`);
  });

  const divider = el("div", "divider");
  divider.appendChild(el("span", "handle"));

  const tagBefore = el("span", "tag tag-before", "Orijinal");
  const tagAfter = el("span", "tag tag-after", "Çevrilmiş");

  stage.append(after, beforeClip, divider, tagBefore, tagAfter, range);

  const overlays = el("div", "ov-layer");
  stage.appendChild(overlays);

  let imgW = 1;
  let imgH = 1;
  const scaleOverlays = () => {
    const fresh = overflowRects(regions, imgW, imgH);
    fresh.style.display = showOverflow ? "" : "none";
    overlays.replaceChildren(fresh);
  };
  scaleOverlays();

  void Promise.all([
    after.decode().then(() => {
      imgW = after.naturalWidth || 1;
      imgH = after.naturalHeight || 1;
      fitStage(stage, imgW, imgH);
      resizeHandler = () => fitStage(stage, imgW, imgH);
      scaleOverlays();
    }).catch(() => undefined),
    before.decode().catch(() => undefined),
  ]);

  return stage;
}

/** Yan yana panelleri tek boyuta hizalar (overlay %'si görsel koordinatlarıyla örtüşsün diye). */
function fitSidePanels(grid: HTMLElement, naturalW: number, naturalH: number): void {
  const maxW = Math.max(320, grid.parentElement?.clientWidth ?? 680);
  const gap = 14;
  const maxH = Math.max(320, window.innerHeight - 320);
  let w = (maxW - gap) / 2;
  let h = (w * naturalH) / naturalW;
  if (h > maxH) {
    h = maxH;
    w = (h * naturalW) / naturalH;
  }
  grid.style.width = `${Math.round(w * 2 + gap)}px`;
  grid.style.height = `${Math.round(h)}px`;
}

function buildSideBySide(
  beforePath: string,
  afterPath: string,
  regions: Region[],
  showOverflow: boolean,
): HTMLElement {
  const grid = el("div", "side-grid");

  const beforePanel = el("figure", "side-panel");
  const beforeImg = imgFor(beforePath, "Orijinal sayfa");
  beforePanel.appendChild(beforeImg);
  beforePanel.appendChild(el("figcaption", "", "Orijinal"));

  const afterPanel = el("figure", "side-panel");
  const afterImg = imgFor(afterPath, "Çevrilmiş sayfa");
  afterPanel.appendChild(afterImg);
  afterPanel.appendChild(el("figcaption", "", "Çevrilmiş"));

  const overlays = el("div", "ov-layer");
  afterPanel.appendChild(overlays);

  void Promise.all([
    afterImg.decode().then(() => {
      const w = afterImg.naturalWidth || 1;
      const h = afterImg.naturalHeight || 1;
      fitSidePanels(grid, w, h);
      resizeHandler = () => fitSidePanels(grid, w, h);
      const fresh = overflowRects(regions, w, h);
      fresh.style.display = showOverflow ? "" : "none";
      overlays.replaceChildren(fresh);
    }).catch(() => undefined),
    beforeImg.decode().catch(() => undefined),
  ]);

  grid.append(beforePanel, afterPanel);
  return grid;
}

export function renderViewer(
  container: HTMLElement,
  page: PageResult,
  mode: ViewMode,
  showOverflow: boolean,
): void {
  const beforePath = page.image;
  const afterPath = page.outputs.translated;
  if (!beforePath || !afterPath) {
    container.replaceChildren();
    return;
  }
  resizeHandler = null;
  const view =
    mode === "compare"
      ? buildCompareStage(beforePath, afterPath, page.regions, showOverflow)
      : buildSideBySide(beforePath, afterPath, page.regions, showOverflow);
  container.replaceChildren(view);
}