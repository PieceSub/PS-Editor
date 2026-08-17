import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { open } from "@tauri-apps/plugin-dialog";
import "./styles.css";
import { stageLabel, type ProgressPayload } from "./labels";
import {
  REGION_STYLE_DEFAULTS,
  pageImageUrl,
  renderViewer,
  type PageResult,
  type Region,
  type ViewMode,
} from "./viewer";
import { renderEditor, type EditorApi } from "./editor";

/* ------------------------------------------------------------------ state */

type Mode = "auto" | "local" | "api";

interface ProviderInfo {
  name: string;
  needs_key: boolean;
  has_key: boolean;
}

/** Mangalar sekmesindeki proje kartı özeti (Rust list_projects). */
interface ProjectSummary {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
  source_type: string;
  page_count: number;
  thumb: string | null;
}

interface CreatedProject {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
}

/** project_add_page sonucu: görseller proje klasörüne kopyalanmış, mutlak yollar. */
interface AddedPage {
  index: number;
  name: string;
  source: string;
  result: PageResult;
}

/** project.json manifest meta kısmı (ön yüzün autosave'de gönderdiği). */
interface ManifestMeta {
  id: string;
  name: string;
  created_at: string;
  source_type: string;
  provider_settings: { mode: Mode; provider: string; target_lang: string };
}

interface PersistedPage {
  index: number;
  name: string;
  source: string;
  result: PageResult;
}

interface ProjectManifest extends ManifestMeta {
  schema_version?: number;
  updated_at: string;
  pages: PersistedPage[];
}

interface DonePage {
  /** Görünen sayfa adı (kaynak dosya adı; dışa aktarma adı için de kullanılır). */
  name: string;
  input: string;
  result: PageResult;
  /** Düzenleme sonrası görsel yenileme sürümü (cache-bust). */
  imgVer: number;
}

type TabId = "mangas" | "anime" | "editor";

const state = {
  tab: "mangas" as TabId,
  pages: [] as string[],
  sourcePath: null as string | null,
  sourceType: "file" as "file" | "folder",
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
  editMode: false,
  selectedRegionId: null as number | null,
  projects: [] as ProjectSummary[],
  activeProject: null as { id: string; name: string } | null,
  cardSize: 220,
  manifestMeta: null as ManifestMeta | null,
  savedAt: null as number | null,
  savedFlash: false,
  saveBusy: false,
};

/** Elle eklenen bölgeler için benzersiz id'ler (otomatik id'lerle çakışmaz). */
let manualRegionSeq = 1000;
const nextManualRegionId = (): number => ++manualRegionSeq;

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
  savedIndicator: $<HTMLDivElement>("saved-indicator"),
  banner: $<HTMLDivElement>("banner"),
  bannerText: $<HTMLSpanElement>("banner-text"),
  bannerClose: $<HTMLButtonElement>("banner-close"),
  tabMangas: $<HTMLButtonElement>("tab-mangas"),
  tabAnime: $<HTMLButtonElement>("tab-anime"),
  mangasView: $<HTMLElement>("mangas-view"),
  animeView: $<HTMLElement>("anime-view"),
  editorView: $<HTMLElement>("editor-view"),
  editorEmpty: $<HTMLElement>("editor-empty"),
  btnBackMangas: $<HTMLButtonElement>("btn-back-mangas"),
  projectGrid: $<HTMLDivElement>("project-grid"),
  projectsEmpty: $<HTMLElement>("projects-empty"),
  btnNewProject: $<HTMLButtonElement>("btn-new-project"),
  resultsCard: $<HTMLElement>("results-card"),
  resultsTitle: $<HTMLHeadingElement>("results-title"),
  newProjectModal: $<HTMLDivElement>("new-project-modal"),
  modalBackdrop: $<HTMLDivElement>("modal-backdrop"),
  modalClose: $<HTMLButtonElement>("modal-close"),
  projectName: $<HTMLInputElement>("project-name"),
  confirmModal: $<HTMLDivElement>("confirm-modal"),
  confirmTitle: $<HTMLHeadingElement>("confirm-title"),
  confirmMessage: $<HTMLElement>("confirm-message"),
  confirmOk: $<HTMLButtonElement>("confirm-ok"),
  confirmCancel: $<HTMLButtonElement>("confirm-cancel"),
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
  resultSummary: $<HTMLParagraphElement>("result-summary"),
  pageMeta: $<HTMLParagraphElement>("page-meta"),
  viewer: $<HTMLDivElement>("viewer"),
  overflowWarning: $<HTMLDivElement>("overflow-warning"),
  btnOverflow: $<HTMLButtonElement>("btn-overflow"),
  btnEdit: $<HTMLButtonElement>("btn-edit"),
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

