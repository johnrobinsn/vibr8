"""WeSep BSRNN target-speaker extractor (TSE) — lazy-loaded, GPU-only.

The vendored ``BSRNN.forward`` always runs its bundled ECAPA encoder on the
enrollment input; we bypass that by replicating the rest of the forward path
inline and injecting a precomputed WeSpeaker embedding (the same one stored
on each enrollment entry as ``embedding_wespeaker``).

API:
    ``extract(audio_16khz, wespeaker_embedding) -> np.ndarray``
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import numpy as np

from server.vendor import ensure_on_path
from server.wespeaker_model import find_checkpoint

logger = logging.getLogger(__name__)

_model: Any = None
_device: str | None = None
_load_lock = threading.Lock()

# Architecture parameters from ``~/.wesep/english/config.yaml``
# (model_args.tse_model). Hardcoded here to avoid a yaml dep at import time
# and to make the contract explicit.
_BSRNN_KWARGS = dict(
    sr=16000,
    win=512,
    stride=128,
    feature_dim=128,
    num_repeat=6,
    spk_emb_dim=192,
    spk_fuse_type="multiply",
    use_spk_transform=False,
    multi_fuse=False,
    joint_training=True,
    spk_model="ECAPA_TDNN_GLOB_c512",
    spk_model_freeze=True,
    spk_args=dict(embed_dim=192, feat_dim=80, pooling_func="ASTP"),
    spk_feat=True,
    feat_type="consistent",
)


def is_available() -> bool:
    """True iff TSE *could* be loaded right now (CUDA + checkpoint present)."""
    try:
        import torch
        if not torch.cuda.is_available():
            return False
        find_checkpoint()
        return True
    except Exception:
        return False


def _ensure_loaded() -> None:
    global _model, _device
    if _model is not None:
        return
    with _load_lock:
        if _model is not None:
            return
        import torch

        if not torch.cuda.is_available():
            raise RuntimeError("TSE requires CUDA — no GPU available.")

        ensure_on_path()
        from wesep.models import get_model
        from wesep.utils.checkpoint import load_pretrained_model

        ckpt = find_checkpoint()
        _device = "cuda"
        logger.info("[tse] Loading WeSep BSRNN from %s ...", ckpt)
        model = get_model("BSRNN")(**_BSRNN_KWARGS)
        load_pretrained_model(model, str(ckpt))
        model.eval()
        model.to(_device)
        _model = model
        logger.info("[tse] WeSep BSRNN loaded (sr=16000, ~%dMB on GPU)",
                    sum(p.numel() for p in model.parameters()) * 4 // (1024 * 1024))


def _separate_with_embedding(model, mixture, embedding):
    """BSRNN.forward, minus the internal ECAPA call — uses a precomputed
    speaker embedding instead. Mirrors
    ``server/vendor/wesep/models/bsrnn.py:300-394``.
    """
    import torch

    wav_input = mixture
    batch_size, nsample = wav_input.shape
    nch = 1

    spec = torch.stft(
        wav_input,
        n_fft=model.win,
        hop_length=model.stride,
        window=torch.hann_window(model.win).to(wav_input.device).type(wav_input.type()),
        return_complex=True,
    )
    spec_RI = torch.stack([spec.real, spec.imag], 1)

    subband_spec = []
    subband_mix_spec = []
    band_idx = 0
    for i in range(len(model.band_width)):
        bw = model.band_width[i]
        subband_spec.append(spec_RI[:, :, band_idx:band_idx + bw].contiguous())
        subband_mix_spec.append(spec[:, band_idx:band_idx + bw])
        band_idx += bw

    subband_feature = []
    for i, bn_func in enumerate(model.BN):
        subband_feature.append(
            bn_func(subband_spec[i].view(batch_size * nch, model.band_width[i] * 2, -1))
        )
    subband_feature = torch.stack(subband_feature, 1)

    # Inject precomputed embedding directly. ``spk_transform`` is Identity in
    # our config (use_spk_transform=False), but we still call it so any future
    # checkpoint that flips that flag still works.
    spk_embedding = model.spk_transform(embedding)
    spk_embedding = spk_embedding.unsqueeze(1).unsqueeze(3)

    sep_output = model.separator(subband_feature, spk_embedding, torch.tensor(nch))

    sep_subband_spec = []
    for i, mask_func in enumerate(model.mask):
        bw = model.band_width[i]
        this_output = mask_func(sep_output[:, i]).view(batch_size * nch, 2, 2, bw, -1)
        this_mask = this_output[:, 0] * torch.sigmoid(this_output[:, 1])
        this_mask_real = this_mask[:, 0]
        this_mask_imag = this_mask[:, 1]
        est_spec_real = (
            subband_mix_spec[i].real * this_mask_real
            - subband_mix_spec[i].imag * this_mask_imag
        )
        est_spec_imag = (
            subband_mix_spec[i].real * this_mask_imag
            + subband_mix_spec[i].imag * this_mask_real
        )
        sep_subband_spec.append(torch.complex(est_spec_real, est_spec_imag))
    est_spec = torch.cat(sep_subband_spec, 1)
    output = torch.istft(
        est_spec.view(batch_size * nch, model.enc_dim, -1),
        n_fft=model.win,
        hop_length=model.stride,
        window=torch.hann_window(model.win).to(wav_input.device).type(wav_input.type()),
        length=nsample,
    )
    return output.view(batch_size, nch, -1).squeeze(1)


def extract(audio_16khz: np.ndarray, wespeaker_embedding: np.ndarray) -> np.ndarray:
    """Run TSE on a 16 kHz float32 mono segment, conditioned on the target
    speaker's WeSpeaker ECAPA embedding.

    Args:
        audio_16khz: float32 mono samples at 16 kHz, shape ``(N,)``.
        wespeaker_embedding: float32 ``(192,)`` un-normalized WeSpeaker output
            (``server.wespeaker_model.embed`` returns this directly).

    Returns: float32 mono samples at 16 kHz, shape ``(N,)``, peak-normalized
    to 0.9 (matching the upstream extractor).
    """
    import torch

    _ensure_loaded()

    if audio_16khz.ndim > 1:
        audio_16khz = audio_16khz.mean(axis=0)
    pcm = torch.from_numpy(np.ascontiguousarray(audio_16khz, dtype=np.float32))
    pcm = pcm.unsqueeze(0).to(_device)
    emb = torch.from_numpy(np.ascontiguousarray(wespeaker_embedding, dtype=np.float32))
    emb = emb.unsqueeze(0).to(_device)

    with torch.no_grad():
        sep = _separate_with_embedding(_model, pcm, emb).cpu()

    peak = sep.abs().max(dim=1, keepdim=True).values.clamp_min(1e-8)
    sep = sep / peak * 0.9
    return sep.squeeze(0).numpy().astype(np.float32)


def is_loaded() -> bool:
    return _model is not None


def unload() -> None:
    global _model, _device
    if _model is not None:
        logger.info("[tse] Unloading WeSep BSRNN")
    _model = None
    _device = None
