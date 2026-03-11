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

    # TODO The data managment server is going to get pilot datasets from another database, not from minio
    pilot_prefix: str = "user_pilot"

    # Path inside the DMS container that maps to the JupyterHub shared data dir.
    # Bind-mounted from the host at /home/energyguard/jupyterhub_data.
    jupyterhub_data_path: str = "/jupyterhub_data"

    # Simple API key for internal service-to-service auth (X-API-Key header)
    api_key: str

    log_level: str = "INFO"

    model_config = {"env_file": ".env"}


settings = Settings()
