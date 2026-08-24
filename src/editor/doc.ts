/** EditorDocument: bir sayfanın katman yığınının DOM-bağımsız modeli.

 * Sorumluluklar:
 *   - Katman sırası (index 0 = en üstte çizilen; panelde de üstten aşağı)
 *   - Aktif katman seçimi ve meta güncellemeleri
 *   - PageResult'tan ilk katman yığınını TÜRETME (eski sayfalarla uyumluluk)
 *   - Kayıtlı layers[] ile birleştirme (pipeline sonrası yeni region'lar)
 *   - Serileştirme (project.json'a yazılacak biçim)
 *
 * Bilinçli olarak canvas/DOM KULLANMAZ — bitmap'ler engine'de, bu sınıf
 * saf meta tutar (node testleri doğrudan çalıştırabilir).
 */

import {
  identityTransform,
  normalizeLayer,
  type LayerMeta,
} from "./types";

export interface RegionLike {
  id: number;
  bbox: number[];
  translation: string;
  disabled?: boolean;
  committed?: boolean;
}

export interface PageLike {
  image: string;
  regions: RegionLike[];
  outputs: { translated?: string | null; cleaned?: string | null };
}

/** Doc olayları: panel/renderer'ı haberdar eder. */
export type DocEvent =
  | { type: "structure" } // sıra/ekleme/silme
  | { type: "meta"; id: string } // görünürlük/opaklık/blend/kilit/ad
  | { type: "active"; id: string | null }
  | { type: "content"; id: string }; // bitmap içeriği değişti (Faz 3+)

export class EditorDocument {
  /** Gerçek sayfa boyutu; arka plan görseli ölçülünce index.ts günceller. */
  width: number;
  height: number;
  /** index 0 = en üstte çizilen katman. */
  layers: LayerMeta[] = [];
  activeId: string | null = null;

  private listeners = new Set<(ev: DocEvent) => void>();

  constructor(width: number, height: number) {
    this.width = Math.max(1, Math.round(width));
    this.height = Math.max(1, Math.round(height));
  }

  on(cb: (ev: DocEvent) => void): () => void {
    this.listeners.add(cb);
    return () => this.listeners.delete(cb);
  }

  emit(ev: DocEvent): void {
    for (const cb of this.listeners) cb(ev);
  }

  get active(): LayerMeta | null {
    return this.layers.find((l) => l.id === this.activeId) ?? null;
  }

  byId(id: string): LayerMeta | null {
    return this.layers.find((l) => l.id === id) ?? null;
  }

  setActive(id: string | null): void {
    if (this.activeId === id) return;
    this.activeId = id;
    this.emit({ type: "active", id });
  }

  /** Yeni katmanı referans katmanın (görsel olarak) HEMEN ÜSTÜNE ekler;
   * refId yoksa en üste. Dönen değer ekleme index'idir. */
  insert(layer: LayerMeta, refId?: string | null): number {
    const base =
      refId != null ? this.layers.findIndex((l) => l.id === refId) : -1;
    const idx = base < 0 ? 0 : base;
    this.layers.splice(idx, 0, layer);
    this.emit({ type: "structure" });
    return idx;
  }

  remove(id: string): boolean {
    const i = this.layers.findIndex((l) => l.id === id);
    if (i < 0 || this.layers[i].kind === "background") return false;
    this.layers.splice(i, 1);
    if (this.activeId === id) this.setActive(this.layers[Math.min(i, this.layers.length - 1)]?.id ?? null);
    this.emit({ type: "structure" });
    return true;
  }

  /** Sürükle-bırak sonrası tam sırayı uygular (id listesi üstten alta). */
  applyOrder(ids: string[]): void {
    const map = new Map(this.layers.map((l) => [l.id, l]));
    const next: LayerMeta[] = [];
    for (const id of ids) {
      const l = map.get(id);
      if (l) next.push(l);
      map.delete(id);
    }
    for (const rest of map.values()) next.push(rest); // kayıp id varsa koru
    // Arka plan her zaman en altta kalır (taşınamaz kuralı).
    const bgIdx = next.findIndex((l) => l.kind === "background");
    if (bgIdx >= 0 && bgIdx !== next.length - 1) {
      const [bg] = next.splice(bgIdx, 1);
      next.push(bg);
    }
    this.layers = next;
    this.emit({ type: "structure" });
  }

