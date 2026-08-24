/** DOM-bağımsız dikdörtgen yardımcıları (node testlerinde de çalışır).
 * Rect biçimi: [x, y, w, h] — Region.bbox ile aynı düzen (bbox[x,y,w,h]). */

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export function rect(x: number, y: number, w: number, h: number): Rect {
  return { x, y, w: Math.max(0, w), h: Math.max(0, h) };
}

/** İki dikdörtgenin birleşimini döndürür (boşken diğerini verir). */
export function unionRect(a: Rect | null, b: Rect | null): Rect | null {
  if (!a) return b ? { ...b } : null;
  if (!b) return { ...a };
  const x1 = Math.min(a.x, b.x);
  const y1 = Math.min(a.y, b.y);
  const x2 = Math.max(a.x + a.w, b.x + b.w);
  const y2 = Math.max(a.y + a.h, b.y + b.h);
  return { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
}

/** Kesişim; kesişmiyorsa null. */
export function intersectRect(a: Rect, b: Rect): Rect | null {
  const x1 = Math.max(a.x, b.x);
  const y1 = Math.max(a.y, b.y);
  const x2 = Math.min(a.x + a.w, b.x + b.w);
  const y2 = Math.min(a.y + a.h, b.y + b.h);
  if (x2 <= x1 || y2 <= y1) return null;
  return { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
}

/** Dikdörtgeni sınırlar içine kırpır; tamamen dışarıdaysa null. */
export function clampRect(r: Rect, bounds: Rect): Rect | null {
  return intersectRect(r, bounds);
}

/** Rect'i tam sayı piksel sınırlarına genişletir (putImageData tamsayı ister). */
export function intRect(r: Rect): Rect {
  const x2 = Math.ceil(r.x + r.w);
  const y2 = Math.ceil(r.y + r.h);
  return { x: Math.floor(r.x), y: Math.floor(r.y), w: x2 - Math.floor(r.x), h: y2 - Math.floor(r.y) };
}

export function rectsEqual(a: Rect, b: Rect): boolean {
  return a.x === b.x && a.y === b.y && a.w === b.w && a.h === b.h;
}
