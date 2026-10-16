"""Env-driven settings. Every field maps 1:1 to a DAOLAB_* variable; .env is
loaded once at import, real environment variables take precedence (dotenv default)."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    db_path: str
    api_host: str
    api_port: int
    api_url: str
    token: str
    internal_secret: str
    usecases_dir: str
    setups_dir: str
    inventory: str
    playbooks_dir: str
    dashboard_dir: str
    hw_class_lead_min: int
    provision_lead_min: int
    engine_interval_sec: int
    max_retries: int
    sched_horizon_hours: int
    webhook_url: str
    log_level: str
    runner: str
    slot_min: int
    cleanup_min: int
    default_hours: int
    ip_pool: str
    early_access: bool
    health_stale_sec: int
    otel_endpoint: str
    max_live_reservations: int
    max_future_reservations: int
    mock_step_sec: float
    mock_fail: str
    job_timeout_sec: int
    prometheus_url: str
    reconcile_attempts: int
    authz_secret: str

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get

        def flag(name: str, default: str = "false") -> bool:
            return env(name, default).strip().lower() in ("1", "true", "yes", "on")

        return cls(
            db_path=env("DAOLAB_DB_PATH", "./daolab.db"),
            api_host=env("DAOLAB_API_HOST", "0.0.0.0"),
            api_port=int(env("DAOLAB_API_PORT", "8080")),
            api_url=env("DAOLAB_API_URL", "http://localhost:8080"),
            token=env("DAOLAB_TOKEN", ""),
            internal_secret=env("DAOLAB_INTERNAL_SECRET", ""),
            usecases_dir=env("DAOLAB_USECASES_DIR", "./usecases"),
            setups_dir=env("DAOLAB_SETUPS_DIR", "./setups"),
            inventory=env("DAOLAB_INVENTORY", "./inventory/hosts.yml"),
            playbooks_dir=env("DAOLAB_PLAYBOOKS_DIR", "./playbooks"),
            dashboard_dir=env("DAOLAB_DASHBOARD_DIR", "./dashboard"),
            hw_class_lead_min=int(env("DAOLAB_HW_CLASS_LEAD_MIN", "15")),
            provision_lead_min=int(env("DAOLAB_PROVISION_LEAD_MIN", "5")),
            engine_interval_sec=int(env("DAOLAB_ENGINE_INTERVAL_SEC", "60")),
            max_retries=int(env("DAOLAB_MAX_RETRIES", "3")),
            sched_horizon_hours=int(env("DAOLAB_SCHED_HORIZON_HOURS", "168")),
            webhook_url=env("DAOLAB_WEBHOOK_URL", ""),
            log_level=env("DAOLAB_LOG_LEVEL", "INFO"),
            runner=env("DAOLAB_RUNNER", "ansible"),
            slot_min=int(env("DAOLAB_SLOT_MIN", "30")),
            cleanup_min=int(env("DAOLAB_CLEANUP_MIN", "10")),
            default_hours=int(env("DAOLAB_DEFAULT_HOURS", "1")),
            ip_pool=env("DAOLAB_IP_POOL", "10.30.0.100-10.30.0.199"),
            early_access=flag("DAOLAB_EARLY_ACCESS"),
            health_stale_sec=int(env("DAOLAB_HEALTH_STALE_SEC", "120")),
            otel_endpoint=env("DAOLAB_OTEL_ENDPOINT", ""),
            max_live_reservations=int(env("DAOLAB_MAX_LIVE_RESERVATIONS", "1")),
            max_future_reservations=int(env("DAOLAB_MAX_FUTURE_RESERVATIONS", "2")),
            mock_step_sec=float(env("DAOLAB_MOCK_STEP_SEC", "5")),
            mock_fail=env("DAOLAB_MOCK_FAIL", ""),
            job_timeout_sec=int(env("DAOLAB_JOB_TIMEOUT_SEC", "1800")),
            prometheus_url=env("DAOLAB_PROMETHEUS_URL", "http://localhost:9090"),
            reconcile_attempts=int(env("DAOLAB_RECONCILE_ATTEMPTS", "2")),
            authz_secret=env("DAOLAB_AUTHZ_SECRET", ""),
        )


settings = Settings.from_env()