/** ISO zamanını kısa "GG.AA HH:MM" biçimine çevirir; bozuksa "—". */
function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return "—";
  return d.toLocaleString("tr-TR", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
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

/* ------------------------------------------------------------- sekme yönetimi */

function setTab(tab: TabId): void {
  state.tab = tab;
  // "editor" gizli bir durumdur: sekme çubuğunda karşılığı yoktur, yalnızca
  // programatik olarak (proje kartına tıklayarak) tetiklenir. Sekmelerden
  // hiçbiri o zaman aktif görünmez.
  const onMangas = tab === "mangas";
  const onAnime = tab === "anime";
  els.tabMangas.classList.toggle("active", onMangas);
  els.tabAnime.classList.toggle("active", onAnime);
  els.tabMangas.setAttribute("aria-selected", String(onMangas));
  els.tabAnime.setAttribute("aria-selected", String(onAnime));
  els.mangasView.classList.toggle("hidden", !onMangas);
  els.animeView.classList.toggle("hidden", !onAnime);
  els.editorView.classList.toggle("hidden", tab !== "editor");
  if (tab === "editor") {
    // Bellekteki veri zaten güncel; yalnızca boş durumu senkronla.
    const hasPages = state.done.length > 0;
    els.editorEmpty.classList.toggle("hidden", hasPages);
    els.resultsCard.classList.toggle("hidden", !hasPages);
  }
}

/* ------------------------------------------------------- proje listesi (Mangalar) */

/** Kart grid zoom sınırları (px) ve adımı. Adım çarpımsaldır: her tekerlek
 *  tıklığında %10 büyüme/küçülme; ani sıçrama olmaz. */
const CARD_SIZE_MIN = 140;
const CARD_SIZE_MAX = 420;
const CARD_SIZE_STEP = 1.1;
const CARD_SIZE_PREF_KEY = "mangas_card_size";

const clampCardSize = (size: number): number =>
  Math.min(CARD_SIZE_MAX, Math.max(CARD_SIZE_MIN, Math.round(size)));

function setCardSize(size: number): void {
  state.cardSize = clampCardSize(size);
  els.projectGrid.style.setProperty("--card-size", `${state.cardSize}px`);
}

let cardPrefTimer: number | undefined;
/** Zoom değişimini debounce'lu biçimde diske yazar (app_data_dir/prefs.json). */
function scheduleCardPrefSave(): void {
  window.clearTimeout(cardPrefTimer);
  cardPrefTimer = window.setTimeout(() => {
    invoke("save_pref", { key: CARD_SIZE_PREF_KEY, value: state.cardSize }).catch(() => {
      /* tercih yazılamadı; oturum içinde çalışmaya devam */
    });
  }, 300);
}

/** Kayıtlı kart boyutunu yükler (yoksa varsayılan 220px). */
async function loadCardSizePref(): Promise<void> {
  try {
    const v = (await invoke("load_pref", { key: CARD_SIZE_PREF_KEY })) as unknown;
    if (typeof v === "number" && Number.isFinite(v)) {
      setCardSize(v);
    }
  } catch {
    /* tercih okunamadı; varsayılanla devam */
  }
}

function renderSavedIndicator(): void {
  const base = "saved-indicator";
  if (!state.activeProject || !state.savedAt) {
    els.savedIndicator.textContent = "Kaydedilmedi";
    els.savedIndicator.className = `${base} dim`;
    els.savedIndicator.title = "Henüz bir proje açık değil";
    return;
  }
  const time = new Date(state.savedAt).toLocaleTimeString("tr-TR");
  els.savedIndicator.textContent = state.savedFlash ? `Kaydedildi ${time}` : `Son kayıt ${time}`;
  els.savedIndicator.className = `${base} ok`;
  els.savedIndicator.title = `Son kayıt: ${new Date(state.savedAt).toLocaleString("tr-TR")}`;
}

async function refreshProjects(): Promise<void> {
  try {
    state.projects = (await invoke("list_projects")) as ProjectSummary[];
  } catch (err) {
    showBanner(`Proje listesi alınamadı: ${String(err)}`, "error");
    state.projects = [];
  }
  renderProjects();
}

function renderProjects(): void {
  els.projectGrid.replaceChildren();
  els.projectsEmpty.classList.toggle("hidden", state.projects.length > 0);
  for (const p of state.projects) {
    const card = document.createElement("div");
    card.className = "project-card";
    card.tabIndex = 0;
    card.setAttribute("role", "button");
    card.title = `${p.name} — aç`;

    let thumb: HTMLElement;
    if (p.thumb) {
      const img = document.createElement("img");
      img.className = "project-thumb";
      img.src = pageImageUrl(p.thumb);
      img.alt = p.name;
      img.loading = "lazy";
      thumb = img;
    } else {
      thumb = document.createElement("div");
      thumb.className = "project-thumb-placeholder";
      thumb.textContent = "Önizleme yok";
    }

    const body = document.createElement("div");
    body.className = "project-card-body";
    const name = document.createElement("p");
    name.className = "project-card-name";
    name.textContent = p.name;
    name.title = p.name;
    const meta = document.createElement("p");
    meta.className = "project-card-meta";
    meta.textContent = `${p.page_count} sayfa işlendi · son düzenleme ${fmtTime(p.updated_at)}`;
    body.append(name, meta);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "project-delete";
    del.textContent = "×";
    del.title = "Projeyi sil";
    del.setAttribute("aria-label", `${p.name} projesini sil`);
    del.addEventListener("click", (ev) => {
      ev.stopPropagation();
      requestDeleteProject(p);
    });

    card.append(thumb, body, del);
    card.addEventListener("click", () => void openProject(p.id));
    card.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        void openProject(p.id);
      }
    });
    els.projectGrid.appendChild(card);
  }
}

