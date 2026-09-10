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
import { disposeEditor, renderEditor, type EditorApi } from "./editor";

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
  starting: false,
  cancelRequested: false,
  done: [] as DonePage[],
  failedCount: 0,
  selected: 0,
  viewMode: "compare" as ViewMode,
  /** Sonuç görünümünde bölge kutuları görünür mü? (varsayılan: kapalı, sayfa temiz kalsın) */
  showBoxes: false,
  currentJob: "",
  editMode: false,
  editorBusy: false,
  selectedRegionId: null as number | null,
  projects: [] as ProjectSummary[],
  activeProject: null as { id: string; name: string } | null,
  cardSize: 220,
  manifestMeta: null as ManifestMeta | null,
  savedAt: null as number | null,
  savedFlash: false,
  saveBusy: false,
  projectsLoading: false,
  projectsError: null as string | null,
  openingProjectId: null as string | null,
  deletingProjectId: null as string | null,
  sourceBusy: false,
  providerLoadError: false,
  serviceReady: false,
};

/** Elle eklenen bölgeler için benzersiz id'ler (otomatik id'lerle çakışmaz). */
let manualRegionSeq = 1000;
function syncManualRegionSeq(): void {
  const maxExisting = state.done.reduce(
    (maxId, page) =>
      page.result.regions.reduce(
        (pageMax, region) => Math.max(pageMax, Number.isFinite(region.id) ? region.id : 0),
        maxId,
      ),
    1000,
  );
  manualRegionSeq = Math.max(1000, maxExisting);
}

function nextManualRegionId(): number {
  // Aynı oturumda başka bir sayfa/proje yüklendiyse de mevcut id'lerin üstünden devam et.
  syncManualRegionSeq();
  return ++manualRegionSeq;
}

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
  app: $<HTMLElement>("app"),
  header: $<HTMLElement>("app-header"),
  tabbar: $<HTMLElement>("app-tabbar"),
  editorNav: $<HTMLElement>("editor-nav"),
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
  btnNewProjectEmpty: $<HTMLButtonElement>("btn-new-project-empty"),
  resultsCard: $<HTMLElement>("results-card"),
  resultsTitle: $<HTMLHeadingElement>("results-title"),
  newProjectModal: $<HTMLDivElement>("new-project-modal"),
  progressModal: $<HTMLDivElement>("progress-modal"),
  modalBackdrop: $<HTMLDivElement>("modal-backdrop"),
  modalClose: $<HTMLButtonElement>("modal-close"),
  projectName: $<HTMLInputElement>("project-name"),
  confirmModal: $<HTMLDivElement>("confirm-modal"),
  confirmTitle: $<HTMLHeadingElement>("confirm-title"),
  confirmMessage: $<HTMLElement>("confirm-message"),
  confirmOk: $<HTMLButtonElement>("confirm-ok"),
  confirmCancel: $<HTMLButtonElement>("confirm-cancel"),
  exportModal: $<HTMLDivElement>("export-modal"),
  exportStatus: $<HTMLParagraphElement>("export-status"),
  exportBackdrop: $<HTMLDivElement>("export-backdrop"),
  exportFolderBtn: $<HTMLButtonElement>("export-folder-btn"),
  exportFolderText: $<HTMLSpanElement>("export-folder-text"),
  exportFormatGroup: $<HTMLDivElement>("export-format-group"),
  exportFormatHint: $<HTMLParagraphElement>("export-format-hint"),
  exportConfirm: $<HTMLButtonElement>("export-confirm"),
  exportCancel: $<HTMLButtonElement>("export-cancel"),
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
  overallBar: $<HTMLDivElement>("overall-bar"),
  overallFill: $<HTMLDivElement>("overall-fill"),
  overallHint: $<HTMLParagraphElement>("overall-hint"),
  pageBar: $<HTMLDivElement>("page-bar"),
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
  state.serviceReady = kind === "ok";
  els.sidecarStatus.className = `badge ${kind}`;
  els.sidecarStatus.textContent = text;
  syncConfigControls();
}

const modalReturnFocus = new WeakMap<HTMLElement, HTMLElement>();

function visibleModal(): HTMLElement | null {
  return [els.confirmModal, els.exportModal, els.progressModal, els.newProjectModal].find(
    (modal) => !modal.classList.contains("hidden"),
  ) ?? null;
}

function showModal(modal: HTMLElement, initialFocus: HTMLElement): void {
  const active = document.activeElement;
  if (active instanceof HTMLElement) modalReturnFocus.set(modal, active);
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
  els.app.setAttribute("inert", "");
  document.documentElement.classList.add("modal-open");
  document.body.classList.add("modal-open");
  window.setTimeout(() => initialFocus.focus(), 0);
}

function hideModal(modal: HTMLElement): void {
  modal.classList.add("hidden");
  modal.setAttribute("aria-hidden", "true");
  if (!visibleModal()) {
    els.app.removeAttribute("inert");
    document.documentElement.classList.remove("modal-open");
    document.body.classList.remove("modal-open");
  }

  const returnTarget = modalReturnFocus.get(modal);
  modalReturnFocus.delete(modal);
  window.setTimeout(() => {
    if (returnTarget?.isConnected && !returnTarget.closest(".hidden")) returnTarget.focus();
  }, 0);
}

/** Bir modalı kapatırken odağı önceki pencereye geri vermeden diğerine taşır. */
function swapModal(from: HTMLElement, to: HTMLElement, initialFocus: HTMLElement): void {
  const returnTarget = modalReturnFocus.get(from);
  modalReturnFocus.delete(from);
  if (returnTarget) {
    modalReturnFocus.set(to, returnTarget);
  } else {
    const active = document.activeElement;
    if (active instanceof HTMLElement) modalReturnFocus.set(to, active);
  }

  from.classList.add("hidden");
  from.setAttribute("aria-hidden", "true");
  to.classList.remove("hidden");
  to.setAttribute("aria-hidden", "false");
  els.app.setAttribute("inert", "");
  document.documentElement.classList.add("modal-open");
  document.body.classList.add("modal-open");
  window.setTimeout(() => initialFocus.focus(), 0);
}

