# EnergyGuard Data Management Server (DMS)

Internal FastAPI service that sits between the dashboard and JupyterHub.
It provisions datasets and notebook files into each
user's JupyterHub home directory.

## Architecture

```
                                                                                     |────── Data lake (future)
                                                                                     |
Dashboard backend  ──POST /api/v1/provision/user──▶  Data Management Server  ◀──────┴───── MinIO
                                                        │
                                                        │
                                                        ▼
                                                      Host FS              
                                               /jupyterhub_data 
                                                        │
                                          ┌─────────────┴──────────────┐
                                          ▼                            ▼
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

Bucket: datasets — pilot exports (written by the nightly job)
└── pilot_datasets/             ← PILOT_DATASETS_PREFIX
    ├── RDN/RDN.csv.gz
    ├── CEDER/CEDER.csv.gz
    └── …                       ← one object per partner

Bucket: notebooks
├── notebook_1.ipynb
└── notebook_2.ipynb
```

Datasets support **multiple files** per dataset. All files under the
`user_{username}/{dataset_name}/` prefix are treated as part of that dataset.

Pilot exports are the exception — exactly one gzipped CSV per partner, refreshed
nightly from the CARTIF data lake. See [Pilot datasets](#pilot-datasets).

## JupyterHub user home layout (after provisioning)

```
/home/jovyan/
├── work/          ← persisted named volume (user's own work)
│   ├── datasets/  ← read-only bind-mount (provisioned by DMS)
│   │   ├── dataset_xx/
│   │   │   ├── file1.csv
│   │   │   └── metadata.json
│   │   ├── dataset_yy/
│   │   └── REA Pilot Data → /home/jovyan/.pilot/REA   ← symlink, not a copy
│   └── notebooks/ ← read-write bind-mount (provisioned by DMS once)
│       ├── notebook_1.ipynb
│       └── notebook_2.ipynb
└── .pilot/        ← read-only bind-mount, ONE shared copy for all users
    ├── REA/REA.csv.gz
    └── …
```

Host file system layout (mounted into JupyterHub containers):

```
/jupyterhub_data/
├── datasets/
│   └── {username}/
│       └── {dataset_name}/    ← synced from MinIO (0o755 / files 0o644)
├── notebooks/
│   └── {username}/            ← provisioned once per user (0o777 / files 0o666)
└── pilot_datasets/
    └── {PARTNER}/{PARTNER}.csv.gz   ← nightly export (0o755 / files 0o644)
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/api/v1/datasets` | List datasets (`?username=x` to filter by owner) |
| `POST` | `/api/v1/datasets/update` | Re-download a dataset for all users that have it cached (mainly for pilot datasets in the future) |
| `GET`  | `/api/v1/notebooks` | List notebooks available in MinIO |
| `POST` | `/api/v1/provision/user` | Provision datasets + notebooks for a user |
| `DELETE` | `/api/v1/datasets/{username}/{dataset_name}` | Delete dataset from MinIO and local cache |
| `DELETE` | `/api/v1/datasets/cache/{username}/{dataset_name}` | Delete dataset only from one user's JupyterHub cache (MinIO untouched) |
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
    "aliki@gmail.com/temperature_2024": "alikis_dataset",
    "pilot@pilot.com/raw_weather": "weather_data"
  },
  "notebooks": null
}
```

- `datasets`: mapping of `dataset_minio_path` → `dataset_name`.
  - `dataset_minio_path` is the bucket-relative prefix where the dataset
    actually lives in MinIO (`<owner>/<original_dataset_name>`). 
  - `dataset_name` is the name the user picked for the dataset (possibly
    renamed via the dashboard). It is the folder name that the dataset will
    be materialized under in JupyterHub at
    `/home/jovyan/datasets/<dataset_name>/`. Because users can rename
    datasets from the dashboard, `dataset_name` may differ from the original
    name embedded in `dataset_minio_path`.
- `notebooks`: `null` = provision ALL platform notebooks (skip if already present);
  pass a list of names to provision specific ones; pass `[]` to skip notebooks entirely
- `force_notebook_refresh`: set `true` to overwrite existing notebooks

So in the example above, `john_doe`'s JupyterHub volume will end up with:

```
/home/jovyan/datasets/
├── alikis_dataset/   ← downloaded from user_aliki@gmail.com/temperature_2024
└── weather_data/     ← downloaded from user_pilot/raw_weather
```

Returns:

```json
{
  "username": "john_doe",
  "datasets_provisioned": [
    "aliki@gmail.com/temperature_2024 -> alikis_dataset",
    "user_pilot/raw_weather -> weather_data"
  ],
  "notebooks_provisioned": ["notebook_1.ipynb"],
  "errors": []
}
```

### DELETE `/api/v1/datasets/{username}/{dataset_name}`

Removes all objects under `user_{username}/{dataset_name}/` in MinIO and
deletes any cached copies at `/jupyterhub_data/datasets/*/{dataset_name}/`.

### DELETE `/api/v1/datasets/cache/{username}/{dataset_name}`

Removes only `/jupyterhub_data/datasets/{username}/{dataset_name}/` from the
host cache. MinIO is **not** touched, and other users that have the same
dataset cached are unaffected. `dataset_name` here is the local folder name
as it appears in JupyterHub (which may be a user-chosen rename of the
underlying MinIO dataset). Returns `404` if the user has no such cached
dataset.

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


### POST `/api/v1/provision/pilot`

Gives a user access to a pilot dataset. Called by the dashboard.

```json
{ "username": "user@example.com", "partner": "RDN", "dataset_name": "RDN Pilot Data" }
```

`username` is the user's **email** — JupyterHub identifies users by email via
Keycloak OIDC, and the per-user directories on disk are named by it.

Returns the standard `ProvisionResult`. `404` if `partner` is not one of
`RDN CEDER BER CEA CARTIF REA ENGREEN`; idempotent, so the dashboard can call
it on every page load.

This creates a **symlink**, not a copy:

```
/jupyterhub_data/datasets/{email}/{dataset_name}  ->  /home/jovyan/.pilot/{PARTNER}
```

The target is a container-side path, so the link is deliberately dangling when
viewed on the host and resolves inside the user's server. Because it is created
under the user's already-mounted data directory, it appears in an **already
running** server — no restart needed, as Jupyter caches no filesystem state.

### POST `/api/v1/pilot-export/run`

Forces an export without waiting for the nightly schedule.

```json
{ "partners": ["REA"] }          // omit or null for all seven
```

Returns `202` and runs in the background. Add `?wait=true` to block for the
results — only sensible for small partners. For a big one, prefer the CLI in
the scheduler container so the work stays out of the API container:

```bash
docker compose exec pilot-export-scheduler python -m app.export_cli CEDER
```

### GET `/api/v1/pilot-export/status`

Per-partner size and timestamp of the last successful export, plus free space
on the shared volume.

## Pilot datasets

Pilot data is platform-owned and byte-identical for every user, so there is
**one copy on disk**, not one per user. Copying CEDER (~127M rows) per user
would cost several GB each time somebody clicks "add dataset".

### Nightly export

A separate container (`pilot-export-scheduler`) runs APScheduler's
`BlockingScheduler`. For each partner it streams

```
COPY (SELECT ts_id, calendar_id, sensor_id, f_value, corrected
      FROM public.f_tsdata) TO STDOUT WITH (FORMAT CSV, HEADER)
```

through gzip into `<PARTNER>.csv.gz.part`, uploads that file to MinIO, then
`os.replace()`s it onto `<PARTNER>.csv.gz`. Nothing is ever held in memory —
peak RSS is a few MB regardless of table size — and because the rename is
atomic and same-directory, a user reading the file in a running notebook sees
either the old complete export or the new one, never a truncated file.

Why a separate container: a multi-GB export cannot block API request handling,
and redeploying the API does not kill a running export. Why APScheduler rather
than cron: the job stays in Python and reuses this repo's config, MinIO client
and logging, while `max_instances=1` gives overlap prevention for free.

Exports are **serialized** (single-worker executor) and staggered 45 minutes
apart from 01:00, largest partner first:

| 01:00 | 01:45 | 02:30 | 03:15 | 04:00 | 04:45 | 05:30 |
|-------|-------|-------|-------|-------|-------|-------|
| CEDER | RDN | BER | CEA | CARTIF | REA | ENGREEN |

`misfire_grace_time` must stay larger than a full CEDER run, or partners queued
behind it get dropped as misfires.

One partner failing never aborts the others — each returns its own result, and
a partner whose database has no `public.f_tsdata` yet (RDN, at the time of
writing) fails cleanly and leaves the previous export in place.

### Startup catch-up

When the scheduler container starts it queues a one-off export for any partner
whose file is missing or older than `PILOT_EXPORT_MAX_AGE_HOURS`, so a first
deploy — or a restart after the VM was down overnight — produces data without
waiting for 01:00.

This is deliberately **conditional on staleness**, not unconditional. The
container restarts on crash, on Docker daemon restart and on every redeploy;
running exports on each of those would re-export CEDER (~127M rows) every time,
hammer the CARTIF data lake, and drag a multi-GB job into the middle of the
working day. With the age check, a crash loop or a routine redeploy finds fresh
files and does nothing.

Catch-up jobs go through the same single-worker executor as the cron jobs, so
they can never run alongside a scheduled export. Set
`PILOT_EXPORT_ON_STARTUP=false` to disable.

Note this lives in the **scheduler** container. Restarting the API container
(`data-management-server`) has no effect on exports — it never runs them.

### Access

`pilot_datasets/` is bind-mounted **read-only** into every singleuser container
at `/home/jovyan/.pilot` by JupyterHub's `pre_spawn_hook`. That mount is a
prerequisite for `POST /api/v1/provision/pilot` — without it the provisioned
symlinks dangle inside the container too.

In a notebook the file is read exactly as it looks:

```python
pd.read_csv('datasets/REA Pilot Data/REA.csv.gz')
```

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
| `PILOT_DATASETS_PREFIX` | `pilot_datasets` | MinIO prefix **and** shared-dir name for the nightly pilot exports |
| `PILOT_MOUNT_PATH` | `/home/jovyan/.pilot` | Where pilot data is mounted read-only in singleuser containers; must match JupyterHub's `pre_spawn_hook` |
| `DATALAKE_HOST` | `srv9.cartif.es` | CARTIF data lake host (this VM's IP is allow-listed) |
| `DATALAKE_PORT` | `60007` | CARTIF data lake port |
| `DATALAKE_USER` | `readonlyaccess` | Read-only data lake account |
| `DATALAKE_PASSWORD` | _(required for exports)_ | Data lake password — **never hardcode it** |
| `PILOT_EXPORT_HOUR` / `PILOT_EXPORT_MINUTE` | `1` / `0` | First export slot of the night |
| `PILOT_EXPORT_STAGGER_MINUTES` | `45` | Gap between consecutive partners |
| `PILOT_EXPORT_MISFIRE_GRACE_TIME` | `14400` | How late a queued/missed export may still start (must exceed a full CEDER run) |
| `PILOT_EXPORT_ON_STARTUP` | `true` | Catch up missing/stale partners when the scheduler container starts |
| `PILOT_EXPORT_MAX_AGE_HOURS` | `36` | Age past which a partner is considered stale — keep it above the 24h cadence |
| `PILOT_EXPORT_STARTUP_DELAY_SECONDS` | `60` | Delay before catch-up work begins |
| `JUPYTERHUB_DATA_PATH` | `/jupyterhub_data` | Container path to shared JupyterHub data |
| `LOG_LEVEL` | `INFO` | Logging level |

## Deployment

### 1. Create the shared data directory on the host

```bash
sudo mkdir -p path/to/jupyterhub_data/datasets \
              path/to/jupyterhub_data/notebooks \
              path/to/jupyterhub_data/pilot_datasets
```

### 2. Generate a strong API key and set it in `.env`

```bash
openssl rand -hex 32
# Paste the result as API_KEY in data_managment_server/.env
```

### 3. Set `DATALAKE_PASSWORD` in `.env`

Required by the export job. `.env` is gitignored; the password must not appear
in source.

### 4. Build and start the services

Starts both the API and the `pilot-export-scheduler` container:

```bash
cd path/to/data_managment_server
docker compose up -d --build
```

### 5. Restart JupyterHub to pick up the new config/volume

Required for pilot datasets — this is what adds the read-only `/home/jovyan/.pilot`
mount. Users with a server already running must restart it once to get the new
mount; after that, newly provisioned pilot datasets appear without a restart.

```bash
cd path/to/energyguard/JupyterHub
docker compose up -d --build
```

### 6. Seed the pilot exports

The nightly schedule will fill these in on its own, but the first run is worth
doing by hand:

```bash
docker compose exec pilot-export-scheduler python -m app.export_cli --all
docker compose logs -f pilot-export-scheduler
```
