import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import "./styles.css";

const statusEl = document.getElementById("sidecar-status") as HTMLDivElement;
const outputEl = document.getElementById("output") as HTMLPreElement;
const helloBtn = document.getElementById("btn-hello") as HTMLButtonElement;
const cudaBtn = document.getElementById("btn-cuda") as HTMLButtonElement;

function setStatus(kind: "ok" | "error" | "unknown", text: string) {
  statusEl.className = `badge ${kind}`;
  statusEl.textContent = text;
}

function show(ok: boolean, text: string) {
  outputEl.textContent = text;
  outputEl.className = `output ${ok ? "ok" : "err"}`;
}

async function request(cmd: string, payload?: unknown): Promise<unknown> {
  return invoke("python_request", { cmd, payload });
}

async function sendHello() {
  helloBtn.disabled = true;
  try {
    const res = (await request("hello", { name: "Dünya" })) as { greeting: string; time: string };
    show(true, `Python cevabı:\n${JSON.stringify(res, null, 2)}`);
  } catch (e) {
    show(false, `Hata: ${e}`);
  } finally {
    helloBtn.disabled = false;
  }
}

async function checkCuda() {
  cudaBtn.disabled = true;
  try {
    const res = await request("check_cuda", {});
    show(true, `GPU / CUDA raporu:\n${JSON.stringify(res, null, 2)}`);
  } catch (e) {
    show(false, `Hata: ${e}`);
  } finally {
    cudaBtn.disabled = false;
  }
}

async function ping() {
  try {
    const res = (await request("ping", {})) as { pong: boolean };
    if (res.pong) setStatus("ok", "Python servisi bağlı");
    else setStatus("error", "Python servisi yanıt verdi ama pong yok");
  } catch (e) {
    setStatus("error", `Python servisine ulaşılamıyor: ${e}`);
  }
}

helloBtn.addEventListener("click", sendHello);
cudaBtn.addEventListener("click", checkCuda);

listen("python-event", (ev) => {
  const p = ev.payload as { name?: string };
  if (p?.name === "ready") setStatus("ok", "Python servisi hazır");
  if (p?.name === "exit") setStatus("error", "Python servisi kapandı");
});

void ping();
