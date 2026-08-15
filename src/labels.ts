/** translate_page_progress event name -> kullanıcı dostu Türkçe etiket. */

export interface ProgressPayload {
  job_id?: string;
  name: string;
  progress?: number;
  message?: string;
  data?: Record<string, unknown>;
}

const STAGE_LABELS: Record<string, string> = {
  started: "Sayfa hazırlanıyor",
  image_loaded: "Görsel yüklendi",
  mode_decided: "İşlem modu belirleniyor",
  ocr_started: "Metin algılanıyor",
  ocr_detect_done: "Bölgeler tespit edildi",
  ocr_models: "OCR modeli yükleniyor",
  ocr_progress: "Metin okunuyor",
  ocr_done: "OCR tamamlandı",
  inpaint_started: "Temizleniyor",
  inpaint_done: "Temizlik tamamlandı",
  translate_started: "Çevriliyor",
  translate_done: "Çeviri tamamlandı",
  typeset_started: "Yerleştiriliyor",
  done: "Tamamlandı",
  warning: "Uyarı",
  error: "Hata",
};

export function stageLabel(name: string): string {
  return STAGE_LABELS[name] ?? name;
}