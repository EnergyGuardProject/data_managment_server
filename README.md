# EnergyGuard Data Management Server (DMS)

Internal FastAPI service that sits between the dashboard and JupyterHub.
It manages dataset/notebook storage in MinIO and provisions files into each
user's JupyterHub home directory.

## Architecture

```
Dashboard backend  ──POST /api/v1/provision/user──▶  Data Management Server
                                                           │
                                          ┌────────────────┼────────────────┐
                                          ▼                ▼                ▼
                                        MinIO          Host FS         MinIO
                                    (read/write)   /jupyterhub_data  (read only)
                                                        │
                                          ┌─────────────┴──────────────┐
                                          ▼                             ▼
                               /home/jovyan/datasets         /home/jovyan/notebooks
                               (read-only bind-mount)       (read-write bind-mount)
                               in singleuser container       in singleuser container
```

## MinIO layout

```
Bucket: datasets
└── user_<username>/
    └── <dataset_name>/
        ├── file1.csv
        ├── file2.csv
        └── metadata.json        ← optional

Bucket: notebooks
├── notebook_1.ipynb
└── notebook_2.ipynb
```

Datasets support **multiple files** per dataset. All files under the
`user_{username}/{dataset_name}/` prefix are treated as part of that dataset.

## JupyterHub user home layout (after provisioning)

```
/home/jovyan/
├── work/          ← persisted named volume (user's own work)
├── datasets/      ← read-only bind-mount (provisioned by DMS)
│   ├── dataset_xx/
│   │   ├── file1.csv
│   │   └── metadata.json
│   └── dataset_yy/
└── notebooks/     ← read-write bind-mount (provisioned by DMS once)
    ├── notebook_1.ipynb
    └── notebook_2.ipynb
```

Host file system layout (mounted into JupyterHub containers):

```
/jupyterhub_data/
├── datasets/
│   └── {username}/
│       └── {dataset_name}/    ← synced from MinIO (0o755 / files 0o644)
└── notebooks/
    └── {username}/            ← provisioned once per user (0o777 / files 0o666)
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/api/v1/datasets` | List datasets (`?username=x` to filter by owner) |
| `POST` | `/api/v1/datasets/update` | Re-download a dataset for all users that have it cached (for pilot datasets only in the future) |
| `GET`  | `/api/v1/notebooks` | List notebooks available in MinIO |
| `POST` | `/api/v1/provision/user` | Provision datasets + notebooks for a user |
| `DELETE` | `/api/v1/datasets/{username}/{dataset_name}` | Delete dataset from MinIO and local cache |
| `POST` | `/api/v1/datasets/upload` | Upload one or more dataset files (+ optional metadata) to MinIO (for testing) |
| `POST` | `/api/v1/datasets/metadata` | Upload/replace a dataset's metadata.json (for testing)|
| `GET`  | `/health` | Health check |

All endpoints (except `/health`) require an `X-API-Key` header.

### GET `/api/v1/datasets`

Optional query param `?username=<owner>` filters to that owner's datasets.
Returns a list of `DatasetInfo` objects:

```json
[
  {
    "owner": "john_doe",
    "name": "building_energy_2024",
    "files": ["readings.csv", "sensors.csv", "metadata.json"],
    "size_bytes": 204800
  }
]
```

### POST `/api/v1/datasets/update`

Re-downloads a dataset from MinIO into the local cache for every user that
currently has it. Stale local files (deleted from MinIO) are removed. In the 
future, this will be used to update the pilot datasets that need to change 
periodically using dagster.

Request body:

```json
{ "dataset_owner": "john_doe", "dataset_name": "building_energy_2024" }
```

Returns `{"dataset_owner": "...", "dataset_name": "...", "users_updated": [...], "errors": [...]}`.

### GET `/api/v1/notebooks`

Returns `[{"name": "notebook_1.ipynb", "size_bytes": 12345}, ...]`.

### POST `/api/v1/provision/user`

When the dashboard redirects a user to JupyterHub, it should first call:

```http
POST http://data-management-server:6060/api/v1/provision/user
X-API-Key: <api_key>
Content-Type: application/json

{
  "username": "john_doe",
  "datasets": {
    "aliki@gmail.com": "alikis_dataset",
    "pilot": "weather_data"
  },
  "notebooks": null
}
```

- `datasets`: mapping of dataset owner → dataset name to make available in the
  target user's volume
- `notebooks`: `null` = provision ALL platform notebooks (skip if already present);
  pass a list of names to provision specific ones; pass `[]` to skip notebooks entirely
- `force_notebook_refresh`: set `true` to overwrite existing notebooks

Returns:

```json
{
  "username": "john_doe",
  "datasets_provisioned": ["aliki@gmail.com/alikis_dataset", "pilot/weather_data"],
  "notebooks_provisioned": ["notebook_1.ipynb"],
  "errors": []
}
```

### DELETE `/api/v1/datasets/{username}/{dataset_name}`

Removes all objects under `user_{username}/{dataset_name}/` in MinIO and
deletes any cached copies at `/jupyterhub_data/datasets/*/{dataset_name}/`.

### POST `/api/v1/datasets/upload` (for testing, this will be done via the dashboard)

Multipart form fields:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `username` | string | yes | Dataset owner |
| `dataset_name` | string | yes | Dataset name |
| `files` | file[] | yes | One or more files to upload |
| `metadata` | file | no | JSON metadata file |

Returns `{"status": "ok", "objects": ["user_x/ds/file1.csv", ...]}`.

### POST `/api/v1/datasets/metadata` (for testing, this will be done via the dashboard)

Multipart form fields: `username`, `dataset_name`, `metadata` (file).
Validates that the uploaded file is valid JSON before storing.


## Configuration

All configuration is via environment variables (loaded from `.env`):

| Variable | Default | Description |
|----------|---------|-------------|
| `API_KEY` | _(required)_ | Internal service-to-service auth key |
| `MINIO_ENDPOINT` | `minio-backend.energy-guard.eu` | MinIO hostname |
| `MINIO_ACCESS_KEY` | _(required)_ | MinIO access key |
| `MINIO_SECRET_KEY` | _(required)_ | MinIO secret key |
| `MINIO_SECURE` | `true` | Use HTTPS for MinIO |
| `DATASETS_BUCKET` | `datasets` | MinIO bucket for datasets |
| `NOTEBOOKS_BUCKET` | `notebooks` | MinIO bucket for notebooks |
| `PILOT_PREFIX` | `user_pilot` | Prefix for platform/pilot datasets (reserved, unused) |
| `JUPYTERHUB_DATA_PATH` | `/jupyterhub_data` | Container path to shared JupyterHub data |
| `LOG_LEVEL` | `INFO` | Logging level |

## Deployment

### 1. Create the shared data directory on the host

```bash
sudo mkdir -p path/to/jupyterhub_data/datasets \
              path/to/jupyterhub_data/notebooks
```

### 2. Generate a strong API key and set it in `.env`

```bash
openssl rand -hex 32
# Paste the result as API_KEY in data_managment_server/.env
```

### 3. Build and start the service

```bash
cd path/to/data_managment_server
docker compose up -d --build
```

### 4. Restart JupyterHub to pick up the new config/volume

```bash
cd path/to/energyguard/JupyterHub
docker compose up -d --build
```
