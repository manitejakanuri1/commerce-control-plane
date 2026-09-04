"""Background workers.

Two loops, and they are what turn reconciliation from a demo into a guarantee.
Nothing here depends on a person noticing a stuck order.

    expiry loop        returns stock from holds whose TTL has passed
    reconciliation     resolves orders the provider never told us about,
                       and retries webhooks that failed to process

Run as its own process:  python workers.py
"""

import logging
import signal
import threading
import time

import config
import core
import db
import payments

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format='{"ts":"%(asctime)s","level":"%(levelname)s",'
           '"logger":"%(name)s","msg":"%(message)s"}')
log = logging.getLogger("workers")

_stop = threading.Event()


def expiry_loop():
    while not _stop.is_set():
        try:
            released = core.release_expired()
            if released:
                log.info("released %s expired reservations", released)
        except Exception:                       # noqa: BLE001
            log.exception("expiry sweep failed")
        _stop.wait(config.EXPIRY_SWEEP_SECONDS)


def reconciliation_loop():
    while not _stop.is_set():
        try:
            retried = payments.retry_failed_webhooks()
            if retried:
                log.info("retried %s webhook events", len(retried))

            resolved = payments.sweep()
            if resolved:
                log.info("reconciled %s orders", len(resolved))
                for line in resolved:
                    log.info("%s", line)

            pressure = core.reconciliation_pressure(window_minutes=15)
            if pressure >= 5:
                # The alert rule. Repeated reconciliation means webhook
                # delivery is degraded, and somebody should be told before
                # customers start noticing.
                log.error(
                    "ALERT reconciliation ran %s times in 15 minutes, "
                    "webhook delivery is degraded", pressure)
        except Exception:                       # noqa: BLE001
            log.exception("reconciliation sweep failed")
        _stop.wait(config.RECONCILE_SWEEP_SECONDS)


def _handle_signal(signum, _frame):
    log.info("signal %s received, shutting down", signum)
    _stop.set()


def main():
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    db.migrate()
    log.info("workers started: expiry every %ss, reconciliation every %ss",
             config.EXPIRY_SWEEP_SECONDS, config.RECONCILE_SWEEP_SECONDS)

    threads = [
        threading.Thread(target=expiry_loop, name="expiry", daemon=True),
        threading.Thread(target=reconciliation_loop, name="reconcile",
                         daemon=True),
    ]
    for thread in threads:
        thread.start()

    while not _stop.is_set():
        time.sleep(0.5)

    for thread in threads:
        thread.join(timeout=5)
    db.close()
    log.info("workers stopped")


if __name__ == "__main__":
    main()
