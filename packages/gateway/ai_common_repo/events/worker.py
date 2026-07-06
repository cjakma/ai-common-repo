"""EventDeliveryWorker — background loop that drives WebhookDispatcher.

Mirrors the job Worker: a daemon thread that periodically claims and delivers due
outbox rows. Exceptions are isolated so a delivery storm can never crash the
gateway. Tests drive ``run_once`` directly instead of the thread.
"""
from __future__ import annotations

import logging
import threading

from .dispatcher import WebhookDispatcher


class EventDeliveryWorker:
    def __init__(self, dispatcher: WebhookDispatcher, *,
                 poll_interval_s: float = 5.0,
                 logger: logging.Logger | None = None):
        self._dispatcher = dispatcher
        self._poll_interval_s = poll_interval_s
        self._log = logger or logging.getLogger("ai_common_repo.webhook.worker")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> int:
        return self._dispatcher.run_pending()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="acr-webhook",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._dispatcher.run_pending()
            except Exception:  # noqa: BLE001 - never let the loop die
                self._log.exception("webhook delivery loop error")
            self._stop.wait(self._poll_interval_s)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
