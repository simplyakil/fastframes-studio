"""Find language models that can write video prompts: local GGUF files (run with llama-server)
and models served by OpenAI-compatible apps that are running right now (MLX-Serve, LM Studio,
Ollama, or any URL you add).

GGUF files are identified from their header (general.architecture, general.name), so image,
video, audio, text-encoder, vision-projector and draft-model files are left out.
"""
from __future__ import annotations

import json
import os
import re
import struct
import urllib.request
from pathlib import Path

HOME = Path.home()
DEFAULT_SEARCH_DIRS = [
    "~/.lmstudio/models", "~/Models", "~/models", "~/.cache/llama.cpp",
    "~/.cache/huggingface/hub", "~/Library/Caches/llama.cpp",
]
DEFAULT_SERVERS = [
    {"name": "MLX-Serve", "url": "http://127.0.0.1:11234/v1"},
    {"name": "LM Studio", "url": "http://127.0.0.1:1234/v1", "only_mlx": True},
    {"name": "Ollama", "url": "http://127.0.0.1:11434/v1"},
]

# Architectures / name fragments that are not chat language models.
NOT_CHAT_ARCH = re.compile(r"clip|mmproj|vision|dflash|dspark|t5|umt5|bert|nomic|embed|whisper|wav|"
                           r"qwen_?image|flux|sd3|stable|unet|mmdit|wan|ltx|hunyuan_?video|vae|tts|"
                           r"encodec|snac|audio_?enc|siglip|dit", re.I)
NOT_CHAT_NAME = re.compile(r"mmproj|dspark|drafter|draft-|embed|reranker|text[_-]?encoder|vae|"
                           r"qwen-?image|flux|whisper|tts|t5xxl|umt5|clip_|siglip|imatrix|skeleton|"
                           r"(^|[-_])mtp[-_]|diar|audio|ocr|(^|[-_.])base($|[-_.@])", re.I)
SKIP_DIRS = re.compile(r"/(blobs|\.git|node_modules|text_encoders|vae|diffusion_models|loras?|"
                       r"controlnet|upscale_models|checkpoints|unet)(/|$)", re.I)

_TYPE_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


def gguf_meta(path, keys=("general.architecture", "general.name", "general.basename",
                          "general.size_label")):
    """Read a few string keys from a GGUF header without loading the file."""
    out = {}
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return None
            version, = struct.unpack("<I", f.read(4))
            if version < 2:
                return None
            _tensors, n_kv = struct.unpack("<QQ", f.read(16))

            def rstr():
                n, = struct.unpack("<Q", f.read(8))
                return f.read(n).decode("utf-8", "replace")

            def skip(t):
                if t == 8:
                    n, = struct.unpack("<Q", f.read(8)); f.seek(n, 1)
                elif t == 9:
                    et, = struct.unpack("<I", f.read(4)); n, = struct.unpack("<Q", f.read(8))
                    if et == 8:
                        for _ in range(n):
                            m, = struct.unpack("<Q", f.read(8)); f.seek(m, 1)
                    else:
                        f.seek(_TYPE_SIZES[et] * n, 1)
                else:
                    f.seek(_TYPE_SIZES[t], 1)

            for _ in range(min(n_kv, 4096)):
                k = rstr()
                t, = struct.unpack("<I", f.read(4))
                if k in keys and t == 8:
                    out[k] = rstr()
                    if len(out) == len(keys):
                        break
                else:
                    skip(t)
                if k.startswith("tokenizer."):   # general.* keys come first; stop early
                    break
    except Exception:
        return None
    return out


def _human(n):
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def tier(size):
    return "fast" if size < 4.5e9 else "balanced" if size < 12e9 else "large"


def supported_archs(server):
    """Architecture names compiled into this llama.cpp build (read from libllama / the binary)."""
    if not server:
        return None
    server = Path(server).resolve()
    libs = sorted(server.parent.glob("libllama.*dylib")) + sorted(server.parent.parent.glob("lib/libllama.*dylib"))
    target = libs[0] if libs else server
    try:
        data = target.read_bytes()
    except OSError:
        return None
    return {w.decode() for w in re.findall(rb"[a-z][a-z0-9_.\-]{1,30}", data)}


