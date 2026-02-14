"""End-of-utterance (EOU) detector using livekit/turn-detector ONNX model.

Adapted from neortc2 (Copyright 2024 John Robinson, Apache 2.0).
"""

from __future__ import annotations

import logging
import string

import numpy as np
import onnxruntime as ort
from huggingface_hub import hf_hub_download
from transformers import AutoTokenizer

logger = logging.getLogger(__name__)

HG_MODEL = "livekit/turn-detector"
ONNX_FILENAME = "model_quantized.onnx"
MAX_HISTORY_TOKENS = 512


def _softmax(logits: np.ndarray) -> np.ndarray:
    exp_logits = np.exp(logits - np.max(logits))
    return exp_logits / np.sum(exp_logits)


class EOU:
    """Predict the probability that a user utterance is complete."""

    def __init__(self) -> None:
        local_path = hf_hub_download(repo_id=HG_MODEL, filename=ONNX_FILENAME)
        self._session = ort.InferenceSession(local_path, providers=["CPUExecutionProvider"])
        self._tokenizer = AutoTokenizer.from_pretrained(
            HG_MODEL, local_files_only=False, truncation_side="left",
        )

    def __call__(self, message: str) -> float:
        """Return end-of-utterance probability for *message* (0.0–1.0)."""
        messages = [{"role": "user", "content": message}]
        return self._calc_messages_eou(messages)

    def _calc_messages_eou(self, messages: list[dict]) -> float:
        text = self._format_chat_messages(messages)
        inputs = self._tokenizer(
            text,
            return_tensors="np",
            truncation=True,
            max_length=MAX_HISTORY_TOKENS,
        )
        input_ids = np.array(inputs["input_ids"], dtype=np.int64)
        outputs = self._session.run(["logits"], {"input_ids": input_ids})
        logits = outputs[0][0, -1, :]
        probs = _softmax(logits)
        eou_token_id = self._tokenizer.encode("<|im_end|>")[-1]
        return float(probs[eou_token_id])

    def _format_chat_messages(self, chat_ctx: list[dict]) -> str:
        puncs = string.punctuation.replace("'", "")

        def normalize(text: str) -> str:
            return " ".join(text.translate(str.maketrans("", "", puncs)).lower().split())

        cleaned = []
        for msg in chat_ctx:
            content = normalize(msg["content"])
            if content:
                cleaned.append({"role": msg["role"], "content": content})

        convo_text = self._tokenizer.apply_chat_template(
            cleaned,
            add_generation_prompt=False,
            add_special_tokens=False,
            tokenize=False,
        )
        # Remove trailing EOU token from current utterance.
        ix = convo_text.rfind("<|im_end|>")
        return convo_text[:ix]


def create_eou() -> EOU:
    return EOU()
