from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # MinIO service credentials
    minio_endpoint: str = "minio-backend.energy-guard.eu"
    minio_access_key: str
    minio_secret_key: str
    minio_secure: bool = True

    # Bucket names (will be created on startup if absent)
    datasets_bucket: str = "datasets"
    notebooks_bucket: str = "notebooks"

    # Prefix for per-user datasets uploaded through the dashboard.
    pilot_prefix: str = "user_pilot"

    # Prefix in the datasets bucket holding the nightly pilot exports, laid out
    # as <pilot_datasets_prefix>/<PARTNER>/<PARTNER>.csv.gz. The dashboard reads
    # the same layout, so changing it here means changing it there too.
    pilot_datasets_prefix: str = "pilot_datasets"

    # Path inside the DMS container that maps to the JupyterHub shared data dir.
    # Bind-mounted from the host at /mnt/datadisk/volumes/jupyterhub_data.
    jupyterhub_data_path: str = "/jupyterhub_data"

    # ── CARTIF data lake (Postgres) ──────────────────────────────────────────
    # Read-only account; this VM's public IP is allow-listed, so the connection
    # is direct. The password must come from the environment — never hardcode.
    datalake_host: str = "srv9.cartif.es"
    datalake_port: int = 60007
    datalake_user: str = "readonlyaccess"
    datalake_password: str = ""
    datalake_connect_timeout: int = 30
    # Ceiling for a single partner's COPY. CEDER is ~127M rows; 6h is generous.
    datalake_statement_timeout_ms: int = 6 * 60 * 60 * 1000

    # gzip level 6 is the sweet spot here: level 9 roughly doubles CPU time on a
    # multi-GB export for a low-single-digit size gain on numeric CSV.
    pilot_export_gzip_level: int = 6

    # ── Pilot export schedule (pilot-export-scheduler container only) ────────
    pilot_export_hour: int = 1
    pilot_export_minute: int = 0
    # Gap between consecutive partners so seven exports never run concurrently.
    pilot_export_stagger_minutes: int = 45
    # On scheduler startup, export any partner whose data is missing or stale
    # rather than waiting for the next nightly slot. Deliberately conditional:
    # the container restarts on crash, on daemon restart and on every redeploy,
    # and an unconditional catch-up would re-export CEDER each time.
    pilot_export_on_startup: bool = True
    # A partner is stale past this age. Must exceed the 24h nightly cadence, or
    # an ordinary daytime redeploy would trigger a full re-export.
    pilot_export_max_age_hours: int = 36
    # Grace period before catch-up work begins, so the scheduler is fully up
    # and a crash-looping container does not start an export it cannot finish.
    pilot_export_startup_delay_seconds: int = 60

    # How late a job may still start (seconds). This covers two cases: the
    # scheduler was down when the job was due, and the job is queued behind a
    # long-running partner (exports are serialized — see app/scheduler.py).
    # It must comfortably exceed a full CEDER run, or the partners scheduled
    # behind it get dropped as misfires.
    pilot_export_misfire_grace_time: int = 4 * 60 * 60

    # Where the pilot datasets are bind-mounted, read-only, inside each
    # JupyterHub singleuser container. Must match JupyterHub's pre_spawn_hook —
    # the symlinks this service provisions point here.
    pilot_mount_path: str = "/home/jovyan/.pilot"

    # Simple API key for internal service-to-service auth (X-API-Key header)
    api_key: str

    log_level: str = "INFO"

    model_config = {"env_file": ".env"}


settings = Settings()
