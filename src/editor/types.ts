/** Katman tabanlı editörün paylaşılan veri tipleri.

 * Mimari karar (2026 araştırması): Fabric.js/Konva.js yerine özel motor —
 * iş yükümüz piksel-dominant (fırça/silgi/klon/sihirli değnek/filtreler
 * ImageData operasyonları); her iki kütüphane de sahne-grağı odaklıdır ve
 * bu araçları sağlamaz. Photopea da aynı gerekçeyle kendi motorunu kullanır
 * (kurucu Ivan Kutskir AMA: "I did not use such libraries, I made my own
 * system"). Kütüphaneler sağlıklı olsa da (Konva 10.3.1 / Fabric 7.3.0,
 * 2026, ikisi de MIT) yalnızca taşıyıcı olurlardı; transform tutamaçları ve
 * panel gibi küçük parçaları kendimiz yazmak toplam maliyeti düşürür.
 *
 * Render stratejisi: katman başına native çözünürlükte <canvas>, DOM'da
 * üst üste; opaklık/blend/görünürlük CSS ile (opacity / mix-blend-mode /
 * isolation), pan+zoom tek CSS transform güncellemesi → compositor işi,
 * animasyon karesinde JS piksel işi sıfır. Piksel okuma/yazma (getImageData)
 * yalnızca eylem sınırlarında; fırça darbesi dirty-rect ile küçük bbox'ta
 * kalır (kaynaklar: browser-rendering.com compositor rehberi,
 * interactive-data-visualization.com "dirty rectangle" bölümü).
 */

export type BlendMode =
  | "normal"
  | "multiply"
  | "screen"
  | "overlay"
  | "darken"
  | "lighten";

export const BLEND_MODES: BlendMode[] = [
  "normal",
  "multiply",
  "screen",
  "overlay",
  "darken",
  "lighten",
];

/** CSS mix-blend-mode karşılıkları (normal → source-over davranışı için
 * mix-blend-mode:normal). Panelde görünen Türkçe adlarla birlikte. */
export const BLEND_CSS: Record<BlendMode, string> = {
  normal: "normal",
  multiply: "multiply",
  screen: "screen",
  overlay: "overlay",
  darken: "darken",
  lighten: "lighten",
};

export type LayerKind = "background" | "text" | "raster";

/** Katmanın sayfa düzlemindeki yerleşimi. rotation derece, merkez pivotlu;
 * ölçek bağımsız eksenlerde (dönüştürme aracı Faz 2'de bunu düzenler). */
export interface LayerTransform {
  x: number;
  y: number;
  scaleX: number;
  scaleY: number;
  rotation: number;
}

export function identityTransform(): LayerTransform {
  return { x: 0, y: 0, scaleX: 1, scaleY: 1, rotation: 0 };
}

/** Katman üst verisi. project.json'da pages[i].result.layers[] olarak
 * kalıcılaşır (Rust Value pass-through; şema additive genişler). */
export interface LayerMeta {
  id: string;
  name: string;
  kind: LayerKind;
  visible: boolean;
  /** 0–100 arası tam sayı (panel kaydırıcısıyla birebir). */
  opacity: number;
  blend: BlendMode;
  locked: boolean;
  transform: LayerTransform;
  /** kind==="text" ise bağlı Region id'si (Region ana verinin sahibidir:
   * çeviri/stil Region'da yaşar, katman ondan türetilir). */
  regionId?: number | null;
  /** kind==="raster" ise içerik PNG'sinin pages/p{n}/ altındaki göreli yolu
   * (örn. "layers/u1.png"). Faz 3'te Rust write_page_png ile yazılır. */
  src?: string | null;
  /** İçerik sınırlı tuval kullanan katmanlar için bitmap boyutu:
   * metin katmanında ilk bbox boyutu, raster katmanda içerik dikdörtgeni.
   * Tam sayfa tuvale sahip arka planda boş (sayfa boyutu geçerli). */
  contentW?: number | null;
  contentH?: number | null;
}

/** Katman meta'sını eksiksizleştirir (eski kayıtlarda eksik alan olabilir). */
export function normalizeLayer(raw: Partial<LayerMeta> & { id: string }): LayerMeta {
  return {
    id: raw.id,
    name: raw.name ?? raw.id,
    kind: raw.kind === "background" || raw.kind === "text" ? raw.kind : "raster",
    visible: raw.visible !== false,
    opacity: clampNum(raw.opacity ?? 100, 0, 100),
    blend: BLEND_MODES.includes(raw.blend as BlendMode) ? (raw.blend as BlendMode) : "normal",
    locked: raw.locked === true,
    transform: {
      x: num(raw.transform?.x),
      y: num(raw.transform?.y),
      scaleX: posNum(raw.transform?.scaleX, 1),
      scaleY: posNum(raw.transform?.scaleY, 1),
      rotation: num(raw.transform?.rotation),
    },
    regionId: typeof raw.regionId === "number" ? raw.regionId : null,
    src: typeof raw.src === "string" ? raw.src : null,
    contentW: typeof raw.contentW === "number" && raw.contentW > 0 ? Math.round(raw.contentW) : null,
    contentH: typeof raw.contentH === "number" && raw.contentH > 0 ? Math.round(raw.contentH) : null,
  };
}

function num(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

/** Pozitif (sıfır hariç) sayı; geçersizse fallback. Ölçek için. */
function posNum(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) && v !== 0 ? v : fallback;
}

function clampNum(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, Math.round(v)));
}