function trapModalFocus(ev: KeyboardEvent): void {
  if (ev.key !== "Tab") return;
  const modal = visibleModal();
  if (!modal) return;
  const focusable = Array.from(
    modal.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((node) => !node.closest(".hidden"));
  if (!focusable.length) {
    ev.preventDefault();
    modal.focus({ preventScroll: true });
    return;
  }

  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (!modal.contains(active)) {
    ev.preventDefault();
    first.focus();
  } else if (ev.shiftKey && active === first) {
    ev.preventDefault();
    last.focus();
  } else if (!ev.shiftKey && active === last) {
    ev.preventDefault();
    first.focus();
  }
}

function enableRadioGroupKeyboard(group: HTMLElement): void {
  group.addEventListener("keydown", (ev) => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(ev.key)) return;
    const buttons = Array.from(group.querySelectorAll<HTMLButtonElement>('button[role="radio"]:not(:disabled)'));
    if (!buttons.length) return;
    const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (current < 0) return;
    ev.preventDefault();
    const backwards = ev.key === "ArrowLeft" || ev.key === "ArrowUp";
    const next = ev.key === "Home"
      ? 0
      : ev.key === "End"
        ? buttons.length - 1
        : (current + (backwards ? -1 : 1) + buttons.length) % buttons.length;
    buttons[next].focus();
    buttons[next].click();
  });
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
  if (state.editorBusy && tab !== "editor") return;
  if (tab !== "editor") disposeEditor();
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
  els.tabMangas.tabIndex = onMangas ? 0 : -1;
  els.tabAnime.tabIndex = onAnime ? 0 : -1;
  els.mangasView.classList.toggle("hidden", !onMangas);
  els.animeView.classList.toggle("hidden", !onAnime);
  els.mangasView.setAttribute("aria-hidden", String(!onMangas));
  els.animeView.setAttribute("aria-hidden", String(!onAnime));
  els.editorView.classList.toggle("hidden", tab !== "editor");
  updateChrome();
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
  if (state.activeProject && state.saveBusy) {
    els.savedIndicator.textContent = "Kaydediliyor…";
    els.savedIndicator.className = `${base} dim`;
    els.savedIndicator.title = `${state.activeProject.name} kaydediliyor`;
    return;
  }
  if (!state.activeProject) {
    els.savedIndicator.textContent = "Proje açık değil";
    els.savedIndicator.className = `${base} dim`;
    els.savedIndicator.title = "Henüz bir proje açık değil";
    return;
  }
  if (failedProjectSaves.has(state.activeProject.id)) {
    els.savedIndicator.textContent = "Kaydedilemedi";
    els.savedIndicator.className = `${base} error`;
    els.savedIndicator.title = `${state.activeProject.name} için kaydedilmemiş değişiklikler var`;
    return;
  }
  if (!state.savedAt) {
    els.savedIndicator.textContent = "Henüz kaydedilmedi";
    els.savedIndicator.className = `${base} dim`;
    els.savedIndicator.title = `${state.activeProject.name} henüz kaydedilmedi`;
    return;
  }
  const time = new Date(state.savedAt).toLocaleTimeString("tr-TR");
  els.savedIndicator.textContent = state.savedFlash ? `Kaydedildi ${time}` : `Son kayıt ${time}`;
  els.savedIndicator.className = `${base} ok`;
  els.savedIndicator.title = `Son kayıt: ${new Date(state.savedAt).toLocaleString("tr-TR")}`;
}

type ProjectsEmptyState = "empty" | "loading" | "error";

function setProjectsEmptyState(kind: ProjectsEmptyState): void {
  const title = els.projectsEmpty.querySelector<HTMLElement>("h3");
  const detail = els.projectsEmpty.querySelector<HTMLElement>(".hint");
  const icon = els.projectsEmpty.querySelector<HTMLElement>(".projects-empty-icon");
  const isEmpty = kind === "empty";

  if (title) {
    title.textContent =
      kind === "loading"
        ? "Projeler yükleniyor…"
        : kind === "error"
          ? "Projeler yüklenemedi"
          : "İlk manga çevirinizi oluşturun";
  }
  if (detail) {
    detail.textContent =
      kind === "loading"
        ? "Bu bilgisayardaki proje kütüphanesi hazırlanıyor."
        : kind === "error"
          ? "Yeniden denemek için bu alana tıklayın veya Enter tuşuna basın."
          : "Bir sayfa ya da manga klasörü seçin; PS Editor metinleri algılayıp temizlesin, çevirsin ve yeniden yerleştirsin.";
  }
  icon?.classList.toggle("hidden", !isEmpty);
  els.btnNewProjectEmpty.classList.toggle("hidden", !isEmpty);

  if (kind === "loading") {
    els.projectsEmpty.setAttribute("role", "status");
    els.projectsEmpty.removeAttribute("tabindex");
  } else if (kind === "error") {
    els.projectsEmpty.setAttribute("role", "button");
    els.projectsEmpty.tabIndex = 0;
  } else {
    els.projectsEmpty.removeAttribute("role");
    els.projectsEmpty.removeAttribute("tabindex");
  }
}

let projectsRefreshQueued = false;
let projectsRefreshLoop: Promise<void> | null = null;

async function drainProjectRefreshes(): Promise<void> {
  state.projectsLoading = true;
  renderProjects();
  try {
    while (projectsRefreshQueued) {
      projectsRefreshQueued = false;
      state.projectsError = null;
      try {
        state.projects = (await invoke("list_projects")) as ProjectSummary[];
      } catch (err) {
        state.projectsError = `Proje listesi alınamadı: ${String(err)}`;
        showBanner(state.projectsError, "error");
      }
    }
  } finally {
    state.projectsLoading = false;
    renderProjects();
    projectsRefreshLoop = null;
  }
}

/** Eşzamanlı yenileme isteklerini tek döngüde sıraya alır; son istek düşmez. */
function refreshProjects(): Promise<void> {
  projectsRefreshQueued = true;
  if (!projectsRefreshLoop) projectsRefreshLoop = drainProjectRefreshes();
  return projectsRefreshLoop;
}

function renderProjects(): void {
  els.projectGrid.setAttribute("aria-busy", String(state.projectsLoading));
  if (state.projectsLoading && state.projects.length === 0) {
    els.projectGrid.replaceChildren();
    setProjectsEmptyState("loading");
    els.projectsEmpty.classList.remove("hidden");
    return;
  }

  els.projectGrid.replaceChildren();
  const showLoadError = !!state.projectsError && state.projects.length === 0;
  if (showLoadError) {
    setProjectsEmptyState("error");
  } else {
    setProjectsEmptyState("empty");
  }
  els.projectsEmpty.classList.toggle("hidden", state.projects.length > 0);
  for (const p of state.projects) {
    const card = document.createElement("article");
    card.className = "project-card";
    card.dataset.projectId = p.id;

    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.className = "project-open";
    openButton.title = `${p.name} — aç`;
    openButton.setAttribute("aria-label", `${p.name} projesini aç`);

    let thumb: HTMLElement;
    if (p.thumb) {
      const img = document.createElement("img");
      img.className = "project-thumb";
      img.src = pageImageUrl(p.thumb);
      img.alt = p.name;
      img.loading = "lazy";
      thumb = img;
    } else {
      thumb = document.createElement("span");
      thumb.className = "project-thumb-placeholder";
      thumb.textContent = "Önizleme yok";
    }

    const body = document.createElement("span");
    body.className = "project-card-body";
    const name = document.createElement("span");
    name.className = "project-card-name";
    name.textContent = p.name;
    name.title = p.name;
    const meta = document.createElement("span");
    meta.className = "project-card-meta";
    meta.textContent = `${p.page_count} sayfa işlendi · son düzenleme ${fmtTime(p.updated_at)}`;
    meta.dataset.defaultText = meta.textContent;
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

    openButton.append(thumb, body);
    openButton.addEventListener("click", () => void openProject(p.id));
    card.append(openButton, del);
    els.projectGrid.appendChild(card);
  }
  syncProjectOpenState();
}

