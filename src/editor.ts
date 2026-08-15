/** Bölge editörü (adım 7): otomatik sonucun elle düzeltilebildiği katman.

 * UX deseni kaynağı: BallonsTranslator (github.com/dmMaze/BallonsTranslator
 * README_EN.md) — "metin düzenleme modu + sahnede blok seçimi + yeni bloğu
 * sürükle-çiz + modül bazlı re-render". KitsuTL 2026 karşılaştırması da aynı
 * akışı doğrular: "review → fix → adjust typesetting → re-render".
 *
 * Etkileşim modeli:
 *   - Kutular % tabanlı overlay div'leri (viewer.ts'teki taşma vurgusuyla aynı
 *     yöntem; görsel koordinatlarıyla birebir örtüşür, kütüphane gerekmez).
 *   - Kutunun içi pointer-events:auto; dışı 'none' — böylece görselin üstünde
 *     sürükle-çiz çalışır (çizim, tıklama ve seçili kutuyu taşıma).
 *   - Canlı önizleme: seçili bölgenin içine stil uygulanmış bir metin div'i
 *     konur; yazarken/tarz değiştirirken anında güncellenir. Kesin sonuç
 *     backend "re_render_region" ile üretilip gösterilir (Uygula).
 */

import {
  REGION_STYLE_DEFAULTS,
  pageImageUrl,
  type PageResult,
  type Region,
  type RegionStyle,
} from "./viewer";

/** Düzenleyiciye gönderilen taslak; Uygula'da backend'e birebir döner. */
export interface EditorDraft {
  id: number;
  bbox: number[];
  translation: string;
  style: RegionStyle;
  disabled: boolean;
  manual: boolean;
  committed: boolean;
  /** Son uygulamadaki bbox; taşındıysa erase_boxes olarak gönderilir. */
  prevBbox: number[] | null;
}

export interface EditorApi {
  onSelect(id: number | null): void;
  onCreateRegion(bbox: number[]): void;
  onApply(draft: EditorDraft): Promise<void>;
  onDisable(region: Region): Promise<void>;
  onDelete(region: Region): Promise<void>;
}

const MIN_DRAG_PX = 8;
const UPDATED_HINT = "Kaydedilmemiş değişiklikler var — Uygula ile göster.";

/** Önceki render'dan kalan klavye dinleyicisini temizlemek için tutulur
 * (renderEditor her çağrıldığında yeniden kurulur, sızıntı olmaz). */
let activeKeyHandler: ((ev: KeyboardEvent) => void) | null = null;

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