async function openProject(id: string): Promise<void> {
  try {
    const manifest = (await invoke("open_project", { projectId: id })) as ProjectManifest;
    state.activeProject = { id, name: manifest.name };
    state.manifestMeta = {
      id: manifest.id,
      name: manifest.name,
      created_at: manifest.created_at,
      source_type: manifest.source_type,
      provider_settings: manifest.provider_settings,
    };
    state.done = manifest.pages.map((pg) => ({
      name: pg.name,
      input: pg.source,
      result: pg.result,
      imgVer: 1,
    }));
    state.selected = 0;
    state.editMode = false;
    state.selectedRegionId = null;
    const t = Date.parse(manifest.updated_at);
    state.savedAt = Number.isFinite(t) ? t : Date.now();
    state.savedFlash = false;
    renderSavedIndicator();
    els.resultsTitle.textContent = manifest.name;
    els.editorEmpty.classList.toggle("hidden", state.done.length > 0);
    els.resultsCard.classList.toggle("hidden", state.done.length === 0);
    if (state.done.length) renderResults();
    setTab("editor");
  } catch (err) {
    showBanner(`Proje açılamadı: ${String(err)}`, "error");
  }
}

/* ------------------------------------------------------- proje silme (onaylı) */

let confirmCallback: (() => void) | null = null;

function requestDeleteProject(p: ProjectSummary): void {
  confirmDialog(
    "Projeyi sil",
    `"${p.name}" projesi ve içindeki tüm sayfalar kalıcı olarak silinecek. Bu işlem geri alınamaz.`,
    "Kalıcı Olarak Sil",
    () => void deleteProject(p),
  );
}

