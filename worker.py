"""Generate one clip with the FastFrames MLX port in its own process (used for FastFrames-L).

app.py starts this with a JSON job description as the only argument and reads progress as JSON lines
on stdout. Running in a separate process keeps the large model's memory (28-48 GB for L in 8-bit)
out of the app server: everything is returned to macOS the moment the clip is done or cancelled.

Job keys: port_dir (folder with ffmlx/run.py), weights ("" = the port's own lookup/download),
model ("L" or "S"), q8 (bool), prompt, seed, frames, out (mp4 path), decoder ("fp32"/"bf16").
"""
import json
import sys
import time
from pathlib import Path


def emit(**kw):
    print(json.dumps(kw), flush=True)


def main():
    job = json.loads(sys.argv[1])
    sys.path.insert(0, str(Path(job["port_dir"]) / "ffmlx"))
    import numpy as np
    import mlx.core as mx
    import run  # the port's run.py: weights, text encoder, 8-bit builder, model load, noise, mp4

    frames, prompt, seed = int(job["frames"]), job["prompt"], int(job["seed"])
    rec = {"model": job["model"], "dit": "q8" if job.get("q8") else "bf16"}
    emit(stage="Locating weights")
    wd, recipe = run.open_weights(job["model"], job.get("weights") or None)

    emit(stage="Encoding prompt")
    t0 = time.perf_counter()
    embs, info = run.encode_prompts([prompt], wd, False, True, recipe.amp)
    rec["text_s"] = round(time.perf_counter() - t0, 2)
    rec["text_cached"] = info.get("text_cached", 0) > 0

    q8 = None
    if job.get("q8"):
        import quantize
        dst = run.EMB_CACHE.parent / f"dit-{recipe.name}-q8.safetensors"
        if not quantize.is_current(dst, Path(wd) / "dit.safetensors"):
            emit(stage="Building the 8-bit model (one time, a few minutes)")
        q8 = run.q8_file(wd, recipe)

    emit(stage="Loading model")
    mx.reset_peak_memory()
    ff = run.load_models(wd, False, frames, job.get("decoder", "fp32"), {}, "bf16", q8)

    noise, trans = run.make_noise(ff.recipe, seed, frames, ff.dit.dtype)
    stamps = []
    t_roll = time.perf_counter()

    def on_frame(f):
        stamps.append(time.perf_counter())
        emit(frame=len(stamps), of=frames, t=round(stamps[-1] - (stamps[-2] if len(stamps) > 1 else t_roll), 2))

    emit(stage=f"Generating frame 1 of {frames}")
    feats, mods = ff.rollout(embs[prompt], noise, trans, on_frame=on_frame)
    rec["rollout_s"] = round(time.perf_counter() - t_roll, 2)
    rec["peak_gb"] = round(mx.get_peak_memory() / 2**30, 2)
    rec["frame_times"] = [round(float(x), 2) for x in np.diff([t_roll] + stamps)]

    emit(stage="Decoding pixels")
    t0 = time.perf_counter()
    px = ff.decode(feats, mods, num_pixel_frames=4 * frames - 3)
    u8 = ((mx.clip(px.astype(mx.float32), -1, 1) + 1) * 127.5).round().astype(mx.uint8)
    mx.eval(u8)
    rec["decode_s"] = round(time.perf_counter() - t0, 2)
    emit(stage="Writing MP4")
    run.write_mp4(np.array(u8), job["out"])
    emit(done=rec)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        emit(error=str(e))
        raise
    except Exception as e:
        emit(error=f"{type(e).__name__}: {e}")
        raise
