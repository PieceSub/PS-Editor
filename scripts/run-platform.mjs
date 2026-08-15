#!/usr/bin/env node
// Platform bağımsız npm script yönlendirici.
// Kullanım: node scripts/run-platform.mjs <script-adı> [argümanlar...]
//   <script-adı> "setup-python" veya "build-sidecar" gibi, platforma göre
//   .ps1 (Windows) veya .sh (Linux/macOS) uzantılı script çağrılır.
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptName = process.argv[2];
const args = process.argv.slice(3);

if (!scriptName) {
  console.error("Kullanım: node scripts/run-platform.mjs <script-adı> [argümanlar...]");
  process.exit(1);
}

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const isWin = process.platform === "win32";
const scriptPath = path.join(__dirname, `${scriptName}.${isWin ? "ps1" : "sh"}`);

if (!existsSync(scriptPath)) {
  console.error(`Script bulunamadı: ${scriptPath}`);
  process.exit(1);
}

let cmd;
if (isWin) {
  // PowerShell switch'lerini normalize et: --clean -> -Clean
  const psArgs = args.map((a) => (a === "--clean" ? "-Clean" : a));
  cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", scriptPath, ...psArgs];
} else {
  cmd = ["bash", scriptPath, ...args];
}

const result = spawnSync(cmd[0], cmd.slice(1), { stdio: "inherit" });
process.exit(result.status ?? 1);
