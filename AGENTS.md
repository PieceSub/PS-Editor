# PS Editor - Çalışma Kuralları

## Test kuralı (ÖNEMLİ)
Kullanıcıya her değişikliğin final halini vermeden ÖNCE önce çalıştırıp test et:
1. `npx tsc --noEmit` ile tip kontrolü yap
2. `npm run build` ile derlemeyi doğrula (dist güncel kalsın)
3. `npm run dev` (vite, port 1420) ile uygulamayı çalıştır ve hata olup olmadığını kontrol et
4. Sorun yoksa kullanıcıya ilet; kullanıcı Tauri uygulamasını kendisi yeniden başlatır

## Notlar
- Kullanıcı uygulamayı genelde derlenmiş `dist/` üzerinden çalıştırıyor; `src/` değişikliklerinden sonra `npm run build` şart.
- Arayüz dili Türkçe; kod yorumları da Türkçe.
