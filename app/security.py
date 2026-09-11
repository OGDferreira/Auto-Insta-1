from cryptography.fernet import Fernet
from werkzeug.security import check_password_hash, generate_password_hash

from .config import get_settings


def hash_password(password: str) -> str:
    return generate_password_hash(password, method="scrypt")


def verify_password(password: str, password_hash: str) -> bool:
    return check_password_hash(password_hash, password)


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
