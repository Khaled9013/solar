import base64
import functools
import hashlib
import hmac

from cryptography.fernet import Fernet
from django.db import models

from solar.config import config


@functools.lru_cache(maxsize=1)
def master_key():
    return base64.urlsafe_b64decode((config()["data_dir"] / "master.key").read_bytes().strip())


def derive_key(purpose):
    return hmac.digest(master_key(), purpose.encode("utf-8"), "sha256")


def cipher(purpose="database"):
    return Fernet(base64.urlsafe_b64encode(derive_key(purpose)))


def seal(value, purpose="database"):
    return "enc:v1:" + cipher(purpose).encrypt(value.encode("utf-8")).decode("ascii")


def unseal(value, purpose="database"):
    if value is None or value == "":
        return ""
    if not value.startswith("enc:v1:"):
        raise ValueError("Expected encrypted data")
    return cipher(purpose).decrypt(value[7:].encode("ascii")).decode("utf-8")


def digest(value, purpose="lookup"):
    return hmac.new(derive_key(purpose), value.encode("utf-8"), hashlib.sha256).hexdigest()


class EncryptedTextField(models.TextField):
    """Randomized authenticated encryption, never searchable by plaintext."""

    def from_db_value(self, value, expression, connection):
        return unseal(value) if value is not None else None

    def get_prep_value(self, value):
        if value is None:
            return None
        return seal(str(value))
