"""Audio I/O and a perfectly-reconstructing STFT (numpy only)."""
import os
import shutil
import subprocess
import sys
import tempfile
from math import gcd

import numpy as np
from scipy import signal
from scipy.io import wavfile

EPS = 1e-10
N_FFT = 2048
HOP = 512
_WIN = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT)).astype(np.float32)  # periodic Hann


# ----------------------------------------------------------------------------- I/O
def _int_to_float(data):
    if data.dtype == np.uint8:
        return (data.astype(np.float32) - 128.0) / 128.0
    if np.issubdtype(data.dtype, np.integer):
        return data.astype(np.float32) / float(np.iinfo(data.dtype).max + 1)
    return data.astype(np.float32)


def have_ffmpeg():
    return shutil.which("ffmpeg") is not None


def load_audio(path, sr=44100):
    """Load any audio file as float32 array shaped (channels, samples) at `sr`.

    WAV is read natively; everything else (mp3, flac, m4a, ogg...) goes through ffmpeg.
    Mono files are duplicated to stereo so every stem has the same shape.
    """
    data, rate = None, None
    if path.lower().endswith(".wav"):
        try:
            rate, raw = wavfile.read(path)
            data = _int_to_float(raw)
            data = data[None, :] if data.ndim == 1 else data.T
        except Exception:
            data = None
    if data is None:
        if not have_ffmpeg():
            raise RuntimeError(
                "Reading this file type needs ffmpeg. Install ffmpeg, or convert the song to .wav first.")
        cmd = ["ffmpeg", "-v", "error", "-i", path, "-f", "f32le", "-acodec", "pcm_f32le",
               "-ac", "2", "-ar", str(sr), "-"]
        out = subprocess.run(cmd, capture_output=True)
        if out.returncode != 0:
            raise RuntimeError("ffmpeg could not read the file: " + out.stderr.decode(errors="ignore")[-400:])
        data = np.frombuffer(out.stdout, dtype=np.float32).reshape(-1, 2).T.copy()
        rate = sr
    if data.shape[0] == 1:
        data = np.vstack([data, data])
    elif data.shape[0] > 2:
        data = data[:2]
    if rate != sr:
        g = gcd(int(rate), int(sr))
        data = signal.resample_poly(data, sr // g, rate // g, axis=1).astype(np.float32)
    return np.ascontiguousarray(data, dtype=np.float32), sr


def save_audio(path, audio, sr, bits=32):
    """Write (channels, samples) audio. .wav natively; other extensions through ffmpeg."""
    audio = np.asarray(audio, dtype=np.float32)
    ext = os.path.splitext(path)[1].lower()
    if bits == 16:
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).T
    else:
        pcm = audio.T
    if ext == ".wav" or not ext:
        wavfile.write(path, sr, pcm)
        return path
    if not have_ffmpeg():
        raise RuntimeError("Saving as " + ext + " needs ffmpeg; save as .wav instead.")
    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, "x.wav")
        wavfile.write(tmp, sr, pcm)
        extra = ["-b:a", "320k"] if ext in (".mp3", ".m4a", ".aac", ".ogg") else []
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", tmp, *extra, path], capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode(errors="ignore")[-400:])
    return path


def wav_bytes(audio, sr):
    """16-bit WAV bytes for streaming previews to the UI."""
    import io
    buf = io.BytesIO()
    pcm = (np.clip(np.asarray(audio, np.float32), -1, 1) * 32767).astype(np.int16)
    wavfile.write(buf, sr, pcm.T if pcm.ndim == 2 else pcm)
    return buf.getvalue()


# ----------------------------------------------------------------------------- STFT
def stft(x):
    """Centered STFT of a 1-D signal -> complex64 (bins, frames). Frame t is centred on sample t*HOP."""
    x = np.asarray(x, dtype=np.float32)
    pad = N_FFT // 2
    xp = np.pad(x, (pad, pad + HOP))
    frames = np.lib.stride_tricks.sliding_window_view(xp, N_FFT)[::HOP]
    n_frames = len(x) // HOP + 1
    frames = frames[:n_frames]
    out = np.empty((N_FFT // 2 + 1, n_frames), dtype=np.complex64)
    step = 4096  # chunk to bound memory on long songs
    for i in range(0, n_frames, step):
        out[:, i:i + step] = np.fft.rfft(frames[i:i + step] * _WIN, axis=1).T
    return out


def istft(X, length):
    """Inverse of stft() (weighted overlap-add)."""
    n_frames = X.shape[1]
    total = (n_frames - 1) * HOP + N_FFT
    out = np.zeros(total, dtype=np.float64)
    norm = np.zeros(total, dtype=np.float64)
    w2 = _WIN.astype(np.float64) ** 2
    step = 4096
    for i0 in range(0, n_frames, step):
        fr = np.fft.irfft(X[:, i0:i0 + step].T, n=N_FFT, axis=1) * _WIN
        for j, f in enumerate(fr):
            s = (i0 + j) * HOP
            out[s:s + N_FFT] += f
            norm[s:s + N_FFT] += w2
    out /= np.maximum(norm, 1e-3)
    pad = N_FFT // 2
    res = out[pad:pad + length]
    if len(res) < length:
        res = np.pad(res, (0, length - len(res)))
    return res.astype(np.float32)


def segment(x, lo, hi):
    """x[..., lo:hi] with zero padding outside the signal."""
    n = x.shape[-1]
    out = np.zeros(x.shape[:-1] + (hi - lo,), dtype=x.dtype)
    a, b = max(lo, 0), min(hi, n)
    if b > a:
        out[..., a - lo:b - lo] = x[..., a:b]
    return out


def log(msg):
    print(msg, file=sys.stderr, flush=True)
