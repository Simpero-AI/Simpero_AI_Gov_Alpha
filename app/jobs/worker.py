import logging
import os

from saq.types import SettingsDict

from app.jobs.queue import get_queue
from app.jobs.tasks import functions

# The `saq` CLI defaults to logging.WARNING unless run with -v (saq/runner.py), but
# this worker runs the ENTIRE analysis pipeline (ingest -> verify -> corroboration
# -> screening -> synthesis), whose per-run summaries and per-claim no-signal
# reasons are logged at INFO. Raise THIS app's own loggers to INFO here -- at
# settings-import time, i.e. before SAQ configures logging -- so those diagnostics
# actually reach the worker's container logs (`docker compose logs worker`) without
# also turning on SAQ's own per-job INFO chatter (root stays at SAQ's level).
# Env-overridable: APP_LOG_LEVEL=WARNING to quieten, =DEBUG for per-decision detail.
logging.getLogger("app").setLevel(os.getenv("APP_LOG_LEVEL", "INFO").upper())

settings: SettingsDict = {
    "queue": get_queue(),
    "functions": functions,
    "concurrency": 10,
}
