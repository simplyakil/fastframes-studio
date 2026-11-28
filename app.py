"""FastFrames Studio: a local web app for Apple's FastFrames-S (MLX port) on Apple silicon.

Serves a single-page UI on http://127.0.0.1:7870, runs one video job at a time in a worker
thread, and offers a prompt generator backed by a small local LLM (any GGUF run with llama.cpp's
llama-server, started on demand) or any OpenAI-compatible server (MLX-Serve, LM Studio, Ollama).

Machine-specific settings live in config.json next to this file (not in git); see
config.example.json. Run:  python app.py   (with an environment from requirements.txt)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import prompt_models as PM

HOME = Path.home()
APP_DIR = Path(__file__).absolute().parent
CONFIG = Path(os.environ.get("FFS_CONFIG", APP_DIR / "config.json"))
HOST, PORT = "127.0.0.1", int(os.environ.get("FFS_PORT", "7870"))
MODEL_REPO = "mlx-community/FastFrames-S-bf16-mlx"
LLM_PORT = int(os.environ.get("FFS_LLM_PORT", "8091"))

# Seconds per latent frame for a first 5 s clip on an M1 Max; used for the time estimate until
# this machine has timed a full clip of its own.
DEFAULT_FRAME_S = [6.21, 2.95, 1.97, 2.52, 2.79, 2.92, 3.3, 3.33, 3.68, 4.13, 4.43, 4.42, 5.29, 5.2,
                   5.31, 5.25, 5.59, 5.61, 6.05, 5.99, 6.26]

NO_THINK = ["--reasoning", "off"]
DEFAULT_CONFIG = {
    "model_dir": "",                      # FastFrames-S-bf16-mlx folder; "" = download to the HF cache
    "out_dir": str(HOME / "Movies/FastFrames Studio"),
    "llama_server": "",                   # "" = find llama-server on PATH or in ~/.unsloth/llama.cpp
    "model_search_dirs": [],              # [] = prompt_models.DEFAULT_SEARCH_DIRS
    "extra_servers": [],                  # [{"name", "url"}] besides MLX-Serve, LM Studio and Ollama
    "llm_choice": "",                     # id of the chosen prompt model; "" = smallest working one
    "verified": {},                       # test-load results per model file
    "keep_llm_loaded": True,
    "frame_profile": DEFAULT_FRAME_S,
}


def load_config():
    c = dict(DEFAULT_CONFIG)
    if CONFIG.exists():
        try:
            c.update(json.loads(CONFIG.read_text()))
        except Exception as e:
            print(f"config.json ignored: {e}", flush=True)
    return c


def save_config(c):
    CONFIG.write_text(json.dumps(c, indent=1))


cfg = load_config()
OUT_DIR = Path(os.path.expanduser(cfg["out_dir"]))
OUT_DIR.mkdir(parents=True, exist_ok=True)


def resolve_model_dir():
    d = cfg.get("model_dir")
    if d and (Path(os.path.expanduser(d)) / "run.py").exists():
        return Path(os.path.expanduser(d))
    from huggingface_hub import snapshot_download
    print(f"Downloading {MODEL_REPO} (about 15 GB) into the Hugging Face cache...", flush=True)
    return Path(snapshot_download(MODEL_REPO, allow_patterns=["*.py", "*.safetensors", "*.txt", "LICENSE*"]))


MODEL_DIR = None  # resolved on first video job (may download)


def find_llama_server():
    for cand in (cfg.get("llama_server"), shutil.which("llama-server"),
                 str(HOME / ".unsloth/llama.cpp/build/bin/llama-server"), "/opt/homebrew/bin/llama-server"):
        if cand and Path(os.path.expanduser(cand)).exists():
            return Path(os.path.expanduser(cand))
    return None




# ------------------------------------------------------------------------------ system stats
def _sysctl(name):
    return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True).stdout.strip()


MEM_TOTAL = int(_sysctl("hw.memsize") or 0)
CHIP = _sysctl("machdep.cpu.brand_string")


def stats():
    gpu = None
    try:
        out = subprocess.run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"],
                             capture_output=True, text=True, timeout=2).stdout
        m = re.search(r'"Device Utilization %"=(\d+)', out)
        gpu = int(m.group(1)) if m else None
    except Exception:
        pass
    used = None
    try:
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2).stdout
        page = int(re.search(r"page size of (\d+)", vm).group(1))
        def pages(label):
            m = re.search(rf"{label}:\s+(\d+)", vm)
            return int(m.group(1)) if m else 0
        used = (pages("Pages active") + pages("Pages wired down")
                + pages("Pages occupied by compressor")) * page
    except Exception:
        pass
    return {"gpu": gpu, "mem_used": used, "mem_total": MEM_TOTAL, "chip": CHIP}


# ------------------------------------------------------------------------------ video jobs
class Cancelled(Exception):
    pass


state_lock = threading.Lock()
job = {"id": 0, "state": "idle"}          # idle | running | done | error | cancelled
cancel_flag = threading.Event()
mlx_ready = {"loaded": False}


def eta_seconds(latent_frames, text_cached=False):
    prof = cfg.get("frame_profile") or DEFAULT_FRAME_S
    per = [prof[i] if i < len(prof) else prof[-1] for i in range(latent_frames)]
    return (0 if text_cached else 5) + sum(per) + 2


def _set(**kw):
    with state_lock:
        job.update(kw)


def run_job(params):
    global MODEL_DIR
    jid = params["id"]
    prompt, seed, frames = params["prompt"], params["seed"], params["frames"]
    decoder = params.get("decoder", "fp32")
    stem = time.strftime("%Y%m%d-%H%M%S") + f"_s{seed}"
    out_mp4 = OUT_DIR / f"{stem}.mp4"
    try:
        if MODEL_DIR is None:
            _set(stage="Locating FastFrames-S (first run downloads ~15 GB)", progress=0.0)
            MODEL_DIR = resolve_model_dir()
            sys.path.insert(0, str(MODEL_DIR))
        import numpy as np
        import mlx.core as mx
        import run as ffrun  # the MLX port's run.py: weights, text encoder, model load, mp4 writer
        wd = ffrun.resolve_weights(str(MODEL_DIR))
        _set(stage="Encoding prompt", progress=0.0)
        t0 = time.perf_counter()
        embs, info = ffrun.encode_prompts([prompt], wd)
        text_s = time.perf_counter() - t0
        if cancel_flag.is_set():
            raise Cancelled()
        _set(stage="Loading model", progress=0.02)
        mx.reset_peak_memory()
        ff = ffrun.load_models(wd, False, frames, decoder, {})
        mlx_ready["loaded"] = True

        mx.reset_peak_memory()
        mx.random.seed(seed)
        noise = mx.random.normal((frames, 16, 60, 104)).astype(mx.bfloat16)
        mx.eval(noise)
        trans = lambda step, frame: mx.random.normal((16, 60, 104)).astype(mx.bfloat16)  # noqa: E731
        stamps = []
        t_roll = time.perf_counter()

        def on_frame(f):
            stamps.append(time.perf_counter())
            done = len(stamps)
            per = np.diff([t_roll] + stamps)
            _set(stage=f"Generating frame {done} of {frames}", progress=0.04 + 0.9 * done / frames,
                 frame_times=[round(float(x), 2) for x in per])
            if cancel_flag.is_set():
                raise Cancelled()

        _set(stage=f"Generating frame 1 of {frames}", progress=0.04)
        feats, mods = ff.rollout(embs[prompt], noise, trans, on_frame=on_frame)
        rollout_s = time.perf_counter() - t_roll
        peak = mx.get_peak_memory()

        _set(stage="Decoding pixels", progress=0.95)
        t0 = time.perf_counter()
        px = ff.decode(feats, mods, num_pixel_frames=4 * frames - 3)
        u8 = ((mx.clip(px.astype(mx.float32), -1, 1) + 1) * 127.5).round().astype(mx.uint8)
        mx.eval(u8)
        decode_s = time.perf_counter() - t0
        _set(stage="Writing MP4", progress=0.98)
        ffrun.write_mp4(np.array(u8), out_mp4)
        del ff, feats, mods, px, u8
        mx.clear_cache()

        per = [round(float(x), 2) for x in np.diff([t_roll] + stamps)]
        if frames == len(DEFAULT_FRAME_S) or len(per) >= len(cfg.get("frame_profile") or []):
            cfg["frame_profile"] = per
            save_config(cfg)
        rec = {"file": out_mp4.name, "prompt": prompt, "seed": seed, "latent_frames": frames,
               "video_frames": 4 * frames - 3, "seconds": round((4 * frames - 3) / 16, 2),
               "decoder": decoder, "text_s": round(text_s, 2), "text_cached": info.get("text_cached", 0) > 0,
               "rollout_s": round(rollout_s, 2), "decode_s": round(decode_s, 2),
               "total_s": round(text_s + rollout_s + decode_s, 2), "peak_gb": round(peak / 2**30, 2),
               "frame_times": per, "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        out_mp4.with_suffix(".json").write_text(json.dumps(rec, indent=1))
        _set(state="done", stage="Done", progress=1.0, result=rec)
    except Cancelled:
        import mlx.core as mx2
        mx2.clear_cache()
        _set(state="cancelled", stage="Cancelled", progress=0.0)
    except Exception as e:
        traceback.print_exc()
        _set(state="error", stage="Error", error=f"{type(e).__name__}: {e}")
    finally:
        cancel_flag.clear()
        print(f"[job {jid}] {job.get('state')}", flush=True)


def start_job(body):
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return {"error": "Enter a prompt first."}, 400
    frames = max(2, min(21, int(body.get("frames", 21))))
    seed = int(body.get("seed", 0)) & 0x7FFFFFFF
    decoder = body.get("decoder") if body.get("decoder") in ("fp32", "bf16") else "fp32"
    with state_lock:
        if job.get("state") == "running":
            return {"error": "A video is already generating."}, 409
        jid = job["id"] + 1
        job.clear()
        job.update(id=jid, state="running", stage="Starting", progress=0.0, prompt=prompt, seed=seed,
                   frames=frames, started=time.time(), eta=eta_seconds(frames), frame_times=[])
    cancel_flag.clear()
    params = {"id": jid, "prompt": prompt, "seed": seed, "frames": frames, "decoder": decoder}
    threading.Thread(target=run_job, args=(params,), daemon=True).start()
    return {"id": jid}, 200


def history():
    items = []
    for j in sorted(OUT_DIR.glob("*.json"), reverse=True):
        try:
            rec = json.loads(j.read_text())
            if (OUT_DIR / rec["file"]).exists():
                items.append(rec)
        except Exception:
            continue
    return items


# ------------------------------------------------------------------------------ prompt LLM
llm = {"proc": None, "state": "stopped", "name": None, "id": None}
llm_lock = threading.Lock()

RULES = ("ONE vivid paragraph of 70 to 120 words in plain English. One continuous shot, concrete visual "
         "details, no dialogue, no on-screen text, no lists, no quotation marks, no headings, no preamble, and do "
         "not mention resolution, frame rate, clip length or the word prompt. Output only the paragraph.")

ENHANCE_SYSTEM = (
    "You write prompts for a text-to-video model that makes short cinematic clips. Rewrite the user's idea as "
    "a richer version describing, in this order: the main subject and its look (colours, clothing, materials), "
    "the action and motion, the setting and background, the lighting and time of day, the camera (shot size, "
    "angle, movement), and the visual style. Write " + RULES)

WRITE_SYSTEM = (
    "You write prompts for a text-to-video model that makes short cinematic clips. The user describes, in "
    "their own words, the video they want. It may be rough notes, a full scene, a question, or a request for "
    "ideas. Turn it into the single best prompt for what they asked: keep every element they mention, then add "
    "concrete detail for the subject and its look, the action and motion, the setting, the lighting and time of "
    "day, the camera (shot size, angle, movement) and the visual style. If they ask a question or want ideas, "
    "answer by writing one prompt that fits. Write " + RULES)

REFINE_SYSTEM = (
    "You edit prompts for a text-to-video model. You get the current prompt and a change the user wants. Apply "
    "the change, keep everything else that still fits, and return the complete revised prompt as " + RULES)

IDEA_SYSTEM = (
    "You invent prompts for a text-to-video model that makes short cinematic clips. Invent one original, "
    "visually striking scene covering subject and look, action and motion, setting, lighting, camera shot and "
    "movement, and visual style. Write " + RULES)


def _up(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


# ---- catalog: every prompt model on this Mac that can actually run ----
catalog = {"local": [], "servers": [], "scanned": 0, "scanning": False}
verify = {"current": None, "queue": 0, "proc": None}


def kill_stale_servers():
    """Stop llama-server processes this app left on its own ports (e.g. after a crash)."""
    for port in (LLM_PORT, LLM_PORT + 1):
        out = subprocess.run(["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
        for pid in out.split():
            cmd = subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True, text=True).stdout
            if "llama-server" in cmd:
                subprocess.run(["kill", pid])


def _vkey(m):
    try:
        st = Path(m["path"]).stat()
        return f"{Path(m['path']).resolve()}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return None


def rescan():
    if catalog["scanning"]:
        return
    catalog["scanning"] = True
    try:
        server = find_llama_server()
        archs = PM.supported_archs(server)
        dirs = cfg.get("model_search_dirs") or PM.DEFAULT_SEARCH_DIRS
        failed = {k.split("|")[0] for k, v in (cfg.get("verified") or {}).items() if not v.get("ok")}
        catalog["local"] = PM.scan_gguf(dirs, archs=archs, max_bytes=max(4e9, MEM_TOTAL - 24 * 2**30),
                                        failed=failed) if server else []
        catalog["servers"] = PM.probe_servers(PM.DEFAULT_SERVERS + (cfg.get("extra_servers") or []))
        catalog["scanned"] = time.time()
    finally:
        catalog["scanning"] = False


def model_list():
    ver = cfg.get("verified") or {}
    out = []
    for m in catalog["local"]:
        v = ver.get(_vkey(m) or "", {})
        status = "ok" if v.get("ok") else "failed" if v else "checking"
        if status == "failed":
            continue
        out.append(dict(m, status=status, tps=v.get("tps"), load_s=v.get("load_s")))
    def family(name):   # "d1-2B-Q8_0" / "d1-2b-mlx" -> "d1-2b"
        return re.sub(r"([-_.@](q\d.*|i\d-.*|mlx.*|gguf|\d+bit.*|bf16|f16|fp8))+$", "", name.lower())
    failed = {family(v.get("name", "")) for v in ver.values() if not v.get("ok") and v.get("name")}
    for m in catalog["servers"]:
        v = ver.get(m["id"], {})
        if (v and not v.get("ok")) or family(m["name"]) in failed:
            continue
        out.append(dict(m, status="ok" if v.get("ok") else "server", tps=v.get("tps")))
    return out


def find_model(mid):
    for m in catalog["local"] + catalog["servers"]:
        if m["id"] == mid:
            return m
    return None


def current_model():
    lst = [m for m in model_list() if m["status"] in ("ok", "server")]
    m = find_model(cfg.get("llm_choice") or "")
    if m and any(x["id"] == m["id"] for x in lst):
        return m
    fast = [x for x in lst if x["kind"] == "gguf"]        # default: smallest model that passed its test
    return fast[0] if fast else (lst[0] if lst else None)


def _start_llama(m, port, log_name):
    server = find_llama_server()
    log = open(OUT_DIR / log_name, "w")
    return subprocess.Popen([str(server), "-m", m["path"], "--jinja", "-ngl", "99", "-c", "4096",
                             *PM.llama_args_for(m["name"], m.get("arch", "")),
                             "--host", "127.0.0.1", "--port", str(port)], stdout=log, stderr=subprocess.STDOUT)


def _post(url, body, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def verify_loop():
    """Test-load each local model once (smallest first) and keep only the ones that answer."""
    TEST_PORT = LLM_PORT + 1
    while True:
        time.sleep(3)
        todo = [m for m in catalog["local"] if _vkey(m) and _vkey(m) not in (cfg.get("verified") or {})]
        verify["queue"] = len(todo)
        if not todo or job.get("state") == "running" or llm["state"] == "loading":
            continue
        used = stats().get("mem_used") or 0
        fits = [x for x in todo if x["size"] + 6 * 2**30 < MEM_TOTAL - used]
        if not fits:                      # not enough free memory right now; try again later
            time.sleep(30)
            continue
        m = fits[0]
        verify["current"] = m["name"]
        proc, res = None, {"ok": False}
        try:
            t0 = time.time()
            proc = verify["proc"] = _start_llama(m, TEST_PORT, ".model-test.log")
            while not _up(TEST_PORT):
                if proc.poll() is not None or time.time() - t0 > 240:
                    raise RuntimeError("did not load")
                if job.get("state") == "running":      # make room for a video job; retry later
                    raise InterruptedError()
                time.sleep(0.5)
            load_s = time.time() - t0
            t1 = time.time()
            r = _post(f"http://127.0.0.1:{TEST_PORT}/v1/chat/completions",
                      {"max_tokens": 60, "temperature": 0.2, "messages": [
                          {"role": "user", "content": "Describe a sunset over the sea in one sentence."}]}, 120)
            text = clean_prompt(r["choices"][0]["message"].get("content") or "")
            n = r.get("usage", {}).get("completion_tokens", 0)
            res = {"ok": len(text.split()) >= 4, "load_s": round(load_s, 1),
                   "tps": round(n / max(0.01, time.time() - t1), 1), "name": m["name"]}
        except InterruptedError:
            res = None
        except Exception as e:
            res = {"ok": False, "err": str(e)[:200], "name": m["name"]}
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            verify["current"] = None
        if res is not None:
            cfg.setdefault("verified", {})[_vkey(m)] = res
            save_config(cfg)
            print(f"[models] {m['name']}: {'ok' if res['ok'] else 'FAILED'} {res}", flush=True)


def ensure_llm(m):
    with llm_lock:
        if llm.get("id") == m["id"] and _up(LLM_PORT):
            llm["state"] = "ready"
            return
    stop_llm()
    with llm_lock:
        llm["proc"] = _start_llama(m, LLM_PORT, ".prompt-model.log")
        llm["id"], llm["name"], llm["state"] = m["id"], m["name"], "loading"
    t0 = time.time()
    while time.time() - t0 < 240:
        if _up(LLM_PORT):
            llm["state"] = "ready"
            return
        if llm["proc"].poll() is not None:
            llm["state"] = "stopped"
            cfg.setdefault("verified", {})[_vkey(m)] = {"ok": False, "err": "exited while loading"}
            save_config(cfg)
            raise RuntimeError(f"{m['name']} failed to load and was removed from the list.")
        time.sleep(0.5)
    raise RuntimeError("The prompt model did not become ready in 4 minutes.")


def stop_llm():
    with llm_lock:
        p = llm["proc"]
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        llm.update(proc=None, state="stopped", id=None, name=None)


def clean_prompt(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"^\s*(\*\*)?\s*(video\s+)?prompt\s*:?\s*(\*\*)?\s*:?", "", text, flags=re.I)
    text = text.replace("**", "").strip().strip('"“”').strip()
    return re.sub(r"\s+", " ", text)


def chat(system, user, temperature=0.8):
    t0 = time.time()
    while not catalog["scanned"] and time.time() - t0 < 30:   # first scan still running after startup
        if not catalog["scanning"]:
            rescan()
        time.sleep(0.2)
    m = current_model()
    if not m:
        raise RuntimeError("No working prompt model was found. Put a GGUF chat model in one of the search "
                           "folders (Settings), or start MLX-Serve, LM Studio or Ollama.")
    if m["kind"] == "server":
        base, model = m["url"], m["model"]
    else:
        ensure_llm(m)
        base, model = f"http://127.0.0.1:{LLM_PORT}/v1", m["name"]
    body = {"model": model, "temperature": temperature, "max_tokens": 1200,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    try:
        data = _post(base + "/chat/completions", body)
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach {m['name']} at {base}: {e}")
    text = clean_prompt(data["choices"][0]["message"].get("content") or "")
    if not text:
        raise RuntimeError(f"{m['name']} returned an empty answer; try another model.")
    if m["kind"] == "server":
        cfg.setdefault("verified", {})[m["id"]] = {"ok": True}
    elif not cfg.get("keep_llm_loaded", True):
        stop_llm()
    return text


# ------------------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "FastFramesStudio/1.0"

    def log_message(self, fmt, *args):
        if "/api/status" not in (args[0] if args else "") and "/api/stats" not in (args[0] if args else ""):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("content-length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _file(self, path, ctype):
        size = path.stat().st_size
        rng = self.headers.get("range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else max(0, size - int(b))
            end = int(b) if (a and b) else size - 1
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("content-range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("content-type", ctype)
        if ctype.startswith("text/html"):
            self.send_header("cache-control", "no-store")
        self.send_header("accept-ranges", "bytes")
        self.send_header("content-length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            return self._file(APP_DIR / "index.html", "text/html; charset=utf-8")
        if p == "/api/status":
            with state_lock:
                return self._json(dict(job, now=time.time()))
        if p == "/api/stats":
            s = stats()
            cm = current_model()
            s["build"] = int(max((APP_DIR / f).stat().st_mtime for f in ("index.html", "app.py", "prompt_models.py")))
            s.update(llm=llm["state"] if llm["state"] != "ready" or _up(LLM_PORT) else "stopped",
                     llm_name=(cm or {}).get("name"), model_loaded=mlx_ready["loaded"],
                     verifying=verify["current"], verify_queue=verify["queue"])
            return self._json(s)
        if p == "/api/models":
            t0 = time.time()
            while catalog["scanning"] and time.time() - t0 < 30:   # first scan after startup still running
                time.sleep(0.2)
            if time.time() - catalog["scanned"] > 60:
                rescan()
            cm = current_model()
            return self._json({"models": model_list(), "current": (cm or {}).get("id"),
                               "verifying": verify["current"], "queue": verify["queue"],
                               "llama_server": str(find_llama_server() or "")})
        if p == "/api/history":
            return self._json(history())
        if p == "/api/config":
            return self._json({k: v for k, v in cfg.items() if k not in ("frame_profile", "verified")}
                              | {"eta_full": round(eta_seconds(21)), "frame_profile": cfg.get("frame_profile"),
                                 "model_dir": str(MODEL_DIR or cfg.get("model_dir") or "(downloads on first use)"),
                                 "out_dir": str(OUT_DIR),
                                 "search_dirs": cfg.get("model_search_dirs") or PM.DEFAULT_SEARCH_DIRS})
        if p.startswith("/outputs/"):
            name = unquote(p[len("/outputs/"):])
            f = (OUT_DIR / name).resolve()
            if f.parent == OUT_DIR.resolve() and f.suffix == ".mp4" and f.exists():
                return self._file(f, "video/mp4")
        self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            body = self._body()
        except Exception:
            return self._json({"error": "Bad JSON"}, 400)
        try:
            if p == "/api/generate":
                obj, code = start_job(body)
                return self._json(obj, code)
            if p == "/api/cancel":
                if job.get("state") == "running":
                    cancel_flag.set()
                return self._json({"ok": True})
            if p == "/api/enhance":
                idea = (body.get("prompt") or "").strip()
                if not idea:
                    return self._json({"error": "Type an idea first."}, 400)
                return self._json({"prompt": chat(ENHANCE_SYSTEM, idea, 0.7)})
            if p == "/api/write":
                d = (body.get("description") or "").strip()
                if not d:
                    return self._json({"error": "Describe the video you want first."}, 400)
                extra = (body.get("details") or "").strip()
                user = d + (f"\n\nAlso include: {extra}" if extra else "")
                return self._json({"prompt": chat(WRITE_SYSTEM, user, 0.7)})
            if p == "/api/refine":
                cur, req = (body.get("prompt") or "").strip(), (body.get("request") or "").strip()
                if not cur or not req:
                    return self._json({"error": "Need a prompt and the change you want."}, 400)
                return self._json({"prompt": chat(REFINE_SYSTEM, f"Current prompt:\n{cur}\n\nChange: {req}", 0.5)})
            if p == "/api/idea":
                theme = (body.get("theme") or "").strip()
                user = f"Theme or ingredients: {theme}" if theme else "Surprise me with something visually striking."
                return self._json({"prompt": chat(IDEA_SYSTEM, user, 1.0)})
            if p == "/api/llm/stop":
                stop_llm()
                return self._json({"ok": True})
            if p == "/api/quit":
                if job.get("state") == "running":
                    return self._json({"error": "A video is generating; cancel it first."}, 409)
                self._json({"ok": True})
                def _quit():
                    stop_llm()
                    vp = verify.get("proc")
                    if vp is not None and vp.poll() is None:
                        vp.terminate()
                    os._exit(0)
                threading.Thread(target=_quit, daemon=True).start()
                return
            if p == "/api/models/select":
                if not find_model(body.get("id", "")):
                    return self._json({"error": "Unknown model."}, 400)
                cfg["llm_choice"] = body["id"]
                save_config(cfg)
                return self._json({"ok": True})
            if p == "/api/models/rescan":
                rescan()
                return self._json({"ok": True})
            if p == "/api/config":
                for k in ("keep_llm_loaded", "llama_server", "model_search_dirs", "extra_servers"):
                    if k in body:
                        cfg[k] = body[k]
                save_config(cfg)
                if "model_search_dirs" in body or "llama_server" in body or "extra_servers" in body:
                    catalog["scanned"] = 0
                return self._json({"ok": True})
            if p == "/api/reveal":
                name = body.get("file")
                target = (OUT_DIR / name) if name else OUT_DIR
                if target.resolve().parent == OUT_DIR.resolve() or target == OUT_DIR:
                    subprocess.run(["open", "-R", str(target)] if name else ["open", str(target)])
                return self._json({"ok": True})
            if p == "/api/delete":
                name = body.get("file", "")
                f = (OUT_DIR / name).resolve()
                if f.parent == OUT_DIR.resolve() and f.suffix == ".mp4":
                    for x in (f, f.with_suffix(".json")):
                        if x.exists():
                            subprocess.run(["trash", str(x)])
                return self._json({"ok": True})
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)
        self.send_error(404)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"FastFrames Studio on http://{HOST}:{PORT}", flush=True)
    kill_stale_servers()
    threading.Thread(target=rescan, daemon=True).start()
    threading.Thread(target=verify_loop, daemon=True).start()
    try:
        srv.serve_forever()
    finally:
        stop_llm()


if __name__ == "__main__":
    main()
