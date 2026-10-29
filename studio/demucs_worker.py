"""Runs one Demucs model on one WAV file in a separate process.

Uses Demucs' model code directly (get_model + apply_model) and does all audio reading/writing with
numpy/scipy, so it doesn't depend on torchaudio's file I/O (which changed in newer torchaudio
versions and breaks `python -m demucs`).

    python -m studio.demucs_worker MODEL INPUT.wav OUTPUT_DIR [DEVICE]

Prints "PASSES n" once, then tqdm progress on stderr, then "DONE <stem names>".
"""
import os
import sys

import numpy as np
from scipy.io import wavfile


def pick_device(requested=None):
    import torch
    if requested and requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def run(model_name, inp, outdir, device=None):
    import torch
    from demucs.apply import apply_model, BagOfModels
    from demucs.pretrained import get_model

    model = get_model(model_name)
    model.eval()
    passes = len(model.models) if isinstance(model, BagOfModels) else 1
    print(f"PASSES {passes}", flush=True)

    rate, data = wavfile.read(inp)
    data = data.astype(np.float32)
    if data.ndim == 1:
        data = np.stack([data, data], 1)
    if rate != model.samplerate:
        raise SystemExit(f"expected {model.samplerate} Hz input, got {rate}")
    wav = torch.from_numpy(np.ascontiguousarray(data.T[:model.audio_channels]))
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    wav = (wav - mean) / std

    dev = pick_device(device)
    try:
        with torch.no_grad():
            sources = apply_model(model, wav[None], device=dev, shifts=1, split=True, overlap=0.25, progress=True)[0]
    except (RuntimeError, NotImplementedError) as e:
        if dev == "cpu":
            raise
        print(f"FALLBACK cpu ({e.__class__.__name__}: {str(e)[:120]})", flush=True)
        with torch.no_grad():
            sources = apply_model(model, wav[None], device="cpu", shifts=1, split=True, overlap=0.25, progress=True)[0]
    sources = sources * std + mean
    os.makedirs(outdir, exist_ok=True)
    for name, src in zip(model.sources, sources):
        wavfile.write(os.path.join(outdir, name + ".wav"), model.samplerate, src.cpu().numpy().T.astype(np.float32))
    print("DONE " + " ".join(model.sources), flush=True)


if __name__ == "__main__":
    a = sys.argv[1:]
    run(a[0], a[1], a[2], a[3] if len(a) > 3 else None)