  moveBy(id: string, delta: number): void {
    const i = this.layers.findIndex((l) => l.id === id);
    if (i < 0) return;
    if (this.layers[i].kind === "background") return;
    const j = i + delta;
    if (j < 0 || j >= this.layers.length) return;
    if (this.layers[j].kind === "background") return;
    [this.layers[i], this.layers[j]] = [this.layers[j], this.layers[i]];
    this.emit({ type: "structure" });
  }

  patch(id: string, changes: Partial<LayerMeta>, ev: DocEvent = { type: "meta", id }): void {
    const l = this.byId(id);
    if (!l) return;
    Object.assign(l, changes);
    this.emit(ev);
  }

  /** project.json'a yazılacak düz liste. */
  toJSON(): LayerMeta[] {
    return this.layers.map((l) => ({ ...l, transform: { ...l.transform } }));
  }

  /* ------------------------------------------------------------ türetme */

  /** PageResult'tan ilk katman yığını kurar.
   *  - Sıra: üstten alta [son region, …, ilk region, arka plan]
   *    (backend typeset sırayla çizer; son bölge en üstte kalır).
   *  - Arka plan: cleaned (yoksa orijinal) — kilitli.
   *  Dönen doc henüz bitmap'sizdir; içerik engine'de yüklenir. */
  static deriveFromPage(page: PageLike): EditorDocument {
    const w = page.regions.reduce((m, r) => Math.max(m, r.bbox[2] ?? 0), 100);
    const h = page.regions.reduce((m, r) => Math.max(m, r.bbox[3] ?? 0), 100);
    // Boyut bilgisi region'lardan çıkarılamayabilir; index.ts görsel
    // yüklenince gerçek boyutla günceller. Buradaki değer ilk tahmin.
    const doc = new EditorDocument(w, h);

    const bgSrc = page.outputs.cleaned || page.image;
    const bg: LayerMeta = {
      ...normalizeLayer({ id: "bg", name: "Arka Plan", kind: "background" }),
      locked: true,
      src: bgSrc,
    };
    doc.layers = [...page.regions.map(textLayerFor).reverse(), bg];
    doc.activeId = doc.layers.length > 1 ? doc.layers[0].id : null;
    return doc;
  }

  /** Kayıtlı layers[] ile mevcut region kümesini uzlaştırır.
   *  - Kayıtlı katmanlar aynen korunur (sıra dahil).
   *  - Kayıtta olmayan region'lar için yeni metin katmanları arka planın
   *    hemen üstüne eklenir.
   *  - Kayıttaki yetim regionId'ler (region silinmiş) gizli bırakılır. */
  static fromSaved(page: PageLike, saved: Partial<LayerMeta>[]): EditorDocument {
    const doc = EditorDocument.deriveFromPage(page);
    const norm = saved.map((s) => normalizeLayer(s as LayerMeta & { id: string }));
    const known = new Set(norm.map((l) => l.id));
    const newLayers: LayerMeta[] = [];
    for (const r of page.regions) {
      if (!known.has(textLayerId(r.id))) newLayers.push(textLayerFor(r));
    }
    // Arka planın hemen üstü = bg index'i (üstten-alta listede bg'nin üstü).
    const bgIdx = norm.findIndex((l) => l.kind === "background");
    const insertAt = bgIdx >= 0 ? bgIdx : norm.length;
    norm.splice(insertAt, 0, ...newLayers.reverse());
    doc.layers = norm.map((l) =>
      l.kind === "background" ? { ...l, locked: true } : l,
    );
    doc.emit({ type: "structure" });
    return doc;
  }
}

export function textLayerId(regionId: number): string {
  return `t${regionId}`;
}

/** Region'dan metin katmanı meta'sı üretir. Konum bbox sol-üst köşesi;
 * tuval bbox boyutunda oluşturulur (contentW/H), transform ile yerleşir. */
export function textLayerFor(r: RegionLike): LayerMeta {
  const [x, y, w, h] = r.bbox.length >= 4 ? r.bbox : [0, 0, 10, 10];
  const t = identityTransform();
  t.x = x;
  t.y = y;
  return normalizeLayer({
    id: textLayerId(r.id),
    name: `Metin ${r.id}`,
    kind: "text",
    visible: !r.disabled,
    transform: t,
    regionId: r.id,
    contentW: Math.max(4, Math.round(w)),
    contentH: Math.max(4, Math.round(h)),
  });
}

/** Metin katmanının içerik (bbox) boyutunu okur. */
export function textLayerSize(l: LayerMeta): { w: number; h: number } {
  return { w: l.contentW ?? 10, h: l.contentH ?? 10 };
}
