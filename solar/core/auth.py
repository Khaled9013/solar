from datetime import timedelta
from functools import lru_cache

from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone

from solar.crypto import digest
from solar.core.models import LoginThrottle, User


@lru_cache(maxsize=1)
def dummy_hash():
    return make_password("unavailable-account")


class PrivateBackend(BaseBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        user = User.objects.filter(login_id=digest(username.strip().casefold(), "login")).first()
        if user is None:
            # Same expensive hash path for a nonexistent username.
            check_password(password, dummy_hash())
            return None
        return user if user.check_password(password) and user.is_active else None

    def get_user(self, user_id):
        return User.objects.filter(pk=user_id, is_active=True).first()


def consume_login_attempt(username, ip):
    keys = [digest(username.strip().casefold(), "throttle-user"), digest(ip, "throttle-ip")]
    now = timezone.now()
    with transaction.atomic():
        rows = []
        for key in keys:
            row, _ = LoginThrottle.objects.get_or_create(key=key)
            if row.window_at < now - timedelta(minutes=15):
                row.attempts, row.window_at = 0, now
            rows.append(row)
        blocked = any(row.attempts >= (10 if i == 0 else 50) for i, row in enumerate(rows))
        if not blocked:
            for row in rows:
                row.attempts += 1
                row.save()
        return not blocked


def clear_user_attempts(username):
    LoginThrottle.objects.filter(key=digest(username.strip().casefold(), "throttle-user")).delete()