function confirmDialog(title: string, message: string, okLabel: string, onOk: () => void): void {
  confirmCallback = onOk;
  els.confirmTitle.textContent = title;
  els.confirmMessage.textContent = message;
  els.confirmOk.textContent = okLabel;
  els.confirmModal.classList.remove("hidden");
  els.confirmModal.setAttribute("aria-hidden", "false");
}

function closeConfirm(): void {
  confirmCallback = null;
  els.confirmModal.classList.add("hidden");
  els.confirmModal.setAttribute("aria-hidden", "true");
}

async function deleteProject(p: ProjectSummary): Promise<void> {
  try {
    await invoke("delete_project", { projectId: p.id });
    if (state.activeProject?.id === p.id) {
      state.activeProject = null;
      state.manifestMeta = null;
      state.done = [];
      state.savedAt = null;
      renderSavedIndicator();
      els.editorEmpty.classList.remove("hidden");
      els.resultsCard.classList.add("hidden");
    }
    showBanner(`"${p.name}" silindi.`, "ok");
    await refreshProjects();
  } catch (err) {
    showBanner(`Proje silinemedi: ${String(err)}`, "error");
  }
}

/* ------------------------------------------------- "Yeni çeviri ekle" modalı */

function openNewProjectModal(): void {
  state.pages = [];
  state.sourcePath = null;
  els.sourceInfo.textContent = "";
  els.sourceInfo.classList.add("hidden");
  els.progressCard.classList.add("hidden");
  els.projectName.value = "";
  els.btnStart.disabled = true;
  els.newProjectModal.classList.remove("hidden");
  els.newProjectModal.setAttribute("aria-hidden", "false");
  window.setTimeout(() => els.btnPickFile.focus(), 0);
}

function closeNewProjectModal(): void {
  if (state.running) return; // İşlem sürerken kapatılamaz.
  els.newProjectModal.classList.add("hidden");
  els.newProjectModal.setAttribute("aria-hidden", "true");
}

