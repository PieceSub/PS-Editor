import { convertFileSrc, invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
import "./styles.css";
import { stageLabel, type ProgressPayload } from "./labels";
import { renderViewer, type PageResult, type ViewMode } from "./viewer";

/* ------------------------------------------------------------------ state */

type Mode = "auto" | "local" | "api";

interface ProviderInfo {
  name: string;
  needs_key: boolean;
  has_key: boolean;
}

interface DonePage {
  input: string;
  result: PageResult;
}

const state = {
  pages: [] as string[],
  sourceLabel: "",
  mode: "auto" as Mode,
  provider: "mock",
  providers: [] as ProviderInfo[],
  running: false,
  cancelRequested: false,
  done: [] as DonePage[],
  failedCount: 0,
  selected: 0,
  viewMode: "compare" as ViewMode,
  showOverflow: true,
  currentJob: "",
};

const PROVIDER_NAMES: Record<string, string> = {
  mock: "Mock (test)",
  local: "Yerel (Ollama)",
  openai: "OpenAI",
  openai_compat: "OpenAI Uyumlu (Groq / Together / DeepSeek)",
  anthropic: "Anthropic (Claude)",
};

const MODE_HINTS: Record<Mode, string> = {
  auto: "VRAM yeterliyse yerel model, değilse API kullanılır.",
  local: "Yerel Ollama sunucusu kullanılır (http://localhost:11434).",
  api: "Seçili sağlayıcının API'si kullanılır.",
};

/* ------------------------------------------------------------------- dom */

const $ = <T extends HTMLElement>(id: string): T => {
  const node = document.getElementById(id);
  if (!node) throw new Error(`DOM öğesi bulunamadı: #${id}`);
  return node as T;
};

const els = {
  sidecarStatus: $<HTMLDivElement>("sidecar-status"),
  banner: $<HTMLDivElement>("banner"),
  bannerText: $<HTMLSpanElement>("banner-text"),
  bannerClose: $<HTMLButtonElement>("banner-close"),
  btnPickFile: $<HTMLButtonElement>("btn-pick-file"),
  btnPickFolder: $<HTMLButtonElement>("btn-pick-folder"),
  sourceInfo: $<HTMLDivElement>("source-info"),
  modeGroup: $<HTMLDivElement>("mode-group"),
  modeHint: $<HTMLParagraphElement>("mode-hint"),
  providerField: $<HTMLDivElement>("provider-field"),
  providerSelect: $<HTMLSelectElement>("provider-select"),
  providerHint: $<HTMLParagraphElement>("provider-hint"),
  langSelect: $<HTMLSelectElement>("lang-select"),
  btnStart: $<HTMLButtonElement>("btn-start"),
  btnCancel: $<HTMLButtonElement>("btn-cancel"),
  progressCard: $<HTMLElement>("progress-card"),
  progressCount: $<HTMLSpanElement>("progress-count"),
  overallFill: $<HTMLDivElement>("overall-fill"),
  overallHint: $<HTMLParagraphElement>("overall-hint"),
  pageFill: $<HTMLDivElement>("page-fill"),
  pagePct: $<HTMLSpanElement>("page-pct"),
  stageLabel: $<HTMLParagraphElement>("stage-label"),
  stageDetail: $<HTMLParagraphElement>("stage-detail"),
  resultsCard: $<HTMLElement>("results-card"),
  resultSummary: $<HTMLParagraphElement>("result-summary"),
  pageMeta: $<HTMLParagraphElement>("page-meta"),
  viewer: $<HTMLDivElement>("viewer"),
  overflowWarning: $<HTMLDivElement>("overflow-warning"),
  btnOverflow: $<HTMLButtonElement>("btn-overflow"),
  btnExport: $<HTMLButtonElement>("btn-export"),
  viewModeGroup: $<HTMLDivElement>("view-mode-group"),
  thumbs: $<HTMLDivElement>("thumbs"),
};

/* ------------------------------------------------------------- yardımcılar */

function basename(path: string): string {
  return path.split(/[\\/]/).pop() ?? path;
}

function stripExt(name: string): string {
  return name.replace(/\.[^.]+$/, "");
}

async function request(cmd: string, payload?: unknown): Promise<unknown> {
  return invoke("python_request", { cmd, payload });
}

function setBadge(kind: "ok" | "error" | "unknown", text: string): void {
  els.sidecarStatus.className = `badge ${kind}`;
  els.sidecarStatus.textContent = text;
}

let bannerTimer: number | undefined;
function showBanner(text: string, kind: "error" | "ok" | "warn"): void {
  window.clearTimeout(bannerTimer);
  els.banner.className = `banner ${kind}`;
  els.bannerText.textContent = text;
  if (kind !== "error") {
    bannerTimer = window.setTimeout(() => {
      els.banner.classList.add("hidden");
    }, 6000);
  }
}

function hideBanner(): void {
  window.clearTimeout(bannerTimer);
  els.banner.classList.add("hidden");
}

/* -------------------------------------------------------- kaynak seçimi */

async function pickFile(): Promise<void> {
  const file = await open({
    multiple: false,
    title: "Manga sayfası seçin",
    filters: [{ name: "Görseller", extensions: ["png", "jpg", "jpeg", "webp", "bmp", "gif"] }],
  });
  if (!file) return;
  const path = Array.isArray(file) ? file[0] : file;
  state.pages = [path];
  state.sourceLabel = `Tek sayfa · ${basename(path)}`;
  showSourceInfo();
}

async function pickFolder(): Promise<void> {
  const dir = await open({ directory: true, multiple: false, title: "Sayfaları içeren klasörü seçin" });
  if (!dir) return;
  const folder = Array.isArray(dir) ? dir[0] : dir;
  const images = (await invoke("list_images", { dir: folder })) as string[];
  if (!images.length) {
    showBanner("Seçilen klasörde görsel bulunamadı (PNG/JPG/WebP/BMP/GIF).", "warn");
  }
  state.pages = images;
  state.sourceLabel = `${images.length} sayfa · ${folder}`;
  showSourceInfo();
}

function showSourceInfo(): void {
  els.sourceInfo.textContent = state.sourceLabel;
  els.sourceInfo.classList.remove("hidden");
  els.btnStart.disabled = !state.pages.length || state.running;
}

/* ---------------------------------------------------------------- modlar */

function setMode(mode: Mode): void {
  state.mode = mode;
  for (const btn of els.modeGroup.querySelectorAll<HTMLButtonElement>("button.seg")) {
    const active = btn.dataset.mode === mode;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-checked", String(active));
  }
  els.modeHint.textContent = MODE_HINTS[mode];
  els.providerSelect.disabled = state.running || mode === "local";
  els.providerField.classList.toggle("dim", mode === "local");
  syncProviderDefault();
}

function syncProviderDefault(): void {
  if (state.mode === "local" && state.providers.length) {
    els.providerSelect.value = "local";
    state.provider = "local";
  } else if (state.provider === "local") {
    els.providerSelect.value = "mock";
    state.provider = "mock";
  }
  updateProviderHint();
}

async function loadProviders(): Promise<void> {
  try {
    const res = (await request("list_providers", {})) as { providers: ProviderInfo[] };
    state.providers = res.providers;
  } catch {
    state.providers = [
      { name: "mock", needs_key: false, has_key: false },
      { name: "local", needs_key: false, has_key: false },
      { name: "openai", needs_key: true, has_key: false },
      { name: "openai_compat", needs_key: true, has_key: false },
      { name: "anthropic", needs_key: true, has_key: false },
    ];
  }
  renderProviderSelect();
}

function renderProviderSelect(): void {
  const sel = els.providerSelect;
  sel.replaceChildren();
  for (const p of state.providers) {
    const opt = document.createElement("option");
    opt.value = p.name;
    opt.textContent = PROVIDER_NAMES[p.name] ?? p.name;
    if (p.needs_key && !p.has_key) opt.textContent += " · anahtar yok";
    sel.appendChild(opt);
  }
  sel.value = state.providers.some((p) => p.name === state.provider) ? state.provider : "mock";
  state.provider = sel.value;
  syncProviderDefault();
}

function updateProviderHint(): void {
  const sel = els.providerSelect;
  const info = state.providers.find((p) => p.name === sel.value);
  if (!info) {
    els.providerHint.textContent = "";
    return;
  }
  if (state.mode === "local") {
    els.providerHint.textContent = "Yerel modda sağlayıcı seçimi geçersiz; Ollama kullanılır.";
  } else if (info.needs_key && !info.has_key) {
    els.providerHint.textContent =
      "Sistemde kayıtlı API anahtarı yok; pipeline test mock çevirisine düşer.";
  } else if (info.needs_key) {
    els.providerHint.textContent = "API anahtarı sistemde kayıtlı (güvenli depo).";
  } else {
    els.providerHint.textContent = "";
  }
}

/* ----------------------------------------------------------- ilerleme UI */

function setPageProgress(progress: number, label: string, detail: string): void {
  const pct = Math.round(progress * 100);
  els.pageFill.style.width = `${pct}%`;
  els.pagePct.textContent = `%${pct}`;
  els.stageLabel.textContent = label;
  els.stageDetail.textContent = detail;
}

function onProgress(p: ProgressPayload): void {
  if (!state.running) return;
  if (p.job_id && p.job_id !== state.currentJob) return;
  const progress = p.progress ?? 0;
  setPageProgress(progress, stageLabel(p.name), p.message ?? "");

  const total = state.pages.length || 1;
  const overall = (state.done.length + Math.min(1, progress)) / total;
  els.overallFill.style.width = `${Math.min(100, overall * 100)}%`;
  els.progressCount.textContent = `${state.done.length}${state.cancelRequested ? "" : " / " + total} sayfa`;
}

/* ------------------------------------------------------------ ana akış */

function setRunning(running: boolean): void {
  state.running = running;
  els.btnStart.disabled = running || !state.pages.length;
  els.btnPickFile.disabled = running;
  els.btnPickFolder.disabled = running;
  els.langSelect.disabled = running;
  for (const btn of els.modeGroup.querySelectorAll<HTMLButtonElement>("button.seg")) {
    btn.disabled = running;
  }
  els.btnCancel.classList.toggle("hidden", !running);
  els.btnCancel.disabled = !running;
}

async function run(): Promise<void> {
  const pages = state.pages;
  if (!pages.length || state.running) return;

  state.done = [];
  state.failedCount = 0;
  state.selected = 0;
  state.cancelRequested = false;
  hideBanner();
  setRunning(true);
  els.resultsCard.classList.add("hidden");
  els.progressCard.classList.remove("hidden");
  els.overallFill.style.width = "0%";
  setPageProgress(0, stageLabel("started"), "Hazırlanıyor…");

  const lang = els.langSelect.value;
  const provider = state.mode === "local" ? "local" : state.provider;

  for (let i = 0; i < pages.length; i++) {
    if (state.cancelRequested) break;
    state.currentJob = `batch-${Date.now().toString(36)}-${i}`;
    const pageName = basename(pages[i]);
    els.overallHint.textContent = `Sayfa ${i + 1}/${pages.length}: ${pageName}`;
    els.progressCount.textContent = `${i}/${pages.length} sayfa`;
    setPageProgress(0, stageLabel("started"), pageName);

    try {
      const result = (await request("translate_page", {
        image: pages[i],
        target_lang: lang,
        mode: state.mode,
        provider,
        job_id: state.currentJob,
      })) as PageResult;
      state.done.push({ input: pages[i], result });
      if (state.running && !state.cancelRequested) {
        els.overallFill.style.width = `${(state.done.length / pages.length) * 100}%`;
        els.progressCount.textContent = `${state.done.length}/${pages.length} sayfa`;
      }
    } catch (err) {
      state.failedCount++;
      showBanner(`"${pageName}" işlenemedi: ${String(err)}`, "error");
    }
  }

  setRunning(false);
  els.progressCard.classList.add("hidden");

  if (state.done.length) {
    renderResults();
  }
  if (state.cancelRequested) {
    showBanner(
      `İptal edildi — ${state.done.length}/${pages.length} sayfa tamamlandı.`,
      "warn",
    );
  }
}

/* -------------------------------------------------------------- sonuçlar */

function renderResults(): void {
  els.resultsCard.classList.remove("hidden");
  const total = state.done.length + state.failedCount;
  els.resultSummary.textContent = `${state.done.length}/${total} sayfa başarıyla işlendi${
    state.failedCount ? ` · ${state.failedCount} hata` : ""
  }`;

  const overflowCount = state.done.reduce(
    (acc, d) => acc + d.result.regions.filter((r) => r.overflow).length,
    0,
  );
  els.overflowWarning.classList.toggle("hidden", overflowCount === 0);
  els.overflowWarning.textContent =
    overflowCount > 0
      ? `Dikkat: ${overflowCount} bölgede metin taşması tespit edildi — çevrilen metin bölge sınırını aşıyor (kızıl çerçeveler).`
      : "";

  renderThumbs();
  renderSelected();
}

function renderThumbs(): void {
  els.thumbs.replaceChildren();
  els.thumbs.classList.remove("hidden");
  state.done.forEach((item, idx) => {
    const thumb = document.createElement("button");
    thumb.type = "button";
    thumb.className = "thumb" + (idx === state.selected ? " active" : "");
    thumb.title = `${basename(item.input)} · ${item.result.provider?.name ?? ""}`;
    const img = document.createElement("img");
    img.src = convertFileSrc(item.result.outputs.translated);
    img.alt = `Sayfa ${idx + 1}`;
    thumb.appendChild(img);
    thumb.addEventListener("click", () => {
      state.selected = idx;
      renderSelected();
    });
    els.thumbs.appendChild(thumb);
  });
}

function renderSelected(): void {
  const item = state.done[state.selected];
  if (!item) return;
  const r = item.result;
  renderViewer(els.viewer, r, state.viewMode, state.showOverflow);

  const meta: string[] = [
    basename(item.input),
    r.provider?.name ? `Arka uç: ${r.provider.name}${r.provider.model ? ` (${r.provider.model})` : ""}` : "",
    r.mode_decision?.decision ? `Mod kararı: ${r.mode_decision.decision}` : "",
    r.mode_decision?.chosen_backend ? `Kullanılan sağlayıcı: ${r.mode_decision.chosen_backend}` : "",
    r.timings_ms?.total ? `Süre: ${(r.timings_ms.total / 1000).toFixed(1)}s` : "",
  ].filter(Boolean);
  els.pageMeta.textContent = meta.join(" · ");
  els.pageMeta.classList.remove("hidden");

  Array.from(els.thumbs.children).forEach((btn, idx) => {
    btn.classList.toggle("active", idx === state.selected);
  });
}

/* ----------------------------------------------------------- dışa aktarma */

async function exportResults(): Promise<void> {
  if (!state.done.length) return;
  const dir = await open({ directory: true, multiple: false, title: "Sonuçların kaydedileceği klasörü seçin" });
  if (!dir) return;
  const folder = Array.isArray(dir) ? dir[0] : dir;

  let copied = 0;
  const errors: string[] = [];
  for (const item of state.done) {
    const r = item.result;
    const base = stripExt(basename(item.input));
    try {
      if (r.outputs.translated) {
        await invoke("copy_file", { src: r.outputs.translated, dstDir: folder });
      }
      await invoke("write_text_file", {
        path: `${folder}\\${base}_result.json`,
        contents: JSON.stringify({ ...r, source_image: item.input }, null, 2),
      });
      copied++;
    } catch (err) {
      errors.push(String(err));
    }
  }
  showBanner(
    errors.length
      ? `${copied} sayfa dışa aktarıldı; ${errors.length} hata (${errors[0]})`
      : `${copied} sayfa dışa aktarıldı → ${folder}`,
    errors.length ? "error" : "ok",
  );
}

/* -------------------------------------------------------------- olaylar */

async function initEvents(): Promise<void> {
  await listen("python-event", (ev) => {
    const msg = ev.payload as Record<string, unknown> | undefined;
    if (!msg || typeof msg !== "object") return;
    const name =
      typeof msg.event === "string" ? msg.event : typeof msg.name === "string" ? msg.name : "";
    const payload = msg.payload as ProgressPayload | undefined;

    if (name === "translate_page_progress" && payload) {
      onProgress(payload);
    } else if (name === "ready") {
      setBadge("ok", "Python servisi hazır");
    } else if (name === "exit") {
      setBadge("error", "Python servisi kapandı");
    } else if (name === "error") {
      setBadge("error", "Python servisi hatası");
    }
  });

  els.bannerClose.addEventListener("click", hideBanner);
  els.btnPickFile.addEventListener("click", () => void pickFile());
  els.btnPickFolder.addEventListener("click", () => void pickFolder());

  for (const btn of els.modeGroup.querySelectorAll<HTMLButtonElement>("button.seg")) {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.mode as Mode | undefined;
      if (mode) setMode(mode);
    });
  }

  els.providerSelect.addEventListener("change", () => {
    state.provider = els.providerSelect.value;
    updateProviderHint();
  });

  for (const btn of els.viewModeGroup.querySelectorAll<HTMLButtonElement>("button.seg")) {
    btn.addEventListener("click", () => {
      const view = btn.dataset.view as ViewMode | undefined;
      if (!view) return;
      state.viewMode = view;
      for (const b of els.viewModeGroup.querySelectorAll<HTMLButtonElement>("button.seg")) {
        const active = b.dataset.view === view;
        b.classList.toggle("active", active);
        b.setAttribute("aria-checked", String(active));
      }
      renderSelected();
    });
  }

  els.btnOverflow.addEventListener("click", () => {
    state.showOverflow = !state.showOverflow;
    els.btnOverflow.classList.toggle("active", state.showOverflow);
    renderSelected();
  });

  els.btnStart.addEventListener("click", () => void run());
  els.btnCancel.addEventListener("click", () => {
    state.cancelRequested = true;
    els.btnCancel.disabled = true;
  });
  els.btnExport.addEventListener("click", () => void exportResults());
}

/* ----------------------------------------------------------------- kur */

async function main(): Promise<void> {
  setMode("auto");
  await initEvents();
  await loadProviders();
}

void main();