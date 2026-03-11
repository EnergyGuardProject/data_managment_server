# EnergyGuard Data Management Server

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
        ├── data.csv
        └── metadata.json

Bucket: notebooks
├── notebook_1.ipynb
└── notebook_2.ipynb
```

## JupyterHub user home layout (after provisioning)

```
/home/jovyan/
├── work/          ← persisted named volume (user's own work)
├── datasets/      ← read-only bind-mount (provisioned by DMS)
│   ├── dataset_xx/
│   │   ├── data.csv
│   │   └── metadata.json
│   └── dataset_yy/
└── notebooks/     ← read-write bind-mount (provisioned by DMS once)
    ├── notebook_1.ipynb
    └── notebook_2.ipynb
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/datasets/upload` | Upload a dataset CSV (+ optional metadata) to MinIO |
| `POST` | `/api/v1/datasets/metadata` | Upload/replace a dataset's metadata.json |
| `GET`  | `/api/v1/datasets` | List datasets (`?username=x` to filter) |
| `DELETE` | `/api/v1/datasets/{username}/{dataset_name}` | Delete dataset from MinIO and local cache |
| `POST` | `/api/v1/datasets/update` | Re-download a dataset for all users that have it |
| `GET`  | `/api/v1/notebooks` | List notebooks available in MinIO |
| `POST` | `/api/v1/provision/user` | Provision datasets + notebooks for a user |
| `POST` | `/api/v1/provision/sync-pilot-datasets` | (Dagster) Refresh all pilot datasets (TODO) |
| `GET`  | `/health` | Health check |

All endpoints (except `/health`) require an `X-API-Key` header.

## Deployment

### 1. Create the shared data directory on the host

```bash
sudo mkdir -p /home/energyguard/jupyterhub_data/datasets \
              /home/energyguard/jupyterhub_data/notebooks
# Allow the appuser inside the DMS container (UID 1000) and the JupyterHub
# container to write to this directory.
sudo chown -R 1000:1000 /home/energyguard/jupyterhub_data
```

### 2. Generate a strong API key and set it in `.env`

```bash
openssl rand -hex 32
# Paste the result as API_KEY in data_managment_server/.env
```

### 3. Build and start the service

```bash
cd /home/energyguard/data_managment_server
docker compose up -d --build
```

### 4. Restart JupyterHub to pick up the new config/volume

```bash
cd /home/energyguard/JupyterHub
docker compose up -d --build
```

## Calling the provision endpoint (dashboard integration)

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

- `datasets`: mapping of dataset owner to dataset name to make available in the
  target user's volume
- `notebooks`: `null` = provision ALL platform notebooks (skip if already present);
  pass a list of names to provision specific ones; pass `[]` to skip notebooks.
- `force_notebook_refresh`: set `true` to overwrite existing notebooks.

## Dataset update flow

When a dataset is changed via the dashboard:

```http
POST http://data-management-server:6060/api/v1/datasets/update
X-API-Key: <api_key>
Content-Type: application/json

{ "dataset_owner": "john_doe", "dataset_name": "building_energy_2024" }
```

## Dagster scheduled sync (TODO)

Point your Dagster job at:

```http
POST http://data-management-server:6060/api/v1/provision/sync-pilot-datasets
X-API-Key: <api_key>
```
