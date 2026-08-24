/** DOM-bağımsız çekirdek testleri: src/editor/selftest.ts'i esbuild ile
 * paketleyip node'da çalıştırır (tarayıcı/Tauri gerekmez). */
import { build } from "esbuild";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const outdir = mkdtempSync(join(tmpdir(), "ps-editor-tests-"));
const outfile = join(outdir, "selftest.cjs");

await build({
  entryPoints: [join(root, "src/editor/selftest.ts")],
  bundle: true,
  platform: "node",
  format: "cjs",
  outfile,
  logLevel: "silent",
});

const res = spawnSync(process.execPath, [outfile], { stdio: "inherit" });
process.exit(res.status ?? 1);
