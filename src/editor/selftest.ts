/** Çekirdek birim testleri (DOM yok — node'da çalışır).
 *  Çalıştırma: node scripts/core-tests.mjs
 *
 *  Kapsam: katman türetme (deriveFromPage), kayıtla uzlaştırma (fromSaved),
 *  sıralama kuralları (arka plan her zaman altta), serileştirme turu,
 *  normalizeLayer savunmacılığı ve rect yardımcıları. */

import assert from "node:assert/strict";
import { EditorDocument, textLayerId } from "./doc";
import { normalizeLayer } from "./types";
import { unionRect, intersectRect, clampRect, intRect } from "./core/rect";

let passed = 0;
function test(name: string, fn: () => void): void {
  fn();
  passed++;
  console.log(`  ✓ ${name}`);
}

const page = {
  image: "/abs/pages/p0/original.png",
  outputs: { cleaned: "/abs/pages/p0/cleaned.png", translated: "/abs/pages/p0/translated.png" },
  regions: [
    { id: 1, bbox: [10, 20, 200, 80], translation: "Merhaba", disabled: false },
    { id: 2, bbox: [30, 140, 180, 60], translation: "", disabled: true },
    { id: 3, bbox: [50, 260, 220, 90], translation: "Üçüncü", disabled: false },
  ],
};

test("deriveFromPage: bg kilitli ve en altta", () => {
  const doc = EditorDocument.deriveFromPage(page);
  assert.equal(doc.layers[doc.layers.length - 1].id, "bg");
  assert.equal(doc.layers[doc.layers.length - 1].kind, "background");
  assert.equal(doc.layers[doc.layers.length - 1].locked, true);
  assert.equal(doc.layers[doc.layers.length - 1].src, page.outputs.cleaned);
});

test("deriveFromPage: metin katmanları üstten alta [t3,t2,t1,bg]", () => {
  const doc = EditorDocument.deriveFromPage(page);
  assert.deepEqual(
    doc.layers.map((l) => l.id),
    ["t3", "t2", "t1", "bg"],
  );
});

test("metin katmanı: bbox konumu transform'da, disabled gizli", () => {
  const doc = EditorDocument.deriveFromPage(page);
  const t1 = doc.byId(textLayerId(1))!;
  assert.equal(t1.transform.x, 10);
  assert.equal(t1.transform.y, 20);
  assert.equal(t1.contentW, 200);
  const t2 = doc.byId(textLayerId(2))!;
  assert.equal(t2.visible, false);
});

test("fromSaved: kayıt korunur, yeni region arka planın üstüne eklenir", () => {
  const saved = [
    normalizeLayer({ id: "u1", name: "Çizimim", kind: "raster" }),
    normalizeLayer({ id: "t3", name: "Metin 3", kind: "text" }),
    normalizeLayer({ id: "bg", name: "Arka Plan", kind: "background" }),
  ];
  const doc = EditorDocument.fromSaved(page, saved);
  const ids = doc.layers.map((l) => l.id);
  // t1/t2 kayıtta yok → bg'nin üstüne, yenisi en üstte olacak şekilde.
  assert.deepEqual(ids, ["u1", "t3", "t2", "t1", "bg"]);
  assert.equal(doc.byId("bg")!.locked, true);
});

test("applyOrder: arka plan sona zorlanır", () => {
  const doc = EditorDocument.deriveFromPage(page);
  doc.applyOrder(["bg", "t1", "t3", "t2"]);
  assert.deepEqual(
    doc.layers.map((l) => l.id),
    ["t1", "t3", "t2", "bg"],
  );
});

test("moveBy: bg taşınamaz, sınır dışına çıkılmaz", () => {
  const doc = EditorDocument.deriveFromPage(page);
  doc.moveBy("bg", -1);
  assert.equal(doc.layers[doc.layers.length - 1].id, "bg");
  doc.moveBy("t3", -5); // zaten en üstte
  assert.equal(doc.layers[0].id, "t3");
  doc.moveBy("t3", 1);
  assert.equal(doc.layers[0].id, "t2");
});

test("insert: ref katmanın hemen üstüne yerleşir", () => {
  const doc = EditorDocument.deriveFromPage(page);
  doc.insert(normalizeLayer({ id: "u9", kind: "raster" }), "t1");
  const ids = doc.layers.map((l) => l.id);
  assert.equal(ids[ids.indexOf("t1") - 1], "u9"); // görsel olarak üstte
});

test("remove: bg silinemez; aktif düşer", () => {
  const doc = EditorDocument.deriveFromPage(page);
  assert.equal(doc.remove("bg"), false);
  doc.setActive("t2");
  doc.remove("t2");
  assert.equal(doc.byId("t2"), null);
  assert.notEqual(doc.activeId, "t2");
});

test("toJSON→normalizeLayer turu bilgi kaybetmez", () => {
  const doc = EditorDocument.deriveFromPage(page);
  const json = JSON.parse(JSON.stringify(doc.toJSON()));
  const back = json.map((l: any) => normalizeLayer(l));
  assert.deepEqual(back, doc.layers);
});

test("normalizeLayer: bozuk değerler varsayılana çöker", () => {
  const l = normalizeLayer({
    id: "x",
    opacity: 250,
    blend: "yok" as never,
    transform: { scaleX: NaN as never } as never,
  });
  assert.equal(l.opacity, 100);
  assert.equal(l.blend, "normal");
  assert.equal(l.transform.scaleX, 1);
  assert.equal(l.visible, true);
  assert.equal(l.kind, "raster");
});

test("rect: union / intersect / clamp / int", () => {
  assert.deepEqual(unionRect({ x: 0, y: 0, w: 10, h: 10 }, { x: 5, y: 5, w: 10, h: 10 }), {
    x: 0, y: 0, w: 15, h: 15,
  });
  assert.deepEqual(intersectRect({ x: 0, y: 0, w: 10, h: 10 }, { x: 20, y: 20, w: 5, h: 5 }), null);
  assert.deepEqual(clampRect({ x: -5, y: -5, w: 20, h: 20 }, { x: 0, y: 0, w: 100, h: 100 }), {
    x: 0, y: 0, w: 15, h: 15,
  });
  assert.deepEqual(intRect({ x: 1.2, y: 3.7, w: 4.4, h: 2.2 }), {
    x: 1, y: 3, w: 5, h: 3,
  });
});

console.log(`\n${passed} test geçti ✓`);
