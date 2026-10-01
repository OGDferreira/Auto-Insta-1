import secrets

from cryptography.fernet import Fernet
from werkzeug.security import check_password_hash, generate_password_hash

from .config import get_settings


def hash_password(password: str) -> str:
    return generate_password_hash(password, method="scrypt")


def verify_password(password: str, password_hash: str) -> bool:
    return check_password_hash(password_hash, password)


def ensure_csrf_token(session: dict) -> str:
    """Return a per-session CSRF token, creating it on first use."""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def csrf_token_matches(expected: str | None, supplied: str | None) -> bool:
    """Compare CSRF tokens without leaking timing information."""
    if not expected or not supplied:
        return False
    return secrets.compare_digest(expected, supplied)


def _fernet() -> Fernet:
    key = get_settings().fernet_key
    if not key:
        raise RuntimeError("FERNET_KEY is required to store Instagram tokens")
    try:
        return Fernet(key.encode())
    except Exception as exc:
        raise RuntimeError("FERNET_KEY is not a valid Fernet key") from exc


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt_token(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
