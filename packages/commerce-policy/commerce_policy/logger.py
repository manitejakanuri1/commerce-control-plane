"""Sends a decision summary to the control plane, and never gets in the way.

Two rules govern this file.

**It cannot block a sale.** The queue is bounded and non-blocking; when it is
full, records are dropped and counted rather than made to wait. A merchant
whose shop stalls because our logging endpoint is slow would be right to
remove the package entirely.

**It cannot carry a secret.** Everything sent is listed in SAFE_FIELDS. Cost
is not among them, and neither is any customer field, because sending cost
here would quietly undo the entire reason the engine runs on their server.
"""

import json
import logging
import queue
import threading
import urllib.error
import urllib.request

from .version import USER_AGENT, __version__

log = logging.getLogger("commerce_policy.logger")

# The complete list of what leaves the merchant's server. Anything not named
# here is dropped before the request is built, so adding a field to a decision
# elsewhere in the package cannot accidentally start exporting it.
SAFE_FIELDS = (
    "merchant_id",      # who
    "sku",              # which product
    "asked_bps",        # what discount was requested
    "allowed_bps",      # what the band permitted
    "result",           # approved / refused
    "failed_rules",     # which rule names blocked it
    "engine_version",   # so an old release can be refused
    "at",               # when
)

TIMEOUT_SECONDS = 5


class DecisionLogger:
    def __init__(self, settings):
        self.url = settings["control_plane_url"].rstrip("/") + "/v1/policy/logs"
        self.api_key = settings.get("api_key") or ""
        self.enabled = bool(settings.get("send_logs")) and bool(self.api_key)
        self.dropped = 0
        self._queue = queue.Queue(maxsize=settings.get("log_queue_size", 1000))
        self._thread = None

        if self.enabled:
            self._start()

    def _start(self):
        # Daemon, so a shop's process still exits cleanly on Ctrl-C with
        # records outstanding. Losing a log line at shutdown is acceptable;
        # hanging a merchant's deploy is not.
        self._thread = threading.Thread(
            target=self._drain, name="commerce-policy-logger", daemon=True)
        self._thread.start()

    def send(self, decision):
        """Queue one record. Returns immediately, always."""
        if not self.enabled:
            return False
        record = {k: decision.get(k) for k in SAFE_FIELDS if k in decision}
        try:
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            # Counted rather than logged: a burst would otherwise turn one
            # outage into thousands of log lines on the merchant's server.
            self.dropped += 1
            return False

    def _drain(self):
        while True:
            record = self._queue.get()
            try:
                self._post(record)
            except Exception as exc:                       # noqa: BLE001
                # Nothing here is worth retrying forever. The merchant's own
                # policy.decisions table already holds the authoritative copy.
                log.debug("policy log not delivered (%s: %s)",
                          type(exc).__name__, exc)
            finally:
                self._queue.task_done()

    def _post(self, record):
        body = json.dumps(record).encode()
        request = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "X-API-Key": self.api_key,
                     "X-Engine-Version": __version__,
                     "User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status == 426:
                # 426 Upgrade Required: the control plane has refused this
                # release. Say so loudly — this is the one message a merchant
                # must not miss, because their engine is running a fault we
                # have already fixed and cannot reach.
                log.error("commerce-policy %s is no longer accepted. "
                          "Run: pip install -U commerce-policy", __version__)

    def flush(self, timeout=2.0):
        """Best-effort drain, for a script that is about to exit."""
        if not self.enabled:
            return
        finished = threading.Event()
        threading.Thread(
            target=lambda: (self._queue.join(), finished.set()),
            daemon=True).start()
        finished.wait(timeout)
