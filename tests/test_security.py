import importlib
import os
import time

import pytest
from fastapi import HTTPException


def load_security(secret="x" * 40):
    os.environ["SESSION_SECRET"] = secret
    import security
    return importlib.reload(security)


def test_signed_session_round_trip():
    security = load_security()
    token = security.create_session_token("0333593681", "USER")
    principal = security.decode_session_token(token)
    assert principal.username == "0333593681"
    assert principal.role == "USER"


def test_tampered_session_is_rejected():
    security = load_security()
    token = security.create_session_token("adminpro", "SUPER_ADMIN")
    with pytest.raises(HTTPException) as exc:
        security.decode_session_token(token + "x")
    assert exc.value.status_code == 401


def test_short_secret_is_rejected():
    security = load_security("short")
    with pytest.raises(RuntimeError):
        security.create_session_token("user", "USER")


class FakeRedis:
    def __init__(self): self.values = {}; self.ttls = {}
    def incr(self, key): self.values[key] = self.values.get(key, 0) + 1; return self.values[key]
    def expire(self, key, ttl): self.ttls[key] = ttl


def test_rate_limit():
    security = load_security()
    cache = FakeRedis()
    security.enforce_rate_limit(cache, "login:test", 2, 60)
    security.enforce_rate_limit(cache, "login:test", 2, 60)
    with pytest.raises(HTTPException) as exc:
        security.enforce_rate_limit(cache, "login:test", 2, 60)
    assert exc.value.status_code == 429
