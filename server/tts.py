"""Text-to-speech via OpenAI's streaming TTS API with Opus output.

Adapted from neortc2 (Copyright 2024 John Robinson, Apache 2.0).

Streams audio from the OpenAI TTS API, parses the Ogg/Opus response
in real-time, and delivers individual Opus frames via a callback.
"""

from __future__ import annotations

import logging
import os
import struct
from typing import Callable, Optional

from aiohttp import ClientSession
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_openai_api_key = os.getenv("OPENAI_API_KEY", "")


class _OggProcessor:
    """Incremental Ogg/Opus stream parser.

    Accumulates raw bytes from a chunked HTTP response and extracts
    individual Opus audio frames, delivering them via a callback.
    """

    _PAGE_MAGIC = struct.unpack(">I", b"OggS")[0]
    _HEADER_MAGIC = struct.unpack(">Q", b"OpusHead")[0]
    _COMMENT_MAGIC = struct.unpack(">Q", b"OpusTags")[0]

    def __init__(self, cb: Callable[[bytes, dict], None]) -> None:
        self._cb = cb
        self._buffer = b""
        self._meta: Optional[dict] = None

    def _on_meta_page(self, page: bytes, header_size: int) -> None:
        meta_format = "<8sBBHIhB"
        (magic, version, channel_count, pre_skip, sample_rate, gain, channel_mapping) = struct.unpack_from(
            meta_format, page, header_size
        )
        sample_rate *= 2  # Ogg Opus header quirk
        self._meta = {
            "magic": magic.decode("utf-8"),
            "version": version,
            "channelCount": channel_count,
            "sampleRate": sample_rate,
        }

    def _on_page(self, page: bytes, header_size: int, segment_sizes: tuple) -> None:
        if self._cb and self._meta:
            i = header_size
            for size in segment_sizes:
                self._cb(page[i : i + size], self._meta)
                i += size

    def add_buffer(self, data: bytes) -> None:
        """Accumulate incoming bytes and parse all complete Ogg pages."""
        self._buffer += data
        i = 0
        while len(self._buffer) >= i + 27:
            if self._PAGE_MAGIC != struct.unpack_from(">I", self._buffer, i)[0]:
                i += 1
                continue

            num_segments = struct.unpack_from("B", self._buffer, i + 26)[0]
            header_size = 27 + num_segments

            if len(self._buffer) < i + header_size:
                return  # Wait for more data.

            segment_sizes = struct.unpack_from("B" * num_segments, self._buffer, i + 27)
            page_size = header_size + sum(segment_sizes)

            if len(self._buffer) < i + page_size:
                return  # Wait for more data.

            page = self._buffer[i : i + page_size]
            page_data_size = len(page) - header_size

            if page_data_size >= 8 and self._HEADER_MAGIC == struct.unpack_from(">Q", page, header_size)[0]:
                self._on_meta_page(page, header_size)
            elif page_data_size >= 8 and self._COMMENT_MAGIC == struct.unpack_from(">Q", page, header_size)[0]:
                pass  # Skip comment pages.
            else:
                self._on_page(page, header_size, segment_sizes)

            i += page_size
            self._buffer = self._buffer[i:]
            i = 0


class TTS_OpenAI:
    """Stream text-to-speech from the OpenAI API as Opus frames.

    Each Opus frame is delivered to *opus_frame_handler(frame_bytes)*
    as soon as it is parsed from the streamed Ogg response.

    Call :meth:`cancel` to abort an in-progress request (e.g. on barge-in).
    """

    def __init__(self, opus_frame_handler: Optional[Callable[[bytes], None]] = None) -> None:
        self._opus_frame_handler = opus_frame_handler
        self._cancelled = False

    def cancel(self) -> None:
        """Abort the current TTS request."""
        self._cancelled = True

    async def say(self, text: str) -> None:
        """Synthesize *text* and stream Opus frames to the handler."""
        if not text.strip():
            return

        def on_segment(segment: bytes, meta: dict) -> None:
            if self._opus_frame_handler:
                self._opus_frame_handler(segment)

        await self._request_tts(text, on_segment)

    async def _request_tts(self, text: str, callback: Callable) -> None:
        url = "https://api.openai.com/v1/audio/speech"
        headers = {
            "Authorization": f"Bearer {_openai_api_key}",
            "Content-Type": "application/json",
        }
        data = {
            "model": "tts-1-hd",
            "input": text,
            "voice": "echo",
            "response_format": "opus",
            "speed": 1.0,
        }

        async with ClientSession() as session:
            async with session.post(url=url, json=data, headers=headers, chunked=True) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error("[tts] OpenAI TTS failed (status=%d): %s", response.status, body[:200])
                    return

                processor = _OggProcessor(callback)
                async for chunk in response.content.iter_chunked(16384):
                    if self._cancelled:
                        logger.info("[tts] TTS cancelled (barge-in)")
                        return
                    processor.add_buffer(chunk)
