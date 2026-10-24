"""
In-process background worker: sends due notifications and releases expired holds.

The matcher only inserts notification jobs and calls `wake()`; sending happens
here, so a slow or failing provider never blocks a passenger or depot request.
Jobs are leased atomically in MongoDB, so running this worker in several app
processes (or alongside `python manage_matches.py run-jobs`) is safe.
"""
import logging
import threading
import uuid

logger = logging.getLogger(__name__)


class NotificationWorker:
    def __init__(self, service, interval_seconds: int):
        self.service = service
        self.interval = interval_seconds
        self.worker_id = f"worker-{uuid.uuid4().hex[:8]}"
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._tasks = []

    def add_task(self, fn):
        """Run `fn()` on every cycle too (e.g. Feature 3's claim expiry)."""
        self._tasks.append(fn)

    def run_once(self) -> dict:
        sent = self.service.process_due_notifications(self.worker_id)
        released = self.service.release_expired_matches()
        for task in self._tasks:
            task()
        return {"jobs": sent, "released": released}

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("Notification worker cycle failed; retrying next cycle.")
            self._wake.wait(self.interval)
            self._wake.clear()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="match-notify-worker", daemon=True)
        self._thread.start()
        logger.info("Match notification worker %s started (every %ss).", self.worker_id, self.interval)

    def wake(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()
