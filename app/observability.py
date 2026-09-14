import logging
from collections import deque
from datetime import datetime, timezone


class RecentLogHandler(logging.Handler):
    def __init__(self, limit: int = 100):
        super().__init__()
        self.records = deque(maxlen=limit)

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(
            {
                "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
            }
        )


recent_logs = RecentLogHandler()
recent_logs.setFormatter(logging.Formatter("%(message)s"))


def configure_logging() -> None:
    root = logging.getLogger()
    if recent_logs not in root.handlers:
        root.addHandler(recent_logs)
    root.setLevel(logging.INFO)


def get_recent_logs() -> list[dict]:
    return list(recent_logs.records)
