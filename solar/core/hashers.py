"""Keep concurrent memory-hard password checks within a fixed memory budget."""
import threading

from django.contrib.auth.hashers import Argon2PasswordHasher


_HASH_SLOTS = threading.BoundedSemaphore(2)


class BoundedArgon2PasswordHasher(Argon2PasswordHasher):
    def encode(self, password, salt):
        with _HASH_SLOTS:
            return super().encode(password, salt)

    def verify(self, password, encoded):
        with _HASH_SLOTS:
            return super().verify(password, encoded)