function defaultProjectName(): string {
  if (!state.sourcePath) return "Yeni Proje";
  return state.sourceType === "folder"
    ? basename(state.sourcePath)
    : stripExt(basename(state.sourcePath));
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
  state.sourcePath = path;
  state.sourceType = "file";
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
  state.sourcePath = folder;
  state.sourceType = "folder";
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
  els.modalClose.disabled = running;
  els.projectName.disabled = running;
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
  els.progressCard.classList.remove("hidden");
  els.resultsCard.classList.add("hidden");
  els.overallFill.style.width = "0%";
  setPageProgress(0, stageLabel("started"), "Hazırlanıyor…");

  const lang = els.langSelect.value;
  const provider = state.mode === "local" ? "local" : state.provider;
  const name = els.projectName.value.trim() || defaultProjectName();

  // 1) Her işlem artık her zaman kalıcı bir proje oluşturur (adım 6/7'deki
  //    pipeline çağrıları değişmez; yalnızca sonuç diske yazılmaya eklenir).
  let projectId: string;
  try {
    const created = (await invoke("create_project", {
      name,
      sourceType: state.sourceType,
      mode: state.mode,
      provider,
      targetLang: lang,
    })) as CreatedProject;
    projectId = created.id;
  } catch (err) {
    setRunning(false);
    els.progressCard.classList.add("hidden");
    showBanner(`Proje oluşturulamadı: ${String(err)}`, "error");
    return;
  }
  state.activeProject = { id: projectId, name };
  state.manifestMeta = {
    id: projectId,
    name,
    created_at: new Date().toISOString(),
    source_type: state.sourceType,
    provider_settings: { mode: state.mode, provider, target_lang: lang },
  };
  state.savedAt = null;
  renderSavedIndicator();

  for (let i = 0; i < pages.length; i++) {
    if (state.cancelRequested) break;
    state.currentJob = `batch-${Date.now().toString(36)}-${i}`;
    const pageName = basename(pages[i]);
    els.overallHint.textContent = `Sayfa ${i + 1}/${pages.length}: ${pageName}`;
    els.progressCount.textContent = `${i}/${pages.length} sayfa`;
    setPageProgress(0, stageLabel("started"), pageName);

    try {
      // 1a) Görseli ÖNCE proje klasörüne kopyala (kaynak klasöre asla
      //     dokunulmaz): pipeline yalnızca proje içi kopya üzerinde çalışır.
      const prepared = (await invoke("project_prepare_page", {
        projectId,
        index: i,
        source: pages[i],
      })) as { source: string; out_dir: string };
      const result = (await request("translate_page", {
        image: prepared.source,
        target_lang: lang,
        mode: state.mode,
        provider,
        job_id: state.currentJob,
        settings: { out_dir: prepared.out_dir },
      })) as PageResult;
      // 2) Sonucu projeye kopyala + manifeste yaz (incremental kayıt).
      const added = (await invoke("project_add_page", {
        projectId,
        page: { name: pageName, result },
      })) as AddedPage;
      state.done.push({ name: added.name, input: added.source, result: added.result, imgVer: 1 });
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
    state.savedAt = Date.now();
    state.savedFlash = false;
    renderSavedIndicator();
    renderResults();
    els.resultsTitle.textContent = name;
    closeNewProjectModal();
    setTab("editor");
    await refreshProjects();
  } else {
    // Hiç sayfa işlenemedi: boş proje klasörü bırakma.
    try {
      await invoke("delete_project", { projectId });
    } catch {
      /* yoksay */
    }
    state.activeProject = null;
    state.manifestMeta = null;
    renderSavedIndicator();
    closeNewProjectModal();
    await refreshProjects();
  }
  if (state.cancelRequested) {
    showBanner(
      `İptal edildi — ${state.done.length}/${pages.length} sayfa tamamlandı.`,
      "warn",
    );
  }
}

/* -------------------------------------------------------------- sonuçlar */

function refreshOverflowWarning(): void {
  const overflowCount = state.done.reduce(
    (acc, d) =>
      acc + d.result.regions.filter((r) => r.overflow && !r.disabled).length,
    0,
  );
  els.overflowWarning.classList.toggle("hidden", overflowCount === 0);
  els.overflowWarning.textContent =
    overflowCount > 0
      ? `Dikkat: ${overflowCount} bölgede metin taşması tespit edildi — çevrilen metin bölge sınırını aşıyor (kızıl çerçeveler).`
      : "";
}

function renderResults(): void {
  els.resultsCard.classList.remove("hidden");
  const total = state.done.length + state.failedCount;
  els.resultSummary.textContent = `${state.done.length}/${total} sayfa başarıyla işlendi${
    state.failedCount ? ` · ${state.failedCount} hata` : ""
  }`;

  refreshOverflowWarning();

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
    thumb.title = `${item.name} · ${item.result.provider?.name ?? ""}`;
    const img = document.createElement("img");
    img.src = pageImageUrl(item.result.outputs.translated, item.imgVer);
    img.alt = `Sayfa ${idx + 1}`;
    thumb.appendChild(img);
    thumb.addEventListener("click", () => {
      state.selected = idx;
      renderSelected();
    });
    els.thumbs.appendChild(thumb);
  });
}

function selectedItem(): DonePage | null {
  return state.done[state.selected] ?? null;
}