def scan_gguf(dirs, max_files=4000, archs=None, max_bytes=None, failed=()):
    seen, names, found = set(), set(), []
    for d in dirs:
        root = Path(os.path.expanduser(d))
        if not root.exists():
            continue
        count = 0
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            if SKIP_DIRS.search(dirpath + "/"):
                dirnames[:] = []
                continue
            dirnames[:] = [x for x in dirnames if not x.startswith(".") or x == ".cache"]
            for fn in filenames:
                if not fn.lower().endswith(".gguf"):
                    continue
                count += 1
                if count > max_files:
                    break
                if NOT_CHAT_NAME.search(fn):
                    continue
                m = re.search(r"-(\d{5})-of-(\d{5})\.gguf$", fn)
                if m and m.group(1) != "00001":
                    continue
                p = Path(dirpath) / fn
                try:
                    real = p.resolve()
                    size = real.stat().st_size
                except OSError:
                    continue
                try:
                    st = real.stat()
                except OSError:
                    continue
                key = (st.st_dev, st.st_ino)
                if key in seen or size < 300e6 or str(real) in failed:
                    continue
                meta = gguf_meta(real)
                if meta is None:
                    continue
                arch = meta.get("general.architecture", "")
                if not arch or NOT_CHAT_ARCH.search(arch):
                    continue
                if archs is not None and arch not in archs:
                    continue
                if m:  # split model: count every part
                    size = sum(x.stat().st_size for x in real.parent.glob(fn[:m.start()] + "-*-of-*.gguf"))
                if max_bytes and size > max_bytes:
                    continue
                seen.add(key)
                label = fn[:-5]
                if (label.lower(), size) in names:
                    continue
                names.add((label.lower(), size))
                found.append({"id": f"gguf:{real}", "kind": "gguf", "path": str(p), "name": label,
                              "arch": arch, "size": size, "size_h": _human(size), "tier": tier(size),
                              "source": _source(p)})
    found.sort(key=lambda x: x["size"])
    return found


def _source(p):
    s = str(p)
    for key, name in ((".lmstudio", "LM Studio"), ("LMStudio", "LM Studio"), ("huggingface", "Hugging Face cache"),
                      (".cache/llama.cpp", "llama.cpp cache"), ("Ollama", "Ollama files")):
        if key in s:
            return name
    return str(Path(s).parent).replace(str(HOME), "~")


def probe_servers(servers, timeout=0.6):
    out = []
    for s in servers:
        url = s["url"].rstrip("/")
        try:
            with urllib.request.urlopen(url + "/models", timeout=timeout) as r:
                data = json.loads(r.read())
        except Exception:
            continue
        for m in data.get("data", []):
            mid = m.get("id", "")
            caps = m.get("capabilities") or m.get("type") or ""
            if NOT_CHAT_NAME.search(mid) or re.search(r"image|video|music|audio|speech|tts|asr|3d|"
                                                      r"embed|sfx|sound|idol|ocr|omni", mid, re.I):
                continue
            # LM Studio also lists its GGUF files, which already appear as local models; keep its MLX ones.
            if s.get("only_mlx") and "mlx" not in mid.lower():
                continue
            if isinstance(caps, list) and caps and not any(c in caps for c in ("chat", "text", "completion")):
                continue
            out.append({"id": f"server:{url}|{mid}", "kind": "server", "url": url, "model": mid,
                        "name": mid.split("/")[-1], "source": s["name"], "tier": "server", "size_h": ""})
    return out


def llama_args_for(name, arch=""):
    """Thinking settings per model family: prompt writing needs no long reasoning."""
    n = f"{name} {arch}".lower()
    if "inkling" in n:   # its template defaults to high effort; a short capped think is fast and good
        return ["--reasoning-effort", "low", "--reasoning-budget", "200"]
    return ["--reasoning", "off"]
