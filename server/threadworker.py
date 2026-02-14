"""Simple queue-based worker thread for offloading blocking tasks.

Adapted from neortc2 (Copyright 2024 John Robinson, Apache 2.0).
"""

from __future__ import annotations

import logging
import queue
import threading

logger = logging.getLogger(__name__)


class ThreadWorker:
    """Execute callables on a background thread via a task queue."""

    def __init__(self, support_out_q: bool = True) -> None:
        self._in_q: queue.Queue = queue.Queue()
        self._out_q: queue.Queue | None = queue.Queue() if support_out_q else None
        self._stopped = False
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def _worker(self) -> None:
        while not self._stopped:
            item = self._in_q.get()
            if item is None:
                break
            func, args, kwargs = item
            try:
                result = func(*args, **kwargs)
                if self._out_q is not None and result is not None:
                    self._out_q.put(result)
            except Exception as e:
                logger.exception("[threadworker] Error in worker task")
                if self._out_q is not None:
                    self._out_q.put(e)
            finally:
                self._in_q.task_done()

    def add_task(self, func, *args, **kwargs) -> None:
        """Enqueue a callable to be executed on the worker thread."""
        self._in_q.put((func, args, kwargs))

    def get_result(self):
        """Retrieve the next result from the output queue (blocking)."""
        return self._out_q.get() if self._out_q else None

    def stop(self) -> None:
        """Gracefully shut down the worker thread."""
        self._stopped = True
        self._in_q.put(None)
        self._thread.join(timeout=5)
