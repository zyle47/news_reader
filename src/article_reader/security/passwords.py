"""Bounded Argon2id adapter; hashing runs outside database transactions."""

from argon2 import PasswordHasher as Argon2Hasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type


class Argon2PasswordHasher:
    def __init__(self) -> None:
        self._hasher = Argon2Hasher(time_cost=3, memory_cost=65_536, parallelism=1, type=Type.ID)

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, encoded: str, password: str) -> bool:
        try:
            return self._hasher.verify(encoded, password)
        except (InvalidHashError, VerificationError):
            return False
