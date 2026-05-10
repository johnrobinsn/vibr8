"""WeSpeaker ECAPA embedding model — lazy-loaded.

Loads the ECAPA-TDNN encoder bundled inside the WeSep ``bsrnn_ecapa_vox1``
checkpoint and exposes an ``embed(wav)`` API symmetric to
``server.speaker_model``. The two embedding spaces are NOT compatible with
SpeechBrain ECAPA — they must be stored separately on each enrollment entry.

Used for two purposes:
- conditioning input to the WeSep BSRNN target-speaker extractor (TSE)
- (future) gating itself, if we ever want to share one model

The ECAPA weights live under the ``spk_model.*`` prefix in the BSRNN
state dict; we load only that subset to keep VRAM small (~24 MB) when TSE
itself is not loaded.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

from server.vendor import ensure_on_path

logger = logging.getLogger(__name__)

_model = None
_device = None

# ECAPA architecture / fbank parameters — must match the BSRNN config
# (model_args.tse_model.spk_args / dataset_args.fbank_args of bsrnn_ecapa_vox1).
_FEAT_DIM = 80          # num_mel_bins
_EMBED_DIM = 192
_POOLING = "ASTP"
_FRAME_LENGTH_MS = 25
_FRAME_SHIFT_MS = 10
_SAMPLE_RATE = 16000


def _candidate_model_dirs() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("VIBR8_WESEP_DIR")
    if env:
        paths.append(Path(env))
    paths.append(Path.home() / ".vibr8" / "models" / "wespeaker-bsrnn-vox1")
    paths.append(Path.home() / ".wesep" / "english")
    return paths


def find_checkpoint() -> Path:
    """Return the path to ``avg_model.pt``, raising if not present."""
    for d in _candidate_model_dirs():
        ckpt = d / "avg_model.pt"
        if ckpt.exists():
            return ckpt
    tried = ", ".join(str(d) for d in _candidate_model_dirs())
    raise FileNotFoundError(
        "Could not find WeSep BSRNN checkpoint (avg_model.pt). "
        f"Looked in: {tried}. Set VIBR8_WESEP_DIR to override."
    )


def _ensure_loaded() -> None:
    global _model, _device
    if _model is not None:
        return

    import torch

    ensure_on_path()
    from wespeaker.models.speaker_model import get_speaker_model

    _device = "cuda" if torch.cuda.is_available() else "cpu"
    ckpt_path = find_checkpoint()
    logger.info("[wespeaker] Loading ECAPA from %s (device=%s)...", ckpt_path, _device)

    model = get_speaker_model("ECAPA_TDNN_GLOB_c512")(
        feat_dim=_FEAT_DIM,
        embed_dim=_EMBED_DIM,
        pooling_func=_POOLING,
    )

    # The checkpoint is the full BSRNN state dict; ECAPA weights live under
    # the ``spk_model.`` prefix.
    states = torch.load(str(ckpt_path), map_location="cpu")
    full_state = states["models"][0]
    spk_state = {
        k[len("spk_model."):]: v
        for k, v in full_state.items()
        if k.startswith("spk_model.")
    }
    if not spk_state:
        raise RuntimeError(
            f"No spk_model.* keys found in {ckpt_path}; checkpoint is not a "
            "joint-trained BSRNN+ECAPA model."
        )
    missing, unexpected = model.load_state_dict(spk_state, strict=False)
    if missing:
        logger.warning("[wespeaker] missing keys: %s", missing[:5])
    if unexpected:
        logger.warning("[wespeaker] unexpected keys: %s", unexpected[:5])

    model.eval()
    model.to(_device)
    _model = model
    logger.info("[wespeaker] ECAPA loaded (%d-dim embeddings)", _EMBED_DIM)


def _compute_fbank(wav: np.ndarray):
    """Match ``Extractor.compute_fbank`` from wesep (kaldi fbank + CMN)."""
    import torch
    import torchaudio.compliance.kaldi as kaldi

    if wav.ndim > 1:
        wav = wav.mean(axis=0)
    t = torch.from_numpy(wav.astype(np.float32)).unsqueeze(0)  # (1, T)
    feat = kaldi.fbank(
        t,
        num_mel_bins=_FEAT_DIM,
        frame_length=_FRAME_LENGTH_MS,
        frame_shift=_FRAME_SHIFT_MS,
        sample_frequency=_SAMPLE_RATE,
    )
    feat = feat - torch.mean(feat, 0)
    return feat


def embed(wav: np.ndarray) -> np.ndarray:
    """Extract a 192-dim WeSpeaker ECAPA embedding from float32 16 kHz mono.

    Returns the **un-normalized** ECAPA output — this matches what the BSRNN
    target-speaker extractor was trained to receive (its internal ``spk_model``
    feeds its raw output through an Identity ``spk_transform``). Don't
    L2-normalize before passing to TSE.

    Note: lives in a different space from ``server.speaker_model.embed``;
    the two are not interchangeable.
    """
    import torch

    _ensure_loaded()
    feat = _compute_fbank(wav).unsqueeze(0).to(_device)  # (1, T_frames, 80)
    with torch.no_grad():
        emb = _model(feat).squeeze(0).cpu().numpy().astype(np.float32)
    return emb


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity (normalizes inputs on the fly since ``embed`` is raw)."""
    na = float(np.linalg.norm(a)) + 1e-12
    nb = float(np.linalg.norm(b)) + 1e-12
    return float(np.dot(a, b) / (na * nb))


def enroll(wavs: list[np.ndarray]) -> np.ndarray:
    """Average multiple utterance embeddings (no normalization — TSE wants raw)."""
    embs = np.stack([embed(w) for w in wavs])
    return embs.mean(axis=0)


def is_loaded() -> bool:
    return _model is not None


def unload() -> None:
    global _model, _device
    if _model is not None:
        logger.info("[wespeaker] Unloading ECAPA model")
    _model = None
    _device = None
