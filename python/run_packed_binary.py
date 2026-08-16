import json
import shutil
import subprocess
import sys

BIN = "/home/yusufia/Projeler/PS-Editor/src-tauri/binaries/python-sidecar/python-sidecar"
SRC = "/home/yusufia/Projeler/PS-Editor/python/test_data/regression/manga_cover_700x1080.png"
P0 = "/tmp/opencode/gui_proof/p0"

shutil.rmtree("/tmp/opencode/gui_proof", ignore_errors=True)
import os
os.makedirs(P0, exist_ok=True)
shutil.copy(SRC, os.path.join(P0, "original.png"))

payload = {
    "image": os.path.join(P0, "original.png"),
    "target_lang": "tr",
    "mode": "mock",
    "provider": "mock",
    "job_id": "gui-proof-cover",
    "settings": {
        "out_dir": P0,
        "save_intermediate": True,
        "inpaint_method": "lama",
        "inpaint_ring_width": 0,
        "ocr_text_free_min_conf": 0.8,
        "ocr_conf": 0.3,
        "no_refine_remnants": False,
    },
}
line = json.dumps({"id": 1, "cmd": "translate_page", "payload": payload}, ensure_ascii=False) + "\n"

env = dict(os.environ)
env["PS_LAMA_MODEL"] = "/home/yusufia/.cache/torch/hub/checkpoints/big-lama.pt"
proc = subprocess.run([BIN], input=line, capture_output=True, text=True,
                      timeout=900, env=env)
events = [l for l in proc.stdout.splitlines() if l.strip()]
print("--- son satirlar (stdout) ---")
for l in events[-8:]:
    print(l[:300])
if proc.stderr.strip():
    print("--- stderr (son 30 satir) ---")
    for l in proc.stderr.splitlines()[-30:]:
        print(l[:300])
js = None
for l in events:
    try:
        o = json.loads(l)
    except json.JSONDecodeError:
        continue
    if o.get("id") == 1:
        js = o
if js is None:
    print("YANIT BULUNAMADI")
    sys.exit(1)
print("ok =", js.get("ok"))
print("hata =", str(js.get("error"))[:400])
if js.get("ok"):
    res = js["result"]
    print("\n== regions (editorde gorunen) ==")
    for r in res["regions"]:
        print(f"  id={r['id']} idx={r['index']} {r['label_name']:<12} "
              f"bbox={r['bbox']} disabled={r.get('disabled', False)} "
              f"orig={r['original'][:12]!r}")
    print("== outputs ==")
    for k, v in res["outputs"].items():
        print(f"  {k}: {v}")
    print("== mode ==")
    print(" ", res["mode_decision"]["decision"], res["mode_decision"]["reason"])