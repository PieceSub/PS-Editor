/** Katman tabanlı editörün montaj noktası.

 * Kullanım (main.ts): sayfa açıldığında mountEditor(container, opts) çağrılır;
 * dönen handle destroy() ile sökülür. Katman üst verisindeki her kullanıcı
 * değişimi onPersist'e iletilir → main.ts debounced autosave yapar
 * (önceki adımlardaki eylem-bazlı kayıt kuralı korunur).
 *
 * Yol çözümleme notu: layers[].src manifestte göreli tutulur ("pages/p{n}/…")
 * ve outputs.translated'ın dizinine göre mutlaklaştırılır; böylece proje
 * klasörü taşındığında yollar bozulmaz (projects.rs rel/abs kuralıyla uyumlu).
 */

import { EditorDocument } from "./doc";
import { EditorEngine, ensureEditorFonts } from "./renderer";
import { LayersPanel } from "./layerspanel";
import type { LayerMeta } from "./types";
import type { PageResult } from "../viewer";

export interface EditorOptions {
  page: PageResult;
  /** Tauri asset protokolü URL'i (viewer.pageImageUrl ile aynı imza). */
  resolveUrl: (path: string, ver?: number) => string;
  /** Katman meta'sı değişti → kalıcılaşacak (debounce main.ts'te). */
  onPersist: (layers: LayerMeta[]) => void;
}

export interface EditorHandle {
  doc: EditorDocument | null;
  engine: EditorEngine | null;
  panel: LayersPanel | null;
  destroy(): void;
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

/** Sayfa dizinini çıkarır: translated "…/pages/p3/translated.png" ise
 * sonuç "…/pages/p3". */
function pageDirOf(page: PageResult): string {
  const p = page.outputs.translated || page.image || "";
  const i = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
  return i > 0 ? p.slice(0, i) : "";
}

function isAbsolute(p: string): boolean {
  return /^([A-Za-z]:[\\/]|[\\/])/.test(p);
}

export function mountEditor(host: HTMLElement, o: EditorOptions): EditorHandle {
  const page = o.page;
  host.replaceChildren();

  const wrap = el("div", "ed-workspace");
  const stageHost = el("div", "ed-stage-host");
  const side = el("div", "ed-side");
  wrap.append(stageHost, side);
  host.appendChild(wrap);

  const hint = el("div", "ed-loading", "Sayfa hazırlanıyor…");
  stageHost.appendChild(hint);

  let engine: EditorEngine | null = null;
  let panel: LayersPanel | null = null;
  let doc: EditorDocument | null = null;
  let persistQueued = false;

  const queuePersist = (): void => {
    if (!doc || persistQueued) return;
    persistQueued = true;
    window.setTimeout(() => {
      persistQueued = false;
      o.onPersist(doc!.toJSON());
    }, 250);
  };

  const handle: EditorHandle = {
    get doc() {
      return doc;
    },
    get engine() {
      return engine;
    },
    get panel() {
      return panel;
    },
    destroy(): void {
      panel?.destroy();
      panel = null;
      engine?.destroy();
      engine = null;
      host.replaceChildren();
    },
  };

  void (async () => {
    await ensureEditorFonts();

    // Arka plan görselini önce ölç → gerçek sayfa boyutu bellidir.
    const bgSrc = page.outputs.cleaned || page.image;
    let w = 800;
    let h = 1200;
    try {
      const img = await new Promise<HTMLImageElement>((resolve, reject) => {
        const im = new Image();
        im.onload = () => resolve(im);
        im.onerror = () => reject(new Error("arka plan yüklenemedi"));
        im.src = o.resolveUrl(bgSrc);
      });
      w = img.naturalWidth || 800;
      h = img.naturalHeight || 1200;
    } catch {
      /* boyut tahminiyle devam */
    }

    const saved = (page as PageResult & { layers?: Partial<LayerMeta>[] }).layers ?? [];
    doc =
      saved.length > 0
        ? EditorDocument.fromSaved(page, saved)
        : EditorDocument.deriveFromPage(page);
    doc.width = w;
    doc.height = h;

    hint.remove();
    engine = new EditorEngine(stageHost, doc, { resolveUrl: (p) => o.resolveUrl(p) });
    engine.docRegions = page.regions;

    // Kayıtlı raster kaynakları göreliyse mutlaklaştır (translated dizinine göre).
    const dir = pageDirOf(page);
    for (const l of doc.layers) {
      if (l.src && !isAbsolute(l.src)) l.src = `${dir}/${l.src}`;
    }

    panel = new LayersPanel(doc, engine, { onUserChange: queuePersist });
    side.appendChild(panel.root);

    await engine.loadAll();
    engine.fit();
    doc.on((ev) => {
      if (ev.type === "structure" || ev.type === "meta") engine?.refreshDom();
    });
  })();

  return handle;
}

export type { LayerMeta };
