/** Katmanlar paneli: Photoshop/GIMP düzeninde liste + önizleme +
 * görünürlük/kilit/opaklık/harmanlama kontrolleri, sürükle-bırak sıralama.
 *
 * Etkileşim kararları:
 *   - Satır sıralaması doc.layers ile birebir (index 0 en üstte).
 *   - Sıralama için hem HTML5 drag-drop hem ↑/↓ düğmeleri (erişilebilirlik).
 *   - Metin katmanları silinemez (Region akışını kırmamak için); arka plan
 *     taşınamaz/silinmez. Bunlar Faz 6'da metin aracıyla zenginleşecek.
 */

import type { EditorDocument } from "./doc";
import type { EditorEngine } from "./renderer";
import { BLEND_MODES, type BlendMode, type LayerMeta } from "./types";

export interface LayersPanelCallbacks {
  /** Kullanıcı yapıyı/meta'yı değiştirdiğinde (autosave tetikleme). */
  onUserChange(): void;
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

function btn(className: string, text: string, title: string): HTMLButtonElement {
  const b = el("button", className);
  b.type = "button";
  b.textContent = text;
  b.title = title;
  return b;
}

const THUMB_BOX = 44;

export class LayersPanel {
  private doc: EditorDocument;
  private engine: EditorEngine;
  private cbs: LayersPanelCallbacks;

  readonly root: HTMLElement;
  private list: HTMLDivElement;
  private opacityRange: HTMLInputElement | null = null;
  private opacityVal: HTMLSpanElement | null = null;
  private blendSel: HTMLSelectElement | null = null;
  private renameInput: HTMLInputElement | null = null;

  private unsubs: Array<() => void> = [];
  private dragId: string | null = null;

  constructor(doc: EditorDocument, engine: EditorEngine, cbs: LayersPanelCallbacks) {
    this.doc = doc;
    this.engine = engine;
    this.cbs = cbs;

    this.root = el("div", "ed-panel card");
    const head = el("div", "ed-panel-head");
    head.appendChild(el("span", "", "Katmanlar"));
    this.root.appendChild(head);

    this.list = el("div", "ed-list");
    this.list.addEventListener("dragover", (ev) => {
      ev.preventDefault();
    });
    this.list.addEventListener("drop", (ev) => {
      ev.preventDefault();
      this.handleDrop();
    });
    this.root.appendChild(this.list);

    // Aktif katman özellikleri
    const props = el("div", "ed-props");
    const opRow = el("div", "ed-prop-row");
    opRow.appendChild(el("span", "ed-prop-label", "Opaklık"));
    this.opacityRange = el("input") as HTMLInputElement;
    this.opacityRange.type = "range";
    this.opacityRange.min = "0";
    this.opacityRange.max = "100";
    this.opacityRange.value = "100";
    this.opacityRange.addEventListener("input", () => {
      const a = this.doc.active;
      if (!a) return;
      const v = Number(this.opacityRange?.value ?? 100);
      this.doc.patch(a.id, { opacity: v });
      if (this.opacityVal) this.opacityVal.textContent = `${v}%`;
    });
    this.opacityRange.addEventListener("change", () => this.cbs.onUserChange());
    opRow.appendChild(this.opacityRange);
    this.opacityVal = el("span", "ed-prop-value", "100%");
    opRow.appendChild(this.opacityVal);
    props.appendChild(opRow);

    const blendRow = el("div", "ed-prop-row");
    blendRow.appendChild(el("span", "ed-prop-label", "Harmanlama"));
    this.blendSel = el("select") as HTMLSelectElement;
    for (const m of BLEND_MODES) {
      const opt = el("option");
      opt.value = m;
      opt.textContent = BLEND_LABEL[m];
      this.blendSel.appendChild(opt);
    }
    this.blendSel.addEventListener("change", () => {
      const a = this.doc.active;
      if (!a) return;
      this.doc.patch(a.id, { blend: (this.blendSel?.value as BlendMode) ?? "normal" });
      this.cbs.onUserChange();
    });
    blendRow.appendChild(this.blendSel);
    props.appendChild(blendRow);
    this.root.appendChild(props);

    // Eylemler
    const actions = el("div", "ed-actions");
    const addBtn = btn("btn secondary small", "+ Katman", "Yeni boş çizim katmanı ekler");
    addBtn.addEventListener("click", () => this.addRasterLayer());
    actions.appendChild(addBtn);

    const delBtn = btn("btn ghost danger small", "Sil", "Seçili katmanı siler");
    delBtn.addEventListener("click", () => this.deleteActive());
    actions.appendChild(delBtn);
    this.root.appendChild(actions);

    this.unsubs.push(
      doc.on((ev) => {
        switch (ev.type) {
          case "structure":
          case "active":
            this.refresh();
            break;
          case "meta":
            this.syncPropControls();
            break;
          case "content":
            this.refreshThumb(ev.id);
            break;
        }
      }),
    );
    this.unsubs.push(engine.onBitmapChanged((id) => this.refreshThumb(id)));

    this.refresh();
  }