function renderSelected(): void {
  const item = selectedItem();
  if (!item) return;
  const r = item.result;

  if (state.editMode) {
    renderEditor(els.viewer, r, state.selectedRegionId, item.imgVer, editorApi());
  } else {
    renderViewer(els.viewer, r, {
      mode: state.viewMode,
      showOverflow: state.showOverflow,
      ver: item.imgVer,
      onSelect: (region) => {
        state.editMode = true;
        state.selectedRegionId = region.id;
        els.btnEdit.classList.add("active");
        els.btnEdit.textContent = "Düzenle (açık)";
        renderSelected();
      },
    });
  }

  const meta: string[] = [
    item.name,
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

function setEditMode(on: boolean): void {
  state.editMode = on;
  els.btnEdit.classList.toggle("active", on);
  els.btnEdit.textContent = on ? "Düzenle (açık)" : "Düzenle";
  if (!on) state.selectedRegionId = null;
  renderSelected();
}

/* ------------------------------------------------- Autosave (proje manifesti) */

/** Bellekteki manifesti diske yazar; "Kaydedildi" göstergesini tazeler.
 *  Debounce gerekmez: yalnızca net eylem anlarında çağrılır (Uygula /
 *  Devre Dışı Bırak / Sil / yeni bölge). */
async function saveProject(): Promise<void> {
  if (!state.activeProject || !state.manifestMeta) return;
  if (state.saveBusy) return;
  state.saveBusy = true;
  try {
    const manifest: ProjectManifest = {
      ...state.manifestMeta,
      updated_at: new Date().toISOString(),
      pages: state.done.map((d, i) => ({
        index: i,
        name: d.name,
        source: d.input,
        result: d.result,
      })),
    };
    await invoke("save_project", { projectId: state.activeProject.id, manifest });
    state.savedAt = Date.now();
    state.savedFlash = true;
    renderSavedIndicator();
    void refreshProjects();
  } catch (err) {
    showBanner(`Kaydedilemedi: ${String(err)}`, "error");
  } finally {
    state.saveBusy = false;
  }
}

/* -------------------------------------------------------- bölge düzenleme */

/** Düzenleme sonrası önbellek tazeler ve görünümü yeniden kurar. */
function afterRegionEdit(): void {
  const item = selectedItem();
  if (!item) return;
  item.imgVer++;
  refreshOverflowWarning();
  const thumb = els.thumbs.children[state.selected]?.querySelector("img");
  if (thumb) {
    thumb.src = pageImageUrl(item.result.outputs.translated, item.imgVer);
  }
  renderSelected();
}

function editorApi(): EditorApi {
  return {
    onSelect(id) {
      state.selectedRegionId = id;
      renderSelected();
    },
    onCreateRegion(bbox) {
      const item = selectedItem();
      if (!item) return;
      const region: Region = {
        id: nextManualRegionId(),
        index: -1,
        label_name: "manual",
        bbox,
        original: "",
        translation: "",
        font_size: null,
        lines: 0,
        overflow: false,
        manual: true,
        disabled: false,
        committed: false,
      };
      item.result.regions.push(region);
      state.selectedRegionId = region.id;
      refreshOverflowWarning();
      renderSelected();
      void saveProject(); // autosave: yeni bölge anında kalıcı olur
    },
    async onApply(draft) {
      const item = selectedItem();
      if (!item) return;
      const r = item.result;
      const cur = r.regions.find((x) => x.id === draft.id);
      if (!cur) throw new Error("Bölge bulunamadı");

      const eraseBoxes: number[][] = [];
      if (draft.prevBbox && draft.prevBbox.join(",") !== draft.bbox.join(",")) {
        eraseBoxes.push(draft.prevBbox);
      }
      const res = (await request("re_render_region", {
        output: r.outputs.translated,
        cleaned: r.outputs.cleaned,
        region: {
          bbox: draft.bbox,
          translation: draft.disabled ? "" : draft.translation,
          erase: cur.manual ? "inpaint" : "paste",
          erase_boxes: eraseBoxes,
          style: draft.style,
        },
      })) as {
        font_size: number | null;
        lines: number;
        overflow: boolean;
        disabled: boolean;
        style_used?: Partial<Region["style"]>;
      };

      cur.bbox = draft.bbox;
      cur.font_size = res.font_size;
      cur.lines = res.lines;
      cur.overflow = res.overflow;
      cur.disabled = res.disabled;
      cur.committed = true;
      // Devre dışı bırakılsa bile metni koru (tekrar etkinleştirmek için).
      if (!draft.disabled) cur.translation = draft.translation;
      cur.style = { ...REGION_STYLE_DEFAULTS, ...(res.style_used ?? draft.style) };
      afterRegionEdit();
      void saveProject(); // autosave: Uygula anında diske yazılır
    },
    async onDisable(region) {
      const item = selectedItem();
      if (!item) return;
      const r = item.result;
      const cur = r.regions.find((x) => x.id === region.id);
      if (!cur) return;
      if (cur.committed) {
        await request("re_render_region", {
          output: r.outputs.translated,
          cleaned: r.outputs.cleaned,
          region: {
            bbox: cur.bbox,
            translation: "",
            erase: cur.manual ? "inpaint" : "paste",
            style: cur.style ?? null,
          },
        });
      }
      cur.disabled = true;
      cur.overflow = false;
      afterRegionEdit();
      void saveProject();
    },
    async onDelete(region) {
      const item = selectedItem();
      if (!item) return;
      const r = item.result;
      if (region.committed) {
        await request("re_render_region", {
          output: r.outputs.translated,
          cleaned: r.outputs.cleaned,
          region: {
            bbox: region.bbox,
            translation: "",
            erase: region.manual ? "inpaint" : "paste",
            style: region.style ?? null,
          },
        });
      }
      r.regions = r.regions.filter((x) => x.id !== region.id);
      if (state.selectedRegionId === region.id) state.selectedRegionId = null;
      refreshOverflowWarning();
      renderSelected();
      void saveProject();
    },
  };
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
    const base = stripExt(item.name);
    try {
      if (r.outputs.translated) {
        await invoke("copy_file", { src: r.outputs.translated, dstDir: folder });
      }
      await invoke("write_text_file", {
        path: `${folder}/${base}_result.json`,
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

/** F11: mevcut tam ekran durumunu tersine çevirir (açtıysa kapatır). */
async function toggleFullscreen(): Promise<void> {
  const win = getCurrentWindow();
  await win.setFullscreen(!(await win.isFullscreen()));
}

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

  els.tabMangas.addEventListener("click", () => setTab("mangas"));
  els.tabAnime.addEventListener("click", () => setTab("anime"));
  els.btnBackMangas.addEventListener("click", () => setTab("mangas"));
  els.btnNewProject.addEventListener("click", openNewProjectModal);
  els.modalClose.addEventListener("click", closeNewProjectModal);
  els.modalBackdrop.addEventListener("click", closeNewProjectModal);

  // Kart grid zoom: Ctrl (veya Mac trackpad pinch için Cmd/Meta) + tekerlek.
  // passive:false şart — yoksa preventDefault çalışmaz ve tarayıcının kendi
  // sayfa zoom'u (Ctrl+scroll) devreye girip bizimkiyle çakışır.
  els.projectGrid.addEventListener(
    "wheel",
    (ev) => {
      if (!ev.ctrlKey && !ev.metaKey) return; // normal scroll'a dokunma
      ev.preventDefault();
      const delta = ev.deltaY || ev.deltaX;
      if (delta === 0) return;
      const next = state.cardSize * (delta < 0 ? CARD_SIZE_STEP : 1 / CARD_SIZE_STEP);
      if (clampCardSize(next) !== state.cardSize) {
        setCardSize(next);
        scheduleCardPrefSave();
      }
    },
    { passive: false },
  );

  els.confirmOk.addEventListener("click", () => {
    const cb = confirmCallback;
    closeConfirm();
    cb?.();
  });
  els.confirmCancel.addEventListener("click", closeConfirm);
  $<HTMLDivElement>("confirm-backdrop").addEventListener("click", closeConfirm);

  window.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    if (!els.confirmModal.classList.contains("hidden")) {
      closeConfirm();
    } else if (!els.newProjectModal.classList.contains("hidden") && !state.running) {
      closeNewProjectModal();
    }
  });

  // F11: tam ekran aç/kapa. preventDefault WebView'in kendi tam ekran
  // davranışını (ve tarayıcı varsayılanını) engeller, Tauri API'siyle
  // çakışma olmaz. toggle: zaten tam ekrandaysa normal boyuta döner.
  window.addEventListener("keydown", (ev) => {
    if (ev.key !== "F11") return;
    ev.preventDefault();
    void toggleFullscreen();
  });

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

  els.btnEdit.addEventListener("click", () => setEditMode(!state.editMode));

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
  setTab("mangas");
  renderSavedIndicator();
  setCardSize(state.cardSize); // CSS varsayılanını JS durumuyla hizala
  await loadCardSizePref();
  await refreshProjects();
}

void main();
