"""ECAPA-TDNN speaker embedding model — lazy-loaded.

Uses speechbrain/spkrec-ecapa-voxceleb for 192-dim speaker embeddings.
Only loaded when a speaker fingerprint is selected for gating.
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

_model = None
_device = None


def _shim_torchaudio_backend() -> None:
    """Compat shim for torchaudio 2.8+ where backend.common was removed.

    SpeechBrain (and other older libs) may import
    torchaudio.backend.common.AudioMetaData which no longer exists.
    """
    try:
        import torchaudio  # noqa: F401
    except Exception:
        return
    try:
        import torchaudio.backend.common  # noqa: F401
        return
    except Exception:
        pass

    try:
        import torchaudio as _ta
        audio_meta_data = getattr(_ta, "AudioMetaData", None)
        if audio_meta_data is None:
            from dataclasses import dataclass

            @dataclass
            class AudioMetaData:
                sample_rate: int = 0
                num_frames: int = 0
                num_channels: int = 0
                bits_per_sample: int = 0
                encoding: str = ""

            audio_meta_data = AudioMetaData

        common_mod = types.ModuleType("torchaudio.backend.common")
        common_mod.AudioMetaData = audio_meta_data
        backend_mod = types.ModuleType("torchaudio.backend")
        backend_mod.common = common_mod
        sys.modules["torchaudio.backend"] = backend_mod
        sys.modules["torchaudio.backend.common"] = common_mod
        if not hasattr(_ta, "AudioMetaData"):
            _ta.AudioMetaData = audio_meta_data
    except Exception:
        pass


def _ensure_loaded() -> None:
    global _model, _device
    if _model is not None:
        return

    import torch

    _shim_torchaudio_backend()
    from speechbrain.inference.speaker import EncoderClassifier

    _device = "cuda" if torch.cuda.is_available() else "cpu"
    save_dir = str(Path.home() / ".vibr8" / "models" / "ecapa-voxceleb")
    logger.info("[speaker] Loading ECAPA model (device=%s)...", _device)
    _model = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        run_opts={"device": _device},
        savedir=save_dir,
    )
    logger.info("[speaker] ECAPA model loaded")


def embed(wav: np.ndarray) -> np.ndarray:
    """Extract L2-normalized 192-dim embedding from audio.

    wav: float32 mono, 16 kHz.
    """
    import torch

    _ensure_loaded()
    if wav.ndim > 1:
        wav = wav.mean(axis=0)
    t = torch.from_numpy(wav.astype(np.float32)).unsqueeze(0).to(_device)
    with torch.no_grad():
        emb = _model.encode_batch(t).squeeze().cpu().numpy().astype(np.float32)
    n = float(np.linalg.norm(emb)) + 1e-12
    return emb / n


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two L2-normalized embeddings (= dot product)."""
    return float(np.dot(a, b))


def enroll(wavs: list[np.ndarray]) -> np.ndarray:
    """Average multiple utterance embeddings into an L2-normalized centroid."""
    embs = np.stack([embed(w) for w in wavs])
    centroid = embs.mean(axis=0)
    return centroid / (float(np.linalg.norm(centroid)) + 1e-12)


def is_loaded() -> bool:
    return _model is not None


def unload() -> None:
    global _model, _device
    if _model is not None:
        logger.info("[speaker] Unloading ECAPA model")
    _model = None
    _device = None