  /* --------------------------------------------------------------- satırlar */

  refresh(): void {
    this.list.replaceChildren();
    this.renameInput = null;
    for (const l of this.doc.layers) this.list.appendChild(this.buildRow(l));
    this.syncPropControls();
  }

  private buildRow(l: LayerMeta): HTMLElement {
    const row = el("div", "ed-row");
    row.dataset.id = l.id;
    if (l.id === this.doc.activeId) row.classList.add("active");
    if (!l.visible) row.classList.add("hidden-layer");

    const grip = el("span", "ed-grip", "≡");
    if (l.kind !== "background") {
      row.draggable = true;
      grip.title = "Sürükleyerek sırala";
      row.addEventListener("dragstart", (ev) => {
        this.dragId = l.id;
        ev.dataTransfer?.setData("text/plain", l.id);
        row.classList.add("dragging");
      });
      row.addEventListener("dragend", () => {
        row.classList.remove("dragging");
        this.dragId = null;
      });
      row.addEventListener("dragenter", () => {
        if (this.dragId && this.dragId !== l.id) row.classList.add("drop-target");
      });
      row.addEventListener("dragleave", () => row.classList.remove("drop-target"));
    }
    row.appendChild(grip);

    const thumbWrap = el("div", "ed-thumb");
    const tc = document.createElement("canvas");
    tc.width = THUMB_BOX;
    tc.height = THUMB_BOX;
    tc.dataset.thumbFor = l.id;
    thumbWrap.appendChild(tc);
    row.appendChild(thumbWrap);
    this.refreshThumb(l.id);

    const name = el("span", "ed-name", l.name);
    name.title = `${l.name}${l.locked ? " · kilitli" : ""}`;
    name.addEventListener("dblclick", () => this.startRename(row, l));
    row.appendChild(name);

    const eye = btn("ed-eye" + (l.visible ? " on" : ""), l.visible ? "●" : "○", l.visible ? "Gizle" : "Göster");
    eye.setAttribute("aria-label", `${l.name} görünürlüğü`);
    eye.addEventListener("click", (e) => {
      e.stopPropagation();
      this.doc.patch(l.id, { visible: !l.visible });
      this.cbs.onUserChange();
    });
    row.appendChild(eye);

    if (l.kind !== "background") {
      const lock = btn("ed-lock" + (l.locked ? " on" : ""), l.locked ? "🔒" : "🔓", l.locked ? "Kilidi aç" : "Kilitle");
      lock.setAttribute("aria-label", `${l.name} kilidi`);
      lock.addEventListener("click", (e) => {
        e.stopPropagation();
        this.doc.patch(l.id, { locked: !l.locked });
        this.cbs.onUserChange();
      });
      row.appendChild(lock);
    }

    row.addEventListener("pointerdown", () => {
      if (this.renameInput) this.commitRename();
      this.doc.setActive(l.id);
    });

    // Ok tuşlarıyla taşıma (dnd erişilebilir alternatifi)
    if (l.kind !== "background") {
      const moves = el("span", "ed-move-btns");
      const up = btn("ed-mini", "↑", "Bir üste taşı");
      up.addEventListener("click", (e) => {
        e.stopPropagation();
        this.doc.moveBy(l.id, -1);
        this.cbs.onUserChange();
      });
      const down = btn("ed-mini", "↓", "Bir alta taşı");
      down.addEventListener("click", (e) => {
        e.stopPropagation();
        this.doc.moveBy(l.id, 1);
        this.cbs.onUserChange();
      });
      moves.append(up, down);
      row.appendChild(moves);
    }

    return row;
  }