function syncProjectOpenState(): void {
  const busyId = state.openingProjectId ?? state.deletingProjectId;
  els.projectGrid.setAttribute("aria-busy", String(!!busyId || state.projectsLoading));
  for (const card of els.projectGrid.querySelectorAll<HTMLElement>(".project-card")) {
    const busy = !!busyId;
    const isOpening = card.dataset.projectId === busyId;
    card.setAttribute("aria-busy", String(isOpening));
    card.classList.toggle("is-busy", busy);
    const openButton = card.querySelector<HTMLButtonElement>(".project-open");
    if (openButton) openButton.disabled = busy;
    const del = card.querySelector<HTMLButtonElement>(".project-delete");
    if (del) del.disabled = busy;
    const meta = card.querySelector<HTMLElement>(".project-card-meta");
    if (meta) {
      meta.textContent = isOpening
        ? state.deletingProjectId === busyId
          ? "Proje siliniyor…"
          : "Proje açılıyor…"
        : meta.dataset.defaultText ?? "";
    }
  }
}

async function openProject(id: string): Promise<void> {
  if (state.openingProjectId || state.deletingProjectId || state.editorBusy) return;
  state.openingProjectId = id;
  syncProjectOpenState();
  try {
    // Başka bir projeden kalan son düzenlemeler diske inmeden yeni manifesti okuma.
    if (!(await flushProjectSaves())) {
      showBanner("Kaydedilemeyen değişiklikler var; proje değiştirilmedi. Bağlantıyı kontrol edip yeniden deneyin.", "error");
      return;
    }
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
    state.failedCount = 0;
    state.selected = 0;
    state.editMode = false;
    state.selectedRegionId = null;
    syncManualRegionSeq();
    syncEditControls();
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
  } finally {
    state.openingProjectId = null;
    syncProjectOpenState();
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
  showModal(els.confirmModal, els.confirmCancel);
}

function closeConfirm(): void {
  confirmCallback = null;
  hideModal(els.confirmModal);
}

async function deleteProject(p: ProjectSummary): Promise<void> {
  if (state.openingProjectId || state.deletingProjectId) return;
  state.deletingProjectId = p.id;
  syncProjectOpenState();
  try {
    // Silinecek projeye ait kuyruktaki bir kayıt işleminin sonradan dosyayı geri
    // oluşturmasını engellemek için önce mevcut kayıtları tamamla.
    if (!(await flushProjectSaves())) {
      showBanner("Kaydedilemeyen değişiklikler varken proje silinmedi. Yeniden deneyin.", "error");
      return;
    }
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
  } finally {
    state.deletingProjectId = null;
    syncProjectOpenState();
  }
}

/* ------------------------------------------------- "Yeni çeviri ekle" modalı */

function openNewProjectModal(): void {
  if (state.editorBusy) return;
  state.pages = [];
  state.sourcePath = null;
  state.sourceBusy = false;
  els.sourceInfo.textContent = "";
  els.sourceInfo.setAttribute("aria-busy", "false");
  els.sourceInfo.classList.add("hidden");
  els.projectName.value = "";
  syncConfigControls();
  showModal(els.newProjectModal, els.btnPickFile);
}

function closeNewProjectModal(): void {
  if (state.running || state.starting || state.sourceBusy) return; // İşlem/tarama sürerken kapatılamaz.
  hideModal(els.newProjectModal);
}

function defaultProjectName(): string {
  if (!state.sourcePath) return "Yeni Proje";
  return state.sourceType === "folder"
    ? basename(state.sourcePath)
    : stripExt(basename(state.sourcePath));
}

/* -------------------------------------------------------- kaynak seçimi */

async function pickFile(): Promise<void> {
  if (state.running || state.sourceBusy) return;
  try {
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
  } catch (err) {
    const message = `Dosya seçilemedi: ${String(err)}`;
    showBanner(message, "error");
    showSourceError(message);
  }
}

async function pickFolder(): Promise<void> {
  if (state.running || state.sourceBusy) return;
  try {
    const dir = await open({ directory: true, multiple: false, title: "Sayfaları içeren klasörü seçin" });
    if (!dir) return;
    const folder = Array.isArray(dir) ? dir[0] : dir;
    state.pages = [];
    state.sourcePath = folder;
    state.sourceType = "folder";
    state.sourceLabel = "";
    setSourceBusy(true);
    els.sourceInfo.textContent = "Klasör taranıyor…";
    els.sourceInfo.classList.remove("hidden");
    const images = (await invoke("list_images", { dir: folder })) as string[];
    if (!images.length) {
      const message = "Seçilen klasörde görsel bulunamadı (PNG/JPG/WebP/BMP/GIF).";
      showBanner(message, "warn");
      state.pages = [];
      state.sourcePath = folder;
      state.sourceType = "folder";
      state.sourceLabel = `0 sayfa · ${folder}`;
      showSourceError(message);
      return;
    }
    state.pages = images;
    state.sourcePath = folder;
    state.sourceType = "folder";
    state.sourceLabel = `${images.length} sayfa · ${folder}`;
    showSourceInfo();
  } catch (err) {
    const message = `Klasör okunamadı: ${String(err)}`;
    showBanner(message, "error");
    showSourceError(message);
  } finally {
    setSourceBusy(false);
  }
}

function showSourceInfo(): void {
  els.sourceInfo.textContent = state.sourceLabel;
  els.sourceInfo.classList.remove("hidden");
  syncConfigControls();
}

function showSourceError(message: string): void {
  els.sourceInfo.textContent = message;
  els.sourceInfo.classList.remove("hidden");
  syncConfigControls();
}

function setSourceBusy(busy: boolean): void {
  state.sourceBusy = busy;
  els.sourceInfo.setAttribute("aria-busy", String(busy));
  syncConfigControls();
}

/* ---------------------------------------------------------------- modlar */

function setMode(mode: Mode): void {
  state.mode = mode;
  for (const btn of els.modeGroup.querySelectorAll<HTMLButtonElement>("button.seg")) {
    const active = btn.dataset.mode === mode;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-checked", String(active));
    btn.tabIndex = active ? 0 : -1;
  }
  els.modeHint.textContent = MODE_HINTS[mode];
  syncProviderDefault();
  syncConfigControls();
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
    if (!Array.isArray(res.providers) || res.providers.length === 0) {
      throw new Error("Sağlayıcı listesi boş döndü");
    }
    state.providers = res.providers;
    state.providerLoadError = false;
  } catch {
    state.providerLoadError = true;
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
  const loadWarning = state.providerLoadError
    ? "Sağlayıcı durumu alınamadı; varsayılan liste gösteriliyor. "
    : "";
  if (!info) {
    els.providerHint.textContent = loadWarning.trim();
    els.providerHint.classList.toggle("warn-text", !!loadWarning);
    return;
  }
  const shouldWarn = state.providerLoadError || info.name === "mock" || (info.needs_key && !info.has_key);
  els.providerHint.classList.toggle("warn-text", shouldWarn);
  if (state.mode === "local") {
    els.providerHint.textContent = `${loadWarning}Yerel modda sağlayıcı seçimi geçersiz; Ollama kullanılır.`;
  } else if (info.needs_key && !info.has_key) {
    els.providerHint.textContent =
      `${loadWarning}Bu sağlayıcı için API anahtarı yok. Gerçek çeviri başlatılamaz.`;
  } else if (info.needs_key) {
    els.providerHint.textContent = `${loadWarning}API anahtarı sistemde kayıtlı (güvenli depo).`;
  } else if (info.name === "mock") {
    els.providerHint.textContent = `${loadWarning}Test modu: çıktı akışı denenir, gerçek çeviri yapılmaz.`;
  } else {
    els.providerHint.textContent = loadWarning.trim();
  }
}

/* ----------------------------------------------------------- ilerleme UI */

function setPageProgress(progress: number, label: string, detail: string): void {
  const pct = Math.max(0, Math.min(100, Math.round(progress * 100)));
  els.pageFill.style.width = `${pct}%`;
  els.pagePct.textContent = `%${pct}`;
  els.pageBar.setAttribute("aria-valuenow", String(pct));
  els.stageLabel.textContent = label;
  els.stageDetail.textContent = detail;
}

function setOverallProgress(progress: number): void {
  const pct = Math.max(0, Math.min(100, Math.round(progress * 100)));
  els.overallFill.style.width = `${pct}%`;
  els.overallBar.setAttribute("aria-valuenow", String(pct));
}

function onProgress(p: ProgressPayload): void {
  if (!state.running) return;
  if (p.job_id && p.job_id !== state.currentJob) return;
  const progress = p.progress ?? 0;
  setPageProgress(progress, stageLabel(p.name), p.message ?? "");

  const total = state.pages.length || 1;
  const completed = state.done.length + state.failedCount;
  const overall = (completed + Math.min(1, progress)) / total;
  setOverallProgress(overall);
  els.progressCount.textContent = `${completed}${state.cancelRequested ? "" : " / " + total} sayfa işlendi`;
}

/* ------------------------------------------------------------ ana akış */

function syncConfigControls(): void {
  const settingsBusy = state.running || state.starting;
  const configBusy = settingsBusy || state.sourceBusy;
  const providerInfo = state.providers.find((provider) => provider.name === state.provider);
  const missingKey = state.mode !== "local" && !!providerInfo?.needs_key && !providerInfo.has_key;
  els.btnStart.disabled = configBusy || !state.pages.length || !state.serviceReady || missingKey;
  els.btnStart.textContent = state.starting ? "Hazırlanıyor…" : "İşlemeyi başlat";
  els.btnStart.title = !state.serviceReady
    ? "Çeviri altyapısı henüz hazır değil"
    : missingKey
      ? "Seçilen sağlayıcı için API anahtarı gerekli"
      : "";
  els.btnPickFile.disabled = configBusy;
  els.btnPickFolder.disabled = configBusy;
  els.langSelect.disabled = settingsBusy;
  els.modalClose.disabled = configBusy;
  els.projectName.disabled = settingsBusy;
  els.providerSelect.disabled = settingsBusy || state.mode === "local";
  els.providerField.classList.toggle("dim", settingsBusy || state.mode === "local");
  els.providerField.setAttribute("aria-disabled", String(settingsBusy || state.mode === "local"));
  els.modeGroup.setAttribute("aria-disabled", String(settingsBusy));
  for (const btn of els.modeGroup.querySelectorAll<HTMLButtonElement>("button.seg")) {
    btn.disabled = settingsBusy;
  }
}

function setProgressBusy(busy: boolean): void {
  els.progressCard.setAttribute("aria-busy", String(busy));
  els.progressModal.setAttribute("aria-busy", String(busy));
}

function setRunning(running: boolean): void {
  state.running = running;
  syncConfigControls();
  els.btnCancel.classList.toggle("hidden", !running);
  els.btnCancel.disabled = !running;
  els.btnCancel.textContent = "İptal";
  setProgressBusy(running);
}

async function run(): Promise<void> {
  const pages = [...state.pages];
  if (!pages.length || state.running || state.starting) return;
  state.starting = true;
  syncConfigControls();
  setOverallProgress(0);
  setPageProgress(0, "Hazırlanıyor…", "Önceki proje kayıtları denetleniyor…");
  els.progressCount.textContent = "";
  setProgressBusy(true);
  swapModal(els.newProjectModal, els.progressModal, els.progressModal);
  if (!(await flushProjectSaves())) {
    state.starting = false;
    setProgressBusy(false);
    swapModal(els.progressModal, els.newProjectModal, els.btnStart);
    showBanner("Önceki projedeki değişiklikler kaydedilemedi; yeni proje başlatılmadı.", "error");
    syncConfigControls();
    return;
  }

  state.done = [];
  state.failedCount = 0;
  state.selected = 0;
  state.cancelRequested = false;
  state.editMode = false;
  state.selectedRegionId = null;
  manualRegionSeq = 1000;
  syncEditControls();
  hideBanner();
  state.starting = false;
  setRunning(true);
  els.resultsCard.classList.add("hidden");
  setOverallProgress(0);
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
    swapModal(els.progressModal, els.newProjectModal, els.btnStart);
    const message = `Proje oluşturulamadı: ${String(err)}`;
    setPageProgress(0, "İşlem başlatılamadı", message);
    els.overallHint.textContent = "Ayarları kontrol edip yeniden deneyin.";
    showBanner(message, "error");
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
        const completed = state.done.length + state.failedCount;
        setOverallProgress(completed / pages.length);
        els.progressCount.textContent = `${completed}/${pages.length} sayfa işlendi`;
      }
    } catch (err) {
      state.failedCount++;
      setOverallProgress((state.done.length + state.failedCount) / pages.length);
      els.progressCount.textContent = `${state.done.length + state.failedCount} / ${pages.length} sayfa işlendi`;
      showBanner(`"${pageName}" işlenemedi: ${String(err)}`, "error");
    }
  }

  setRunning(false);
  hideModal(els.progressModal);

  if (state.done.length) {
    syncManualRegionSeq();
    state.savedAt = Date.now();
    state.savedFlash = false;
    renderSavedIndicator();
    renderResults();
    els.resultsTitle.textContent = name;
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
    thumb.disabled = state.editorBusy;
    if (idx === state.selected) thumb.setAttribute("aria-current", "page");
    const img = document.createElement("img");
    img.src = pageImageUrl(item.result.outputs.translated, item.imgVer);
    img.alt = `Sayfa ${idx + 1}`;
    thumb.appendChild(img);
    thumb.addEventListener("click", () => {
      if (idx === state.selected) return;
      withEditorDraftGuard(() => {
        state.selected = idx;
        state.selectedRegionId = null;
        renderSelected();
      });
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
    if (state.viewMode === "side") {
      disposeEditor();
      const grid = document.createElement("div");
      grid.className = "split-edit-grid";

      const origPanel = document.createElement("figure");
      origPanel.className = "side-panel split-orig";
      const origImg = document.createElement("img");
      origImg.src = pageImageUrl(r.image, item.imgVer);
      origImg.alt = "Orijinal sayfa";
      origImg.draggable = false;
      origImg.style.pointerEvents = "none";
      origPanel.appendChild(origImg);
      const cap = document.createElement("figcaption");
      cap.textContent = "Orijinal";
      origPanel.appendChild(cap);

      const rightPanel = document.createElement("div");
      rightPanel.className = "split-editor-right";

      const origStage = document.createElement("div");
      origStage.className = "split-orig-stage";
      origStage.appendChild(origImg);
      origStage.appendChild(cap);
      origPanel.replaceChildren(origStage, cap);

      renderEditor(rightPanel, r, state.selectedRegionId, item.imgVer, editorApi(), rightPanel);

      grid.append(origPanel, rightPanel);
      els.viewer.replaceChildren(grid);

      const syncOrigSize = () => {
        const stageEl = rightPanel.querySelector(".editor-stage") as HTMLElement | null;
        if (stageEl) {
          origStage.style.width = stageEl.style.width;
          origStage.style.height = stageEl.style.height;
        }
      };
      void origImg.decode().then(() => {
        syncOrigSize();
        new ResizeObserver(syncOrigSize).observe(rightPanel);
      }).catch(() => undefined);
    } else {
      renderEditor(els.viewer, r, state.selectedRegionId, item.imgVer, editorApi());
    }
  } else {
    disposeEditor();
    renderViewer(els.viewer, r, {
      mode: state.viewMode,
      showBoxes: state.showBoxes,
      ver: item.imgVer,
      onSelect: (region) => {
        state.selectedRegionId = region.id;
        setEditMode(true);
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
    if (idx === state.selected) {
      btn.setAttribute("aria-current", "page");
    } else {
      btn.removeAttribute("aria-current");
    }
  });
  syncEditControls();
}

/** Editör görünümündeyken üst chrome (logo, durum yazıları, sekmeler)
 * gizlenir; yalnızca "Mangalar'a dön" düğmesi görünür kalır. */
function updateChrome(): void {
  const hide = state.tab === "editor";
  els.header.classList.toggle("hidden", hide);
  els.tabbar.classList.toggle("hidden", hide);
}

function hasPendingEditorDraft(): boolean {
  if (!state.editMode) return false;
  const selectedRegion = selectedItem()?.result.regions.find((region) => region.id === state.selectedRegionId);
  const uncommittedManualRegion = !!selectedRegion?.manual && selectedRegion.committed === false;
  return (
    uncommittedManualRegion ||
    !!els.viewer.querySelector(".dirty-hint:not(.hidden), .reg-box.dirty")
  );
}

function discardUncommittedManualRegion(): void {
  const item = selectedItem();
  if (!item || state.selectedRegionId == null) return;
  const region = item.result.regions.find((candidate) => candidate.id === state.selectedRegionId);
  if (!region?.manual || region.committed !== false) return;
  item.result.regions = item.result.regions.filter((candidate) => candidate.id !== region.id);
  state.selectedRegionId = null;
  refreshOverflowWarning();
  void saveProject();
}

/** Editör taslağı yerel olduğu için yeniden render edecek eylemleri kullanıcıya bildirir. */
function withEditorDraftGuard(action: () => void): void {
  if (state.editorBusy) {
    showBanner("Bölge işlemi tamamlanırken görünüm değiştirilemez.", "warn");
    return;
  }
  if (!hasPendingEditorDraft()) {
    action();
    return;
  }
  confirmDialog(
    "Uygulanmamış değişiklikler",
    "Bu bölgedeki değişiklikler henüz uygulanmadı. Devam ederseniz son değişiklikler kaybolacak.",
    "Değişiklikleri At",
    () => {
      discardUncommittedManualRegion();
      action();
    },
  );
}

function syncEditControls(): void {
  els.btnEdit.classList.toggle("active", state.editMode);
  els.btnEdit.textContent = state.editMode ? "Düzenlemeyi bitir" : "Düzenle";
  els.btnEdit.title = state.editMode ? "Sonuç görünümüne dön" : "Metin bölgelerini düzenle";
  els.btnEdit.setAttribute("aria-pressed", String(state.editMode));
  els.btnEdit.disabled = state.editorBusy;
  els.btnBackMangas.disabled = state.editorBusy;

  els.btnExport.disabled = state.editMode || state.editorBusy;
  els.btnExport.setAttribute("aria-disabled", String(state.editMode || state.editorBusy));
  els.btnExport.title = state.editMode ? "Dışa aktarmadan önce düzenlemeyi bitirin" : "Çıktıları dışa aktar";
  els.resultsCard.setAttribute("aria-busy", String(state.editorBusy));
  for (const thumb of els.thumbs.querySelectorAll<HTMLButtonElement>("button.thumb")) {
    thumb.disabled = state.editorBusy;
  }

  els.btnOverflow.classList.toggle("active", state.showBoxes);
  els.btnOverflow.setAttribute("aria-pressed", String(state.showBoxes));
  els.btnOverflow.disabled = state.editMode;
  els.btnOverflow.setAttribute("aria-disabled", String(state.editMode));

  els.viewModeGroup.removeAttribute("aria-disabled");
  for (const btn of els.viewModeGroup.querySelectorAll<HTMLButtonElement>("button.seg")) {
    const active = btn.dataset.view === state.viewMode;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-checked", String(active));
    btn.tabIndex = active ? 0 : -1;
  }
}

function setEditMode(on: boolean): void {
  if (state.editorBusy) return;
  if (state.editMode === on) {
    syncEditControls();
    // İlk açılışta durum değişmiş olsa da görünüm henüz editöre dönmemiş olabilir.
    if (on && !els.viewer.querySelector(".editor-wrap")) renderSelected();
    return;
  }
  state.editMode = on;
  if (!on) state.selectedRegionId = null;
  syncEditControls();
  updateChrome();
  renderSelected();
}

/* ------------------------------------------------- Autosave (proje manifesti) */

interface PendingProjectSave {
  projectId: string;
  projectName: string;
  manifest: ProjectManifest;
}

const pendingProjectSaves = new Map<string, PendingProjectSave>();
const failedProjectSaves = new Map<string, PendingProjectSave>();
let saveLoop: Promise<void> | null = null;

async function drainProjectSaves(): Promise<void> {
  let savedAny = false;
  try {
    while (pendingProjectSaves.size) {
      const next = pendingProjectSaves.entries().next().value as
        | [string, PendingProjectSave]
        | undefined;
      if (!next) break;
      const [projectId, job] = next;
      pendingProjectSaves.delete(projectId);
      try {
        await invoke("save_project", { projectId: job.projectId, manifest: job.manifest });
        failedProjectSaves.delete(job.projectId);
        savedAny = true;
        if (state.activeProject?.id === job.projectId) {
          state.savedAt = Date.parse(job.manifest.updated_at) || Date.now();
          state.savedFlash = true;
        }
      } catch (err) {
        // Anlık görüntüyü bellekte tut; kullanıcı yeniden denerse aynı proje
        // verisini kaybetmeden tekrar yazmayı dene.
        failedProjectSaves.set(job.projectId, job);
        showBanner(`"${job.projectName}" kaydedilemedi: ${String(err)}`, "error");
      }
    }
  } finally {
    if (savedAny) void refreshProjects();
    saveLoop = null;
    state.saveBusy = false;
    renderSavedIndicator();
  }
}

function ensureSaveLoop(): Promise<void> {
  if (saveLoop) return saveLoop;
  state.saveBusy = true;
  renderSavedIndicator();
  saveLoop = drainProjectSaves();
  return saveLoop;
}

function queueProjectSave(
  project: { id: string; name: string },
  meta: ManifestMeta,
  pages: DonePage[],
): Promise<void> {
  const manifest: ProjectManifest = {
    ...meta,
    updated_at: new Date().toISOString(),
    pages: pages.map((d, i) => ({
      index: i,
      name: d.name,
      source: d.input,
      result: structuredClone(d.result),
    })),
  };
  pendingProjectSaves.set(project.id, {
    projectId: project.id,
    projectName: project.name,
    manifest,
  });
  return ensureSaveLoop();
}

/** Her proje için en güncel anlık görüntüyü kuyruğa alır. Proje değişse bile
 * önceki projenin son düzenlemesi yanlış manifest üzerine yazılmaz. */
function saveProject(): Promise<void> {
  if (!state.activeProject || !state.manifestMeta) return Promise.resolve();
  return queueProjectSave(state.activeProject, state.manifestMeta, state.done);
}

/** Bekleyen ve daha önce başarısız olmuş kayıtları bir kez daha dener. */
async function flushProjectSaves(): Promise<boolean> {
  if (saveLoop) await saveLoop;
  if (failedProjectSaves.size) {
    for (const [projectId, job] of failedProjectSaves) {
      pendingProjectSaves.set(projectId, job);
    }
    await ensureSaveLoop();
  } else if (pendingProjectSaves.size) {
    await ensureSaveLoop();
  }
  return pendingProjectSaves.size === 0 && failedProjectSaves.size === 0;
}

/* -------------------------------------------------------- bölge düzenleme */

let editorOperationOwner: object | null = null;

function setEditorBusy(busy: boolean, owner: object): void {
  if (busy) {
    editorOperationOwner = owner;
    state.editorBusy = true;
  } else {
    if (editorOperationOwner !== owner) return;
    editorOperationOwner = null;
    state.editorBusy = false;
  }
  syncEditControls();
}

/** Düzenleme sonrası önbellek tazeler ve görünümü yeniden kurar. */
function afterRegionEdit(item: DonePage, pageIndex: number, projectId: string | null): void {
  item.imgVer++;
  if (!projectId || state.activeProject?.id !== projectId || state.done[pageIndex] !== item) return;
  refreshOverflowWarning();
  const thumb = els.thumbs.children[pageIndex]?.querySelector("img");
  if (thumb) {
    thumb.src = pageImageUrl(item.result.outputs.translated, item.imgVer);
  }
  if (state.selected === pageIndex) renderSelected();
}

function editorApi(): EditorApi {
  const project = state.activeProject ? { ...state.activeProject } : null;
  const meta = state.manifestMeta ? structuredClone(state.manifestMeta) : null;
  const pages = state.done;
  const pageIndex = state.selected;
  const item = pages[pageIndex] ?? null;
  const operationOwner = {};
  const contextIsActive = (): boolean =>
    !!project && state.activeProject?.id === project.id && state.done === pages && state.selected === pageIndex;
  const persistContext = (): Promise<void> =>
    project && meta ? queueProjectSave(project, meta, pages) : Promise.resolve();

  return {
    onBusyChange(busy) {
      if (busy && !contextIsActive()) return;
      setEditorBusy(busy, operationOwner);
    },
    onSelect(id) {
      if (!contextIsActive()) return;
      if (id === state.selectedRegionId) return;
      withEditorDraftGuard(() => {
        state.selectedRegionId = id;
        renderSelected();
      });
    },
    onCreateRegion(bbox) {
      if (!item || !contextIsActive()) return;
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
      void persistContext(); // autosave: yeni bölge anında kalıcı olur
    },
    async onApply(draft) {
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
      afterRegionEdit(item, pageIndex, project?.id ?? null);
      void persistContext(); // autosave: Uygula anında diske yazılır
    },
    async onDisable(region) {
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
      afterRegionEdit(item, pageIndex, project?.id ?? null);
      void persistContext();
    },
    async onDelete(region) {
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
      if (contextIsActive() && state.selectedRegionId === region.id) state.selectedRegionId = null;
      afterRegionEdit(item, pageIndex, project?.id ?? null);
      void persistContext();
    },
  };
}

/* ----------------------------------------------------------- dışa aktarma */

type ExportFormat = "png" | "svg" | "pdf";

const EXPORT_DIR_PREF_KEY = "last_export_dir";

const EXPORT_FORMAT_HINTS: Record<ExportFormat, string> = {
  png: 'Her sayfa ayrı PNG olarak proje adıyla aynı isimli klasöre kaydedilir.',
  svg: 'Her sayfa, PNG gömülü bir SVG dosyası olarak proje klasörüne kaydedilir.',
  pdf: 'Tüm sayfalar tek bir PDF dosyasında, sayfa numarası sırasına göre birleştirilir.',
};

let exportDir: string | null = null;
let exportFmt: ExportFormat = "png";
let exportBusy = false;

/** Dosya/klasör adında geçersiz karakterleri temizler. */
function sanitizeFsName(name: string): string {
  const cleaned = name
    .replace(/[\\/:*?"<>|]/g, "_")
    .trim()
    .replace(/\.+$/, "");
  return cleaned || "Cikti";
}

/** İki yol parçasını platformdan bağımsız "/" ile birleştirir. */
function joinPath(dir: string, name: string): string {
  return `${dir.replace(/[\\/]+$/, "")}/${name}`;
}

function setExportFormat(fmt: ExportFormat): void {
  exportFmt = fmt;
  for (const btn of els.exportFormatGroup.querySelectorAll<HTMLButtonElement>("button.seg")) {
    const active = btn.dataset.fmt === fmt;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-checked", String(active));
    btn.tabIndex = active ? 0 : -1;
  }
  els.exportFormatHint.removeAttribute("role");
  els.exportFormatHint.textContent = EXPORT_FORMAT_HINTS[fmt];
}

function showExportError(message: string): void {
  els.exportFormatHint.textContent = message;
  els.exportFormatHint.setAttribute("role", "alert");
  showBanner(message, "error");
}

function updateExportUi(): void {
  els.exportFolderText.textContent = exportDir ?? "Seçilmedi";
  els.exportConfirm.disabled = !exportDir || exportBusy;
  els.exportFolderBtn.disabled = exportBusy;
  els.exportCancel.disabled = exportBusy;
  els.exportFormatGroup.setAttribute("aria-disabled", String(exportBusy));
  for (const btn of els.exportFormatGroup.querySelectorAll<HTMLButtonElement>("button.seg")) {
    btn.disabled = exportBusy;
  }
  els.exportModal.setAttribute("aria-busy", String(exportBusy));
  els.exportStatus.textContent = exportBusy ? "Dosyalar dışa aktarılıyor. Lütfen bekleyin…" : "";
  els.exportStatus.classList.toggle("hidden", !exportBusy);
  if (exportBusy && !els.exportModal.classList.contains("hidden")) {
    els.exportModal.focus({ preventScroll: true });
  }
}

async function openExportModal(): Promise<void> {
  if (state.editMode || state.editorBusy) {
    showBanner("Dışa aktarmadan önce bölge düzenlemesini bitirin.", "warn");
    return;
  }
  if (!state.done.length) return;
  setExportFormat(exportFmt);
  updateExportUi();
  showModal(els.exportModal, els.exportFolderBtn);
  // Son kullanılan klasörü tercihlerden yükle (bu oturumda seçim yapılmadıysa).
  if (!exportDir) {
    try {
      const v = (await invoke("load_pref", { key: EXPORT_DIR_PREF_KEY })) as unknown;
      // Kullanıcı tercih okunurken yeni bir klasör seçtiyse yeni seçimi ezme.
      if (!exportDir && typeof v === "string" && v) {
        exportDir = v;
        updateExportUi();
      }
    } catch {
      /* tercih okunamadı; kullanıcı elle seçer */
    }
  }
}

function closeExportModal(): void {
  if (exportBusy) return; // aktarım sürerken kapatılamaz
  hideModal(els.exportModal);
}

async function pickExportFolder(): Promise<void> {
  if (exportBusy) return;
  try {
    const dir = await open({ directory: true, multiple: false, title: "Dışa aktarma klasörünü seçin" });
    if (!dir) return;
    exportDir = Array.isArray(dir) ? dir[0] : dir;
    // Bir dahaki sefere hatırlansın.
    invoke("save_pref", { key: EXPORT_DIR_PREF_KEY, value: exportDir }).catch(() => {});
    updateExportUi();
  } catch (err) {
    showExportError(`Dışa aktarma klasörü seçilemedi: ${String(err)}`);
  }
}

async function runExport(): Promise<void> {
  if (!state.done.length || !exportDir || exportBusy) return;
  // Çevrilmiş görseli olmayan sayfalar atlanır.
  const items = state.done.filter((i) => i.result.outputs.translated);
  if (!items.length) {
    showExportError("Dışa aktarılacak çevrilmiş sayfa yok.");
    return;
  }
  const projectName = sanitizeFsName(state.activeProject?.name ?? "Cikti");

  setExportFormat(exportFmt);
  exportBusy = true;
  updateExportUi();
  els.exportConfirm.textContent = "Aktarılıyor…";
  let success: { target: string; count: number; format: ExportFormat } | null = null;
  try {
    let target: string;
    if (exportFmt === "pdf") {
      // Tüm sayfalar tek PDF'te, sayfa sırasına göre birleştirilir.
      target = joinPath(exportDir, `${projectName}.pdf`);
      await invoke("export_pdf", {
        imagePaths: items.map((i) => i.result.outputs.translated),
        outPath: target,
      });
    } else {
      // PNG/SVG: seçilen klasörün içinde proje adıyla klasör oluşturulur.
      target = joinPath(exportDir, projectName);
      await invoke("create_dir", { path: target });
      for (const item of items) {
        const src = item.result.outputs.translated as string;
        const dst = joinPath(target, `${stripExt(item.name)}.${exportFmt}`);
        if (exportFmt === "svg") {
          await invoke("write_svg_from_png", { pngPath: src, svgPath: dst });
        } else {
          await invoke("copy_file", { src, dst });
        }
      }
    }
    success = { target, count: items.length, format: exportFmt };
  } catch (err) {
    showExportError(`Dışa aktarma hatası: ${String(err)}`);
  } finally {
    exportBusy = false;
    els.exportConfirm.textContent = "Dışa Aktar";
    updateExportUi();
    if (!success) els.exportConfirm.focus();
  }
  if (success) {
    closeExportModal();
    showBanner(
      `${success.count} sayfa ${success.format.toUpperCase()} olarak dışa aktarıldı → ${success.target}`,
      "ok",
    );
  }
}

/* -------------------------------------------------------------- olaylar */

/** F11: mevcut tam ekran durumunu tersine çevirir (açtıysa kapatır). */
async function toggleFullscreen(): Promise<void> {
  const win = getCurrentWindow();
  await win.setFullscreen(!(await win.isFullscreen()));
}

async function initEvents(): Promise<void> {
  let sidecarStatusEventSeen = false;
  try {
    await listen("python-event", (ev) => {
      const msg = ev.payload as Record<string, unknown> | undefined;
      if (!msg || typeof msg !== "object") return;
      const name =
        typeof msg.event === "string" ? msg.event : typeof msg.name === "string" ? msg.name : "";
      const payload = msg.payload as ProgressPayload | undefined;

      if (name === "translate_page_progress" && payload) {
        onProgress(payload);
      } else if (name === "ready") {
        sidecarStatusEventSeen = true;
        setBadge("ok", "Çeviri altyapısı hazır");
      } else if (name === "exit") {
        sidecarStatusEventSeen = true;
        setBadge("error", "Çeviri altyapısı kapandı");
        showBanner("İşleme servisi kapandı. Yeni bir işlem başlatmadan önce uygulamayı yeniden başlatın.", "error");
      } else if (name === "error") {
        sidecarStatusEventSeen = true;
        setBadge("error", "Çeviri altyapısı hatası");
        showBanner("İşleme servisinde hata oluştu. Devam edemezseniz uygulamayı yeniden başlatın.", "error");
      }
    });
  } catch {
    // Tarayıcı önizlemesinde Tauri olay köprüsü yoktur; yerel UI olayları yine kurulur.
  }

  // Sidecar, WebView yüklenmeden önce hazır olabilir; bu durumda ilk "ready"
  // olayı dinleyici kurulmadan kaçar. Gerçek bir ping ile rozet durumunu
  // eşitle; isteği beklemeyerek bozuk bir servisin arayüz açılışını durdurma.
  void request("ping")
    .then(() => {
      if (!sidecarStatusEventSeen) setBadge("ok", "Çeviri altyapısı hazır");
    })
    .catch(() => {
      if (!sidecarStatusEventSeen) setBadge("error", "Çeviri altyapısı yanıt vermiyor");
    });

  els.bannerClose.addEventListener("click", hideBanner);
  els.btnPickFile.addEventListener("click", () => void pickFile());
  els.btnPickFolder.addEventListener("click", () => void pickFolder());

  els.tabMangas.addEventListener("click", () => setTab("mangas"));
  els.tabAnime.addEventListener("click", () => setTab("anime"));
  const tabs = [els.tabMangas, els.tabAnime];
  for (const tab of tabs) {
    tab.addEventListener("keydown", (ev) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(ev.key)) return;
      ev.preventDefault();
      const current = tabs.indexOf(tab);
      const next = ev.key === "Home"
        ? 0
        : ev.key === "End"
          ? tabs.length - 1
          : (current + (ev.key === "ArrowLeft" ? -1 : 1) + tabs.length) % tabs.length;
      const nextTab = tabs[next];
      setTab(nextTab === els.tabAnime ? "anime" : "mangas");
      nextTab.focus();
    });
  }
  els.btnBackMangas.addEventListener("click", () => {
    withEditorDraftGuard(() => {
      state.editMode = false;
      state.selectedRegionId = null;
      syncEditControls();
      setTab("mangas");
    });
  });
  els.btnNewProject.addEventListener("click", openNewProjectModal);
  els.btnNewProjectEmpty.addEventListener("click", openNewProjectModal);
  els.modalClose.addEventListener("click", closeNewProjectModal);
  els.modalBackdrop.addEventListener("click", closeNewProjectModal);

  const retryProjects = (): void => {
    if (state.projectsError && !state.projectsLoading) void refreshProjects();
  };
  els.projectsEmpty.addEventListener("click", retryProjects);
  els.projectsEmpty.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      retryProjects();
    }
  });

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
    trapModalFocus(ev);
    if (ev.key !== "Escape") return;
    let modalClosed = false;
    if (!els.confirmModal.classList.contains("hidden")) {
      closeConfirm();
      modalClosed = true;
    } else if (!els.exportModal.classList.contains("hidden")) {
      closeExportModal();
      modalClosed = true;
    } else if (!els.newProjectModal.classList.contains("hidden") && !state.running) {
      closeNewProjectModal();
      modalClosed = true;
    }
    if (modalClosed) {
      // Aynı Escape olayının editör taslağını da geri almasını engelle.
      ev.preventDefault();
      ev.stopImmediatePropagation();
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

  window.addEventListener("beforeunload", (ev) => {
    const workInProgress =
      state.running ||
      state.starting ||
      state.sourceBusy ||
      state.editorBusy ||
      state.saveBusy ||
      exportBusy ||
      pendingProjectSaves.size > 0 ||
      failedProjectSaves.size > 0;
    if (!hasPendingEditorDraft() && !workInProgress) return;
    ev.preventDefault();
    ev.returnValue = "";
  });

  for (const btn of els.modeGroup.querySelectorAll<HTMLButtonElement>("button.seg")) {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.mode as Mode | undefined;
      if (mode) setMode(mode);
    });
  }
  enableRadioGroupKeyboard(els.modeGroup);
  enableRadioGroupKeyboard(els.viewModeGroup);
  enableRadioGroupKeyboard(els.exportFormatGroup);

  els.providerSelect.addEventListener("change", () => {
    state.provider = els.providerSelect.value;
    updateProviderHint();
    syncConfigControls();
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
        b.tabIndex = active ? 0 : -1;
      }
      renderSelected();
    });
  }

  els.btnOverflow.addEventListener("click", () => {
    if (state.editMode) return;
    state.showBoxes = !state.showBoxes;
    syncEditControls();
    renderSelected();
  });

  els.btnEdit.addEventListener("click", () => {
    if (state.editMode) {
      withEditorDraftGuard(() => setEditMode(false));
    } else {
      setEditMode(true);
    }
  });

  els.btnStart.addEventListener("click", () => void run());
  els.btnCancel.addEventListener("click", () => {
    state.cancelRequested = true;
    els.btnCancel.disabled = true;
    els.btnCancel.textContent = "İptal ediliyor…";
    els.stageDetail.textContent = "Geçerli sayfa tamamlandıktan sonra işlem duracak.";
  });
  els.btnExport.addEventListener("click", () => void openExportModal());

  els.exportFolderBtn.addEventListener("click", () => void pickExportFolder());
  els.exportConfirm.addEventListener("click", () => void runExport());
  els.exportCancel.addEventListener("click", closeExportModal);
  els.exportBackdrop.addEventListener("click", closeExportModal);
  for (const btn of els.exportFormatGroup.querySelectorAll<HTMLButtonElement>("button.seg")) {
    btn.addEventListener("click", () => {
      const fmt = btn.dataset.fmt as ExportFormat | undefined;
      if (fmt && !exportBusy) setExportFormat(fmt);
    });
  }
}

/* ----------------------------------------------------------------- kur */

async function main(): Promise<void> {
  setMode("auto");
  syncEditControls();
  await initEvents();
  setTab("mangas");
  renderSavedIndicator();
  setCardSize(state.cardSize); // CSS varsayılanını JS durumuyla hizala
  // Proje kütüphanesi Python servisinden bağımsızdır; sağlayıcı sorgusu yavaşlasa
  // bile ana ekranı bekletmemek için başlangıç yüklerini paralel yürüt.
  await Promise.all([loadProviders(), loadCardSizePref(), refreshProjects()]);
}

void main();
