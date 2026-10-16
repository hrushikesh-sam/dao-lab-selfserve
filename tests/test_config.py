"""B-01 config vars — plan/09 sections 4-8, plan/08 configuration reference."""
from service.config import Settings

NEW_DEFAULTS = {
    "runner": "ansible",
    "slot_min": 30,
    "cleanup_min": 10,
    "default_hours": 1,
    "ip_pool": "10.68.1.0-10.68.2.200",
    "early_access": False,
    "health_stale_sec": 120,
    "otel_endpoint": "",
    "max_live_reservations": 1,
    "max_future_reservations": 2,
    "prometheus_url": "http://localhost:9090",
    "reconcile_attempts": 2,
    "authz_secret": "",
    "allow_destructive": False,
    "deploy_vms": True,
    "become_password_file": "",
}

ENV_NAMES = {
    "runner": "DAOLAB_RUNNER",
    "slot_min": "DAOLAB_SLOT_MIN",
    "cleanup_min": "DAOLAB_CLEANUP_MIN",
    "default_hours": "DAOLAB_DEFAULT_HOURS",
    "ip_pool": "DAOLAB_IP_POOL",
    "early_access": "DAOLAB_EARLY_ACCESS",
    "health_stale_sec": "DAOLAB_HEALTH_STALE_SEC",
    "otel_endpoint": "DAOLAB_OTEL_ENDPOINT",
    "max_live_reservations": "DAOLAB_MAX_LIVE_RESERVATIONS",
    "max_future_reservations": "DAOLAB_MAX_FUTURE_RESERVATIONS",
    "prometheus_url": "DAOLAB_PROMETHEUS_URL",
    "reconcile_attempts": "DAOLAB_RECONCILE_ATTEMPTS",
    "authz_secret": "DAOLAB_AUTHZ_SECRET",
    "allow_destructive": "DAOLAB_ALLOW_DESTRUCTIVE",
    "deploy_vms": "DAOLAB_DEPLOY_VMS",
    "become_password_file": "DAOLAB_BECOME_PASSWORD_FILE",
}


def test_new_settings_load_defaults(monkeypatch):
    for env_name in ENV_NAMES.values():
        monkeypatch.delenv(env_name, raising=False)
    s = Settings.from_env()
    for field, expected in NEW_DEFAULTS.items():
        assert getattr(s, field) == expected, field


def test_overridden_values_take_precedence(monkeypatch):
    monkeypatch.setenv("DAOLAB_RUNNER", "mock")
    monkeypatch.setenv("DAOLAB_SLOT_MIN", "45")
    monkeypatch.setenv("DAOLAB_EARLY_ACCESS", "true")
    monkeypatch.setenv("DAOLAB_MAX_FUTURE_RESERVATIONS", "5")
    s = Settings.from_env()
    assert s.runner == "mock"
    assert s.slot_min == 45
    assert s.early_access is True
    assert s.max_future_reservations == 5
