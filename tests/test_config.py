"""Settings.from_env() regression tests.

These exist because of a real bug: with @dataclass(slots=True), `cls.<field>` returns
the slot descriptor rather than the default value, so every from_env() fallback was
silently wrong. It only crashed on the int fields — the str fields would have happily
used a descriptor repr as their default. Direct construction (Settings(...)) hides this
entirely, which is why the rest of the suite missed it.
"""

from __future__ import annotations

import pytest

from core_sim.config import Settings


def test_defaults_are_real_values_not_descriptors():
    s = Settings.from_env()
    assert s.engine == "redis_lua"
    assert s.redis_url == "redis://localhost:6379"
    assert isinstance(s.redis_max_connections, int)
    assert isinstance(s.redis_pool_timeout, float)
    assert isinstance(s.stream_maxlen, int)
    assert s.stream_maxlen == 1_000_000


def test_class_attribute_access_yields_defaults():
    # The exact mechanism that broke. Guard it directly.
    assert Settings.engine == "redis_lua"
    assert Settings.stream_maxlen == 1_000_000
    assert Settings.log_requests is False


def test_env_overrides(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENGINE", "dragonfly_lua")
    monkeypatch.setenv("REDIS_URL", "redis://dragonfly:6380")
    monkeypatch.setenv("STREAM_MAXLEN", "5000")
    monkeypatch.setenv("REDIS_POOL_TIMEOUT", "1.5")
    monkeypatch.setenv("LOG_REQUESTS", "true")

    s = Settings.from_env()
    assert s.engine == "dragonfly_lua"
    assert s.redis_url == "redis://dragonfly:6380"
    assert s.stream_maxlen == 5000
    assert s.redis_pool_timeout == 1.5
    assert s.log_requests is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("", False),
        ("nonsense", False),
    ],
)
def test_bool_parsing(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool):
    monkeypatch.setenv("LOG_JSON", value)
    assert Settings.from_env().log_json is expected


def test_relay_consumer_defaults_to_hostname(monkeypatch: pytest.MonkeyPatch):
    # Consumer name must be stable per replica so the PEL survives a restart; in K8s
    # that is the pod name via HOSTNAME.
    monkeypatch.setenv("HOSTNAME", "core-sim-relay-abc123")
    assert Settings.from_env().relay_consumer == "core-sim-relay-abc123"
