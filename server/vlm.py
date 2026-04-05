"""VLM (Vision-Language Model) loading and inference.

Ported from /mntc/code/v1/src/v1/model/loader.py and inference.py.
Loads UI-TARS (Qwen2-VL architecture) with BitsAndBytes int4 quantization
for in-process GPU inference.  No external server required.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import torch
from PIL import Image

logger = logging.getLogger(__name__)

# Defaults matching v1/config.py
DEFAULT_MODEL_NAME = "bytedance-research/UI-TARS-7B-DPO"
DEFAULT_QUANTIZATION = "int4"  # "none", "int4", "int8"
DEFAULT_DEVICE = "auto"
DEFAULT_MAX_NEW_TOKENS = 512


@dataclass
class LoadedModel:
    model: object  # transformers.PreTrainedModel
    processor: object  # transformers.AutoProcessor
    device: torch.device
    model_name: str
    quantization: str
    load_time: float

    def vram_usage_mb(self) -> float | None:
        if not torch.cuda.is_available():
            return None
        total = 0
        for i in range(torch.cuda.device_count()):
            total += torch.cuda.memory_allocated(i)
        return total / (1024 * 1024)


@dataclass
class InferenceResult:
    text: str
    input_tokens: int
    output_tokens: int
    total_ms: float
    vram_mb: float | None


def load_model(
    name: str = DEFAULT_MODEL_NAME,
    quantization: str = DEFAULT_QUANTIZATION,
    device: str = DEFAULT_DEVICE,
) -> LoadedModel:
    """Load a VLM (UI-TARS / Qwen2-VL) with the configured quantization.

    This is expensive (~15s + 15GB download on first run).  Call once at
    startup and pass the result to all agents.
    """
    from transformers import Qwen2VLForConditionalGeneration

    start = time.time()
    dev = _resolve_device(device)

    # Pin to a single GPU to avoid cross-GPU splits
    if dev.type == "cuda":
        device_map = {"": dev.index if dev.index is not None else 0}
    else:
        device_map = dev.type

    model_kwargs: dict = {
        "device_map": device_map,
        "torch_dtype": _resolve_dtype(dev),
    }

    if quantization == "int4":
        from transformers import BitsAndBytesConfig
        model_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_quant_type="nf4",
        )
    elif quantization == "int8":
        from transformers import BitsAndBytesConfig
        model_kwargs["quantization_config"] = BitsAndBytesConfig(load_in_8bit=True)

    processor = _load_processor(name)
    model = Qwen2VLForConditionalGeneration.from_pretrained(
        name, trust_remote_code=True, **model_kwargs,
    )

    load_time = time.time() - start
    loaded = LoadedModel(
        model=model,
        processor=processor,
        device=dev,
        model_name=name,
        quantization=quantization,
        load_time=load_time,
    )
    logger.info(
        "[vlm] Model loaded: %s (quant=%s, device=%s, %.1fs, %.0f MB VRAM)",
        name, quantization, dev, load_time, loaded.vram_usage_mb() or 0,
    )
    return loaded


def run_inference(
    model: LoadedModel,
    image: Image.Image,
    prompt: str,
    max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS,
) -> InferenceResult:
    """Run VLM inference: image + text prompt → text response.

    Stateless — no chat history.  Matches v1's inference.py exactly.
    """
    processor = model.processor
    llm = model.model

    # Single user message with image + text (no system role — v1 pattern)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": prompt},
            ],
        }
    ]

    text_input = processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True,
    )
    inputs = processor(
        text=[text_input],
        images=[image],
        return_tensors="pt",
        padding=True,
    )
    inputs = inputs.to(llm.device)
    input_token_count = inputs["input_ids"].shape[1]

    torch.cuda.synchronize() if torch.cuda.is_available() else None
    t0 = time.perf_counter()

    with torch.no_grad():
        output_ids = llm.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
        )

    torch.cuda.synchronize() if torch.cuda.is_available() else None
    t_total = time.perf_counter() - t0

    generated_ids = output_ids[0, input_token_count:]
    output_text = processor.tokenizer.decode(generated_ids, skip_special_tokens=True)
    output_token_count = len(generated_ids)

    return InferenceResult(
        text=output_text.strip(),
        input_tokens=input_token_count,
        output_tokens=output_token_count,
        total_ms=t_total * 1000,
        vram_mb=model.vram_usage_mb(),
    )


# ── Internal helpers ─────────────────────────────────────────────────────────

def _load_processor(model_name: str):
    """Load the processor, handling format differences across model versions.

    UI-TARS uses Qwen2-VL's preprocessor config with {max_pixels, min_pixels}
    in the 'size' field, but newer transformers expects {shortest_edge, longest_edge}.
    Ported from v1/model/loader.py:84-113.
    """
    from transformers import AutoProcessor

    try:
        return AutoProcessor.from_pretrained(model_name, trust_remote_code=True)
    except ValueError:
        from transformers import (
            Qwen2VLImageProcessor, Qwen2VLVideoProcessor,
            AutoTokenizer, Qwen2VLProcessor,
        )
        image_processor = Qwen2VLImageProcessor.from_pretrained(
            model_name,
            size={"shortest_edge": 28, "longest_edge": 2116800},
        )
        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        video_processor = Qwen2VLVideoProcessor.from_pretrained(model_name)
        return Qwen2VLProcessor(
            image_processor=image_processor,
            tokenizer=tokenizer,
            video_processor=video_processor,
        )


def _resolve_device(device_str: str) -> torch.device:
    if device_str == "auto":
        if torch.cuda.is_available():
            return torch.device("cuda")
        return torch.device("cpu")
    return torch.device(device_str)


def _resolve_dtype(device: torch.device) -> torch.dtype:
    if device.type == "cuda":
        return torch.bfloat16
    return torch.float32
