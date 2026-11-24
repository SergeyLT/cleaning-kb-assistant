"""Хеширование пароля без хранения его исходного значения."""

import hashlib
import secrets

ITERATIONS = 600_000


def validate_password_hash(encoded: str) -> bool:
    """Проверить формат и ограничить вычислительную стоимость хеша."""
    try:
        algorithm, iterations, salt, digest = encoded.split("$")
        return (
            algorithm == "pbkdf2_sha256"
            and ITERATIONS <= int(iterations) <= 2_000_000
            and len(bytes.fromhex(salt)) == 16
            and len(bytes.fromhex(digest)) == 32
        )
    except (ValueError, TypeError):
        return False


def hash_password(password: str) -> str:
    """Получить хеш с индивидуальной случайной солью."""
    if not 12 <= len(password) <= 256:
        raise ValueError("Пароль должен содержать от 12 до 256 символов")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Сравнить хеш пароля, не сравнивая исходные секреты."""
    if not validate_password_hash(encoded) or len(password) > 256:
        return False
    _, iterations, salt, expected = encoded.split("$")
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
    )
    return secrets.compare_digest(actual.hex(), expected)
