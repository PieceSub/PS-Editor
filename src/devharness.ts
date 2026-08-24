/** Geliştirme düzeneği: Tauri olmadan, düz tarayıcıda katman editörünü
 * çalıştırmak için (npm run dev → http://localhost:1420/test-editor.html).
 *
 * Sentetik PageResult public/test/ altındaki görselleri kullanır
 * (scripts/make_harness_pages.py üretir); resolveUrl düz yol döndürür.
 */

import "./styles.css";
import { mountEditor } from "./editor/index";
import type { PageResult } from "./viewer";

const page: PageResult = {
  job_id: "harness",
  image: "/test/page.png",
  regions: [
    {
      id: 1,
      index: 0,
      label_name: "balon1",
      bbox: [170, 340, 900, 560],
      original: "",
      translation: "The storm is coming! Run while you still can.",
      font_size: null,
      lines: 1,
      overflow: false,
      committed: true,
    },
    {
      id: 2,
      index: 1,
      label_name: "balon2",
      bbox: [1290, 330, 860, 520],
      original: "",
      translation: "We should head back before dark.",
      font_size: null,
      lines: 1,
      overflow: false,
      committed: true,
      style: { color: "#7a1515" },
    },
    {
      id: 3,
      index: 2,
      label_name: "balon3",
      bbox: [140, 1490, 920, 580],
      original: "",
      translation: "Look at the horizon...",
      font_size: null,
      lines: 1,
      overflow: false,
      committed: true,
    },
    {
      id: 4,
      index: 3,
      label_name: "balon4",
      bbox: [1310, 1450, 880, 600],
      original: "",
      translation: "It is already too late.",
      font_size: null,
      lines: 1,
      overflow: false,
      disabled: true,
      committed: true,
    },
  ],
  warnings: [],
  outputs: {
    translated: "/test/page_translated.png",
    cleaned: "/test/page_cleaned.png",
  },
};

const app = document.getElementById("app");
if (!app) throw new Error("#app bulunamadı");

const t0 = performance.now();
const handle = mountEditor(app, {
  page,
  resolveUrl: (p) => p,
  onPersist: (layers) => console.log("[harness] persist", layers),
});

// Durum çubuğu: yüklenme süresi + katman sayısı (görsel doğrulama için)
const status = document.createElement("div");
status.style.cssText =
  "position:fixed;top:2px;left:8px;z-index:99;font:11px monospace;color:#0f0;background:rgba(0,0,0,.6);padding:2px 6px;border-radius:4px";
document.body.appendChild(status);
const enginePoll = window.setInterval(() => {
  if (handle.engine && handle.doc) {
    window.clearInterval(enginePoll);
    void handle.engine.loadAll().then(() => {
      const ms = (performance.now() - t0).toFixed(0);
      status.textContent = `yükleme ${ms} ms · ${handle.doc!.layers.length} katman · sayfa 2480x3500 · zoom ${(handle.engine!.zoom * 100).toFixed(0)}%`;
      console.log(`[harness] ilk yükleme + loadAll: ${ms} ms`);
      // URL paramları: ?zoom=1.5 (merkeze yakınlaştır), ?active=t2
      const q = new URLSearchParams(location.search);
      const z = Number(q.get("zoom"));
      if (Number.isFinite(z) && z > 0) handle.engine!.setZoom(z);
      const act = q.get("active");
      if (act && handle.doc!.byId(act)) handle.doc!.setActive(act);
    });
  }
}, 50);