  private startRename(row: HTMLElement, l: LayerMeta): void {
    if (this.renameInput) this.commitRename();
    const input = el("input") as HTMLInputElement;
    input.className = "ed-rename";
    input.type = "text";
    input.value = l.name;
    const nameEl = row.querySelector<HTMLElement>(".ed-name");
    if (!nameEl) return;
    nameEl.replaceWith(input);
    this.renameInput = input;
    input.dataset.forId = l.id;
    input.focus();
    input.select();
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        this.commitRename();
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        this.renameInput = null;
        this.refresh();
      }
      ev.stopPropagation(); // araç kısayollarıyla çakışmasın
    });
    input.addEventListener("blur", () => this.commitRename());
  }

  private commitRename(): void {
    const input = this.renameInput;
    if (!input) return;
    this.renameInput = null;
    const id = input.dataset.forId ?? "";
    const layer = this.doc.byId(id);
    const name = input.value.trim();
    if (layer && name && name !== layer.name) {
      this.doc.patch(id, { name });
      this.cbs.onUserChange();
    } else {
      this.refresh();
    }
  }

  private handleDrop(): void {
    const targetRow = this.list.querySelector<HTMLElement>(".ed-row.drop-target");
    targetRow?.classList.remove("drop-target");
    const srcId = this.dragId;
    this.dragId = null;
    if (!srcId || !targetRow) return;
    const dstId = targetRow.dataset.id;
    if (!dstId || dstId === srcId) return;
    const ids = this.doc.layers.map((l) => l.id);
    const from = ids.indexOf(srcId);
    let to = ids.indexOf(dstId);
    if (from < 0 || to < 0) return;
    ids.splice(from, 1);
    to = ids.indexOf(dstId) + (from > to ? 0 : 1);
    ids.splice(to, 0, srcId);
    this.doc.applyOrder(ids);
    this.cbs.onUserChange();
  }

  /* ----------------------------------------------------------- özellikler */

  private syncPropControls(): void {
    const a = this.doc.active;
    const disabled = !a || a.kind === "background";
    if (this.opacityRange && this.opacityVal) {
      this.opacityRange.disabled = disabled;
      this.opacityRange.value = String(a?.opacity ?? 100);
      this.opacityVal.textContent = `${a?.opacity ?? 100}%`;
    }
    if (this.blendSel) {
      this.blendSel.disabled = disabled;
      this.blendSel.value = a?.blend ?? "normal";
    }
  }

  refreshThumb(id: string): void {
    const tc = this.list.querySelector<HTMLCanvasElement>(`canvas[data-thumb-for="${CSS.escape(id)}"]`);
    if (tc) this.engine.drawThumbnail(tc, id);
  }

  /* ------------------------------------------------------------- eylemler */

  /** Benzersiz raster katman kimliği (u1, u2, …). */
  private nextUserId(): string {
    let max = 0;
    for (const l of this.doc.layers) {
      const m = /^u(\d+)$/.exec(l.id);
      if (m) max = Math.max(max, Number(m[1]));
    }
    return `u${max + 1}`;
  }

  addRasterLayer(): void {
    const n = this.doc.layers.filter((l) => /^u\d+$/.test(l.id)).length + 1;
    const above = this.doc.activeId;
    const layer: LayerMeta = {
      id: this.nextUserId(),
      name: `Katman ${n}`,
      kind: "raster",
      visible: true,
      opacity: 100,
      blend: "normal",
      locked: false,
      transform: { x: 0, y: 0, scaleX: 1, scaleY: 1, rotation: 0 },
      regionId: null,
      src: null,
      contentW: this.doc.width,
      contentH: this.doc.height,
    };
    this.doc.insert(layer, above ?? undefined);
    this.doc.setActive(layer.id);
    this.cbs.onUserChange();
  }

  deleteActive(): void {
    const a = this.doc.active;
    if (!a) return;
    if (a.kind === "background") return; // arka plan silinemez
    if (a.kind === "text") {
      // Metin katmanı Region'a bağlıdır; silme akışı klasik editörde kalır.
      window.setTimeout(() => {
        alert("Metin katmanları bu sürümde panelden silinemez. Bölgeyi klasik editörden silebilirsiniz.");
      }, 0);
      return;
    }
    this.doc.remove(a.id);
    this.cbs.onUserChange();
  }

  destroy(): void {
    for (const u of this.unsubs.splice(0)) u();
    this.root.remove();
  }
}

export const BLEND_LABEL: Record<BlendMode, string> = {
  normal: "Normal",
  multiply: "Çarpma",
  screen: "Ekran",
  overlay: "Kaplama",
  darken: "Karart",
  lighten: "Aydınlat",
};