function luma(hex: string): number {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function regionStyle(region: Region): RegionStyle {
  return { ...REGION_STYLE_DEFAULTS, ...(region.style ?? {}) };
}

function estimateFontSize(bbox: number[]): number {
  const h = bbox[3] - bbox[1];
  if (h <= 0) return 16;
  return clamp(Math.round(h * 0.3), 9, 36);
}

const PALETTE = [
  "#ffffff",
  "#000000",
  "#ff3b30",
  "#2f6fe4",
  "#ffc400",
  "#22c55e",
  "#e05a8a",
  "#8b5cf6",
  "#f97316",
];

export function renderEditor(
  container: HTMLElement,
  page: PageResult,
  selectedId: number | null,
  ver: number,
  api: EditorApi,
): void {
  if (activeKeyHandler) {
    window.removeEventListener("keydown", activeKeyHandler);
    activeKeyHandler = null;
  }
  container.replaceChildren();
  const wrap = el("div", "editor-wrap");
  const stage = el("div", "editor-stage");

  const translated = page.outputs.translated;
  const img = el("img");
  img.src = pageImageUrl(translated, ver);
  img.alt = "Çevrilmiş sayfa (düzenleme)";
  img.draggable = false;
  stage.appendChild(img);

  const overlay = el("div", "ov-layer editor-overlay");
  stage.appendChild(overlay);

  let imgW = 1;
  let imgH = 1;

  const selected = page.regions.find((r) => r.id === selectedId) ?? null;
  const draft: EditorDraft | null = selected
    ? {
        id: selected.id,
        bbox: [...selected.bbox],
        translation: selected.translation || "",
        style: regionStyle(selected),
        disabled: !!selected.disabled,
        manual: !!selected.manual,
        committed: !!selected.committed,
        prevBbox: [...selected.bbox],
      }
    : null;

  let dirty = false;
  let applying = false;
  let moveOffset: { dx: number; dy: number } | null = null;
  let drawStart: { x: number; y: number } | null = null;

  const stageBox = (): DOMRect => stage.getBoundingClientRect();
  const toImg = (cx: number, cy: number): [number, number] => {
    const b = stageBox();
    const sx = b.width / Math.max(1, imgW);
    const sy = b.height / Math.max(1, imgH);
    return [clamp((cx - b.left) / sx, 0, imgW), clamp((cy - b.top) / sy, 0, imgH)];
  };
  const pct = (v: number, total: number): string => `${(v / Math.max(1, total)) * 100}%`;
  const scaleX = (): number => stageBox().width / Math.max(1, imgW);

  /* ------------------------------------------------------- kuş gözü kutular */

  function renderOverlay(): void {
    overlay.replaceChildren();
    for (const r of page.regions) {
      if (r.bbox.length < 4) continue;
      const [x, y, w, h] = r.bbox;
      const box = el("div", "reg-box");
      if (r.disabled) box.classList.add("disabled");
      if (r.manual) box.classList.add("manual");
      if (r.overflow && !r.disabled) box.classList.add("overflow");
      if (selected && r.id === selected.id) {
        box.classList.add("selected");
        box.classList.toggle("dirty", dirty);
      }
      box.style.left = pct(x, imgW);
      box.style.top = pct(y, imgH);
      box.style.width = pct(w, imgW);
      box.style.height = pct(h, imgH);
      box.dataset.rid = String(r.id);
      box.title = `${r.label_name || "Bölge"}${r.manual ? " (elle)" : ""}${r.disabled ? " — kapalı" : ""}`;

      // Seçili bölgede canlı önizleme
      if (selected && r.id === selected.id && draft) {
        if (draft.translation.trim()) {
          const prev = el("div", "reg-preview");
          prev.textContent = draft.translation;
          const size =
            draft.style.font_size_override ??
            selected.font_size ??
            estimateFontSize(selected.bbox);
          prev.style.fontSize = `${Math.round(size * scaleX())}px`;
          prev.style.fontWeight = draft.style.font_weight === "bold" ? "700" : "400";
          prev.style.textAlign = draft.style.align;
          const color = draft.style.color;
          if (color) {
            prev.style.color = color;
            prev.style.textShadow = `0 0 ${Math.max(2, Math.round(size * 0.1))}px ${
              luma(color) >= 140 ? "#000" : "#fff"
            }`;
          } else {
            prev.style.color = "#000";
            prev.style.textShadow = `0 0 2px #fff, 0 0 2px #fff`;
          }
          box.appendChild(prev);
        }
      }
      overlay.appendChild(box);
    }

    // Sürüklenen çizim dikdörtgeni
    const ghost = overlay.querySelector<HTMLElement>(".draw-ghost");
    if (ghost) overlay.removeChild(ghost);
    if (drawStart) {
      const g = el("div", "draw-ghost");
      g.style.left = pct(drawStart.x, imgW);
      g.style.top = pct(drawStart.y, imgH);
      overlay.appendChild(g);
    }
  }

  /* ------------------------------------------------------- sürükle-çiz / taşı */

  stage.addEventListener("pointerdown", (ev) => {
    if (applying) return;
    const target = (ev.target as HTMLElement).closest(".reg-box");
    if (target) {
      const rid = Number((target as HTMLElement).dataset.rid ?? -1);
      if (rid !== selectedId) {
        api.onSelect(Number.isFinite(rid) ? rid : null);
        return;
      }
      // Seçili kutuya basınca taşıma başlar
      const b = stageBox();
      moveOffset = {
        dx: ev.clientX - b.left - ((draft?.bbox[0] ?? 0) / Math.max(1, imgW)) * b.width,
        dy: ev.clientY - b.top - ((draft?.bbox[1] ?? 0) / Math.max(1, imgH)) * b.height,
      };
      stage.setPointerCapture(ev.pointerId);
      return;
    }
    // Boş alan: çizim başlar
    const [ix, iy] = toImg(ev.clientX, ev.clientY);
    drawStart = { x: ix, y: iy };
    renderOverlay();
    stage.setPointerCapture(ev.pointerId);
  });

  stage.addEventListener("pointermove", (ev) => {
    if (moveOffset && draft) {
      const b = stageBox();
      const x = (ev.clientX - b.left - moveOffset.dx) / b.width * imgW;
      const y = (ev.clientY - b.top - moveOffset.dy) / b.height * imgH;
      const w = draft.bbox[2] - draft.bbox[0];
      const h = draft.bbox[3] - draft.bbox[1];
      draft.bbox = [
        Math.round(clamp(x, 0, imgW - w)),
        Math.round(clamp(y, 0, imgH - h)),
        Math.round(clamp(x + w, 0, imgW)),
        Math.round(clamp(y + h, 0, imgH)),
      ];
      dirty = true;
      renderOverlay();
      renderPanel();
      return;
    }
    if (drawStart) {
      const [x, y] = toImg(ev.clientX, ev.clientY);
      const g = overlay.querySelector<HTMLElement>(".draw-ghost");
      if (g) {
        const x1 = Math.min(drawStart.x, x);
        const y1 = Math.min(drawStart.y, y);
        g.style.left = pct(x1, imgW);
        g.style.top = pct(y1, imgH);
        g.style.width = pct(Math.abs(x - drawStart.x), imgW);
        g.style.height = pct(Math.abs(y - drawStart.y), imgH);
      }
    }
  });

  const finishPointer = (ev: PointerEvent): void => {
    if (drawStart) {
      const [x, y] = toImg(ev.clientX, ev.clientY);
      const x1 = Math.round(Math.min(drawStart.x, x));
      const y1 = Math.round(Math.min(drawStart.y, y));
      const w = Math.round(Math.abs(x - drawStart.x));
      const h = Math.round(Math.abs(y - drawStart.y));
      drawStart = null;
      moveOffset = null;
      if (w >= MIN_DRAG_PX && h >= MIN_DRAG_PX) {
        api.onCreateRegion([x1, y1, x1 + w, y1 + h]);
        return;
      }
      if (w < MIN_DRAG_PX && h < MIN_DRAG_PX) {
        api.onSelect(null); // boş alana küçük tıklama = seçimi bırak
      }
      renderOverlay();
      return;
    }
    moveOffset = null;
  };

  stage.addEventListener("pointerup", finishPointer);
  stage.addEventListener("pointercancel", () => {
    drawStart = null;
    moveOffset = null;
    renderOverlay();
  });

  /* ------------------------------------------------ panel + stil kontrolleri */

  function markDirty(): void {
    dirty = true;
    renderOverlay();
    renderPanel();
  }

  function buildControlPanel(): HTMLElement {
    const panel = el("div", "editor-panel card");
    if (!selected || !draft) {
      panel.appendChild(el("p", "hint", "Bir bölge seçin ya da boş alanda sürükleyerek yeni bölge çizin."));
      return panel;
    }

    const head = el("div", "editor-panel-head");
    head.appendChild(
      el(
        "span",
        "muted",
        `Bölge ${page.regions.indexOf(selected) + 1} · ${selected.label_name || "?"}${
          selected.manual ? " · elle" : ""
        }${selected.disabled ? " · kapalı" : ""}`,
      ),
    );
    if (dirty) head.appendChild(el("span", "dirty-hint", "Kaydedilmedi"));
    panel.appendChild(head);

    const field = el("div", "field");
    const label = el("label", "field-label", "Çeviri metni");
    label.setAttribute("for", "reg-editor-text");
    field.appendChild(label);
    const ta = el("textarea") as HTMLTextAreaElement;
    ta.id = "reg-editor-text";
    ta.rows = 3;
    ta.spellcheck = false;
    ta.value = draft.translation;
    ta.disabled = draft.disabled || applying;
    ta.placeholder = draft.disabled ? "Bu bölge kapalı — Etkinleştir ve Uygula ile geri açın" : "Çeviri…";
    ta.addEventListener("input", () => {
      draft.translation = ta.value;
      markDirty();
    });
    field.appendChild(ta);
    panel.appendChild(field);

    // Ağırlık + hizalama
    const styleRow = el("div", "row");
    const weightGroup = el("div", "segmented small");
    weightGroup.setAttribute("role", "radiogroup");
    for (const [val, text] of [["normal", "Normal"], ["bold", "Kalın"]] as const) {
      const b = el("button", "seg") as HTMLButtonElement;
      b.type = "button";
      b.dataset.w = val;
      b.textContent = text;
      b.classList.toggle("active", draft.style.font_weight === val);
      b.addEventListener("click", () => {
        draft.style.font_weight = val;
        markDirty();
      });
      weightGroup.appendChild(b);
    }
    styleRow.appendChild(weightGroup);

    const alignGroup = el("div", "segmented small");
    alignGroup.setAttribute("role", "radiogroup");
    for (const [val, text] of [["left", "Sol"], ["center", "Orta"], ["right", "Sağ"]] as const) {
      const b = el("button", "seg") as HTMLButtonElement;
      b.type = "button";
      b.dataset.a = val;
      b.textContent = text;
      b.classList.toggle("active", draft.style.align === val);
      b.addEventListener("click", () => {
        draft.style.align = val;
        markDirty();
      });
      alignGroup.appendChild(b);
    }
    styleRow.appendChild(alignGroup);
    panel.appendChild(styleRow);

    // Boyut
    const sizeRow = el("div", "row size-row");
    const autoCb = el("input") as HTMLInputElement;
    autoCb.type = "checkbox";
    autoCb.id = "reg-editor-auto-size";
    autoCb.checked = draft.style.font_size_override === null;
    const autoLbl = el("label", "check-label", "Otomatik boyut");
    autoLbl.htmlFor = "reg-editor-auto-size";
    autoLbl.appendChild(autoCb);
    sizeRow.appendChild(autoLbl);
    const sizeInput = el("input") as HTMLInputElement;
    sizeInput.type = "number";
    sizeInput.id = "reg-editor-size";
    sizeInput.min = "4";
    sizeInput.max = "200";
    sizeInput.value = String(draft.style.font_size_override ?? 16);
    sizeInput.disabled = autoCb.checked || draft.disabled;
    sizeInput.addEventListener("input", () => {
      const v = parseInt(sizeInput.value, 10);
      if (Number.isFinite(v) && v >= 1) {
        draft.style.font_size_override = v;
        markDirty();
      }
    });
    autoCb.addEventListener("change", () => {
      if (autoCb.checked) {
        draft.style.font_size_override = null;
        sizeInput.disabled = true;
      } else {
        draft.style.font_size_override = parseInt(sizeInput.value, 10) || 16;
        sizeInput.disabled = false;
      }
      markDirty();
    });
    sizeRow.appendChild(sizeInput);
    sizeRow.appendChild(el("span", "muted", "px"));
    panel.appendChild(sizeRow);

    // Renk
    const colorField = el("div", "field");
    colorField.appendChild(el("span", "field-label", "Metin rengi"));
    const swatches = el("div", "swatches");
    const autoSw = el("button", "swatch auto") as HTMLButtonElement;
    autoSw.type = "button";
    autoSw.title = "Otomatik (siyah dolgu + beyaz kontur)";
    autoSw.textContent = "Oto";
    autoSw.classList.toggle("active", draft.style.color === null);
    autoSw.addEventListener("click", () => {
      draft.style.color = null;
      markDirty();
    });
    swatches.appendChild(autoSw);
    for (const c of PALETTE) {
      const sw = el("button", "swatch") as HTMLButtonElement;
      sw.type = "button";
      sw.style.background = c;
      sw.dataset.color = c;
      sw.title = c;
      sw.classList.toggle("active", draft.style.color === c);
      sw.addEventListener("click", () => {
        draft.style.color = c;
        markDirty();
      });
      swatches.appendChild(sw);
    }
    const colorInput = el("input") as HTMLInputElement;
    colorInput.type = "color";
    colorInput.value = draft.style.color ?? "#e05a8a";
    colorInput.classList.add("color-input");
    colorInput.title = "Serbest renk seçimi";
    colorInput.addEventListener("input", () => {
      draft.style.color = colorInput.value;
      markDirty();
    });
    swatches.appendChild(colorInput);
    colorField.appendChild(swatches);
    panel.appendChild(colorField);

    // Eylemler
    const actions = el("div", "row editor-actions");
    const btnApply = el("button", "btn primary") as HTMLButtonElement;
    btnApply.type = "button";
    btnApply.textContent = draft.disabled ? "Kapalı bırak" : "Uygula";
    btnApply.disabled = applying || (!dirty && !draft.disabled);
    btnApply.addEventListener("click", () => void handleApply());
    actions.appendChild(btnApply);

    const btnReset = el("button", "btn secondary") as HTMLButtonElement;
    btnReset.type = "button";
    btnReset.textContent = "Varsayılan stil";
    btnReset.addEventListener("click", () => {
      draft.style = { ...REGION_STYLE_DEFAULTS };
      markDirty();
    });
    actions.appendChild(btnReset);

    if (draft.disabled) {
      const btnEnable = el("button", "btn secondary") as HTMLButtonElement;
      btnEnable.type = "button";
      btnEnable.textContent = "Etkinleştir";
      btnEnable.addEventListener("click", () => {
        draft.disabled = false;
        draft.translation = selected.translation || "";
        markDirty();
      });
      actions.appendChild(btnEnable);
    } else {
      const btnDisable = el("button", "btn ghost") as HTMLButtonElement;
      btnDisable.type = "button";
      btnDisable.textContent = "Devre Dışı Bırak";
      btnDisable.addEventListener("click", () => {
        draft.disabled = true;
        markDirty();
      });
      actions.appendChild(btnDisable);
    }

    const btnDelete = el("button", "btn ghost danger") as HTMLButtonElement;
    btnDelete.type = "button";
    btnDelete.textContent = "Sil";
    btnDelete.addEventListener("click", () => void handleDelete());
    actions.appendChild(btnDelete);
    panel.appendChild(actions);

    if (dirty) panel.appendChild(el("p", "hint", UPDATED_HINT));
    if (selected.overflow && !selected.disabled) {
      panel.appendChild(
        el("p", "hint warn-text", "Bu bölgede çeviri balon sınırını aşıyor — boyutu küçültün ya da metni kısaltın."),
      );
    }
    return panel;
  }

  function renderPanel(): void {
    panel.replaceChildren(buildControlPanel());
  }

  async function handleApply(): Promise<void> {
    if (!draft || applying) return;
    applying = true;
    renderPanel();
    try {
      await api.onApply({
        ...draft,
        translation: draft.disabled ? "" : draft.translation,
        bbox: [...draft.bbox],
        style: { ...draft.style },
        prevBbox: dirty ? (draft.prevBbox ? [...draft.prevBbox] : null) : null,
      });
    } catch (err) {
      console.error("Uygula hatası:", err);
    } finally {
      applying = false;
    }
  }

  async function handleDelete(): Promise<void> {
    if (!selected) return;
    await api.onDelete(selected);
  }

  /* ------------------------------------------------------------ montaj */

  const panel = el("div", "editor-panel-host");
  panel.appendChild(buildControlPanel());
  wrap.append(stage, panel);
  container.appendChild(wrap);
  renderOverlay();

  void img
    .decode()
    .then(() => {
      imgW = img.naturalWidth || 1;
      imgH = img.naturalHeight || 1;
      const maxW = Math.max(200, container.clientWidth);
      const maxH = Math.max(320, window.innerHeight - 420);
      let w = maxW;
      let h = (w * imgH) / imgW;
      if (h > maxH) {
        h = maxH;
        w = (h * imgW) / imgH;
      }
      stage.style.width = `${Math.round(w)}px`;
      stage.style.height = `${Math.round(h)}px`;
      renderOverlay();
    })
    .catch(() => undefined);

  function onKey(ev: KeyboardEvent): void {
    if (ev.key === "Escape" && selected && draft) {
      ev.stopPropagation();
      draft.bbox = [...selected.bbox];
      draft.translation = selected.translation || "";
      draft.style = regionStyle(selected);
      draft.disabled = !!selected.disabled;
      draft.prevBbox = [...selected.bbox];
      dirty = false;
      renderOverlay();
      renderPanel();
      return;
    }
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      void handleApply();
    }
  }

  activeKeyHandler = onKey;
  window.addEventListener("keydown", activeKeyHandler);
}