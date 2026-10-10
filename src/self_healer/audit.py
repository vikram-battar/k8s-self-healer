"""JSON-lines audit log. Every remediation decision — taken or skipped —
is recorded here so a human can always answer "what did the healer do?"
"""

import datetime
import json


def utcnow_iso():
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class AuditLog:
    """Append-only JSON-lines audit log (file or in-memory)."""

    def __init__(self, path=None):
        self.path = path
        self.records = []
        self._fh = None
        if path:
            self._fh = open(path, "a", encoding="utf-8")

    def record(self, **fields):
        entry = {"timestamp": utcnow_iso()}
        entry.update(fields)
        self.records.append(entry)
        if self._fh:
            self._fh.write(json.dumps(entry) + "\n")
            self._fh.flush()
        return entry

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None
