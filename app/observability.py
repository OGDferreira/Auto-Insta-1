import logging
import re
from collections import deque
from datetime import datetime, timezone


_SENSITIVE_QUERY = re.compile(
    r"(?i)([?&](?:access_token|client_secret|code|appsecret_proof)=)[^&\s]+"
)


def redact_sensitive_url(value: str) -> str:
    """Remove credentials that httpx includes in request URLs."""
    return _SENSITIVE_QUERY.sub(r"\1[REDACTED]", value)


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.msg = redact_sensitive_url(record.getMessage())
            record.args = ()
        elif isinstance(record.msg, str):
            record.msg = redact_sensitive_url(record.msg)
        return True


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
recent_logs.addFilter(SensitiveDataFilter())


def configure_logging() -> None:
    root = logging.getLogger()
    redactor = SensitiveDataFilter()
    for handler in root.handlers:
        if not any(isinstance(item, SensitiveDataFilter) for item in handler.filters):
            handler.addFilter(redactor)
    if recent_logs not in root.handlers:
        root.addHandler(recent_logs)
    root.setLevel(logging.INFO)


def get_recent_logs() -> list[dict]:
    return list(recent_logs.records)
