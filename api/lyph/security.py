import base64
import hashlib
import hmac
import secrets
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from cryptography.fernet import Fernet
from .config import settings

password_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(encoded: str, password: str) -> bool:
    try:
        return password_hasher.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False


def token() -> str:
    return secrets.token_urlsafe(32)


def digest(value: str) -> str:
    return hmac.new(settings.secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest()))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


encrypt_secret = encrypt
decrypt_secret = decrypt
