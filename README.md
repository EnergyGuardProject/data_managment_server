# EnergyGuard Data Provisioning Server

Internal FastAPI service that sits between the EnergyGuard dashboard and JupyterHub.
It provisions datasets and notebook files into each user's JupyterHub home
directory and exports the pilot partner data from the EnergyGuard data lake every
night.

The repository runs two containers from the same image.

| Container | Command | Purpose |
|-----------|---------|---------|
| `data-management-server` | `uvicorn app.main:app` | The HTTP API on port 6060 |
| `pilot-export-scheduler` | `python -m app.scheduler` | Nightly pilot exports |

## Architecture

```
Dashboard backend ──POST /api/v1/provision/*──▶ data-management-server ◀──── MinIO
                                                        │                      ▲
                                                        ▼                      │ <PARTNER>.csv.gz
                                                     Host FS                   │
                                   /mnt/datadisk/volumes/jupyterhub_data ◀── pilot-export-scheduler ◀── EnergyGuard data lake
                                                        │                   <PARTNER>.parquet
                          ┌─────────────────────────────┼──────────────────────────────┐
                          ▼                             ▼                              ▼
             /home/jovyan/work/datasets    /home/jovyan/work/notebooks        /home/jovyan/.pilot
             (read-only bind mount)        (read-write bind mount)            (read-only bind mount)
                                     in each singleuser container
```

## MinIO layout

```
Bucket: datasets
├── user_<username>/
│   └── <dataset_name>/
│       ├── file1.csv
│       ├── file2.csv
│       └── metadata.json        ← optional
└── pilot_datasets/              ← PILOT_DATASETS_PREFIX, written by 
    ├── RDN/RDN.csv.gz
    ├── CEDER/CEDER.csv.gz
    └── …                        ← one object per partner

Bucket: notebooks
├── notebook_1.ipynb
└── notebook_2.ipynb
```

A dataset can hold several files. Every object under the
`user_{username}/{dataset_name}/` prefix belongs to that dataset.

Pilot exports are different. Each partner has exactly one gzipped CSV in MinIO
and one Parquet file in JupyterHub, both refreshed nightly from the EnergyGuard data
lake. See [Pilot datasets](#pilot-datasets).

Both buckets are created on API startup if they do not exist.

## JupyterHub user home layout (after provisioning)

```
/home/jovyan/
├── work/          ← persisted named volume (user's own work)
│   ├── datasets/  ← read-only bind mount (provisioned by this service)
│   │   ├── dataset_xx/
│   │   │   ├── file1.csv
│   │   │   └── metadata.json
│   │   ├── dataset_yy/
│   │   └── REA Pilot Data → /home/jovyan/.pilot/REA   ← symlink, not a copy
│   └── notebooks/ ← read-write bind mount (provisioned once)
│       ├── notebook_1.ipynb
│       └── notebook_2.ipynb
└── .pilot/        ← read-only bind mount, one shared copy for all users
    ├── REA/REA.parquet
    └── …
```

Host file system layout. The host directory is
`/mnt/datadisk/volumes/jupyterhub_data` and both containers mount it at
`/jupyterhub_data`.

```
/jupyterhub_data/
├── datasets/
│   └── {username}/
│       └── {dataset_name}/    ← synced from MinIO 
├── notebooks/
│   └── {username}/            ← provisioned once per user
└── pilot_datasets/
    └── {PARTNER}/{PARTNER}.parquet  ← nightly export 
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/api/v1/datasets` | List datasets (`?username=x` to filter by owner) |
| `POST` | `/api/v1/datasets/update` | Re-download a dataset for every user that has it cached |
| `GET`  | `/api/v1/notebooks` | List notebooks available in MinIO |
| `POST` | `/api/v1/provision/user` | Provision datasets and notebooks for a user |
| `POST` | `/api/v1/provision/pilot` | Link a pilot dataset into a user's datasets directory |
| `DELETE` | `/api/v1/datasets/{username}/{dataset_name}` | Delete a dataset from MinIO and from every user's cache |
| `DELETE` | `/api/v1/datasets/cache/{username}/{dataset_name}` | Delete a dataset from one user's JupyterHub cache only (MinIO untouched) |
| `POST` | `/api/v1/datasets/upload` | Upload one or more dataset files (and optional metadata) to MinIO (for testing) |
| `POST` | `/api/v1/datasets/metadata` | Upload or replace a dataset's metadata file (for testing) |
| `POST` | `/api/v1/pilot-export/run` | Start a pilot export now |
| `GET`  | `/api/v1/pilot-export/status` | Show the pilot exports currently on disk |
| `GET`  | `/health` | Health check |

All endpoints except `/health` need an `X-API-Key` header. A missing or wrong
key returns `403`.

### GET `/api/v1/datasets`

The optional query parameter `?username=<owner>` limits the list to that
owner's datasets. Returns a list of `DatasetInfo` objects.

```json
[
  {
    "owner": "user_john_doe",
    "name": "building_energy_2024",
    "files": ["readings.csv", "sensors.csv", "metadata.json"],
    "size_bytes": 204800
  }
]
```

`owner` is the top level MinIO prefix, including `user_`.

### POST `/api/v1/datasets/update`

Downloads a dataset from MinIO again into the local cache of every user whose
cache has a folder with that dataset name. Local files that no longer exist in
MinIO are removed.

Request body

```json
{ "dataset_owner": "john_doe", "dataset_name": "building_energy_2024" }
```

Returns `{"dataset_owner": "...", "dataset_name": "...", "users_updated": [...], "errors": [...]}`.

### GET `/api/v1/notebooks`

Returns every `.ipynb` object in the notebooks bucket, for example
`[{"name": "notebook_1.ipynb", "size_bytes": 12345}]`.

### POST `/api/v1/provision/user`

The dashboard calls this before it redirects a user to JupyterHub.

```http
POST http://data-management-server:6060/api/v1/provision/user
X-API-Key: <api_key>
Content-Type: application/json

{
  "username": "john_doe",
  "datasets": {
    "user_aliki@gmail.com/temperature_2024": "alikis_dataset",
    "user_pilot@pilot.com/raw_weather": "weather_data"
  },
  "notebooks": null,
  "force_notebook_refresh": false
}
```

* `datasets` maps `dataset_minio_path` to `dataset_name`.
  * `dataset_minio_path` is the bucket relative prefix of the dataset in MinIO,
    in the form `user_<owner>/<original_dataset_name>`. The `user_` part is
    optional.
  * `dataset_name` is the name the user gave the dataset in the dashboard. The
    dataset is stored under this folder name in JupyterHub at
    `/home/jovyan/work/datasets/<dataset_name>/`. Users can rename datasets in
    the dashboard, so it can differ from the name in `dataset_minio_path`.
  * Files that already exist in the user's cache are kept. Only missing files
    are downloaded.
* `notebooks` set to `null` provisions every platform notebook that the user
  does not already have. A list of names provisions only those notebooks, and
  `[]` skips notebooks.
* `force_notebook_refresh` set to `true` overwrites notebooks the user already
  has.

With the request above, `john_doe` ends up with

```
/home/jovyan/work/datasets/
├── alikis_dataset/   ← downloaded from user_aliki@gmail.com/temperature_2024
└── weather_data/     ← downloaded from user_pilot@pilot.com/raw_weather
```

and the response

```json
{
  "username": "john_doe",
  "datasets_provisioned": [
    "user_aliki@gmail.com/temperature_2024 -> alikis_dataset",
    "user_pilot@pilot.com/raw_weather -> weather_data"
  ],
  "notebooks_provisioned": ["notebook_1.ipynb"],
  "errors": []
}
```

A dataset that fails to download, or is empty in MinIO, is listed in `errors`.
The other datasets and notebooks are still provisioned.

### POST `/api/v1/provision/pilot`

Gives a user access to a pilot dataset. The dashboard calls it.

```json
{ "username": "user@example.com", "partner": "RDN", "dataset_name": "RDN Pilot Data" }
```

`username` is the user's email. JupyterHub identifies users by email through
Keycloak OIDC, and the per-user directories on disk use that name.

`partner` must be one of `RDN CEDER BER CEA CARTIF REA ENGREEN` (case
insensitive), otherwise the endpoint returns `404`. `username` and
`dataset_name` must each be a single path component, otherwise it returns
`400`. The call is idempotent, so the dashboard can call it on every page load.

The endpoint creates a symlink and copies nothing.

```
/jupyterhub_data/datasets/{email}/{dataset_name}  ->  /home/jovyan/.pilot/{PARTNER}
```

The target is a path inside the singleuser container. 
Returns the standard `ProvisionResult`.

### DELETE `/api/v1/datasets/{username}/{dataset_name}`

Removes all objects under `user_{username}/{dataset_name}/` in MinIO and
deletes every cached copy at `/jupyterhub_data/datasets/*/{dataset_name}/`.

### DELETE `/api/v1/datasets/cache/{username}/{dataset_name}`

Removes only `/jupyterhub_data/datasets/{username}/{dataset_name}/` from the
host cache. MinIO is not touched, and other users with the same dataset cached
are not affected. `dataset_name` is the local folder name as it appears in
JupyterHub, which may be a rename of the MinIO dataset. Returns `404` if the
user has no cached dataset with that name.

### POST `/api/v1/datasets/upload` (for testing)

In production the dashboard uploads datasets. Multipart form fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `username` | string | yes | Dataset owner |
| `dataset_name` | string | yes | Dataset name |
| `files` | file[] | yes | One or more files to upload |
| `metadata` | file | no | JSON metadata file |

Files are stored under `user_{username}/{dataset_name}/` with their original
file names. Returns `{"status": "ok", "objects": ["user_x/ds/file1.csv", ...]}`.
Invalid metadata JSON returns `400`.

### POST `/api/v1/datasets/metadata` (for testing)

Multipart form fields `username`, `dataset_name` and `metadata` (file). The
file must contain valid JSON and is stored under its own file name in
`user_{username}/{dataset_name}/`. Returns
`{"status": "ok", "object": "user_x/ds/metadata.json"}`.

### POST `/api/v1/pilot-export/run`

Starts an export without waiting for the nightly schedule.

```json
{ "partners": ["REA"] }
```

Leave out `partners` or send `null` to export all seven. An unknown partner
returns `404`.

By default the export runs in the background and the endpoint returns `202`
with `{"started": [...], "detail": "..."}`. Add `?wait=true` to wait for the
results, which returns `{"results": [...]}` with one summary per partner. Use
`wait` only for small partners, since CEDER takes far longer than an HTTP
timeout.

Each export takes a per-partner lock in the shared directory. A manual run that
overlaps a scheduled run of the same partner fails for that partner and leaves
the other export running.

For large partners, run the CLI in the scheduler container so the export does
not run inside the API container.

```bash
docker compose exec pilot-export-scheduler python -m app.export_cli CEDER
```

### GET `/api/v1/pilot-export/status`

Returns the size and modification time of each partner's Parquet file, whether
it exists, its MinIO object name, and the free space on the shared volume.

## Pilot datasets

Pilot data is owned by the platform and is the same for every user, so there
is one copy on disk for everyone. Users get symlinks to it through
`POST /api/v1/provision/pilot`.

### Nightly export

The `pilot-export-scheduler` container runs an APScheduler
`BlockingScheduler`. For each partner it streams the following query from the
partner's database in the EnergyGuard data lake.

```
COPY (SELECT <calendar_id decoded> AS datetime, sensor_id,
             f_value AS "values", corrected
      FROM public.f_tsdata
      ORDER BY sensor_id, calendar_id) TO STDOUT WITH (FORMAT CSV, HEADER)
```

| Partner | Database |
|---------|----------|
| RDN | `TEF1_RDN` |
| CEDER | `TEF2_CEDER` |
| BER | `TEF3_BER` |
| CEA | `TEF4_CEA` |
| CARTIF | `TEF5_CARTIF` |
| REA | `TEF6_REA` |
| ENGREEN | `TEF7_ENGREEN` |

The output goes through gzip into the hidden temp file
`.<PARTNER>.csv.gz.part`. That file is converted in batches into
`.<PARTNER>.parquet.part` (zstd compressed), and the two row counts are
checked against each other. The gzipped CSV is then uploaded to MinIO as
`pilot_datasets/<PARTNER>/<PARTNER>.csv.gz`, and the Parquet file is renamed
with `os.replace()` to `<PARTNER>.parquet` in the shared directory. Any older
`<PARTNER>.csv` or `<PARTNER>.csv.gz` in that directory is deleted. The rename is atomic and happens in the same directory, so a user reading the file
in a running notebook sees either the previous complete export or the new one.

If the MinIO upload fails, the Parquet file is still published and the result
reports the MinIO error.

Exports run one at a time (single worker executor) and start 45 minutes apart
from 01:00 container time, largest partner first.

| 01:00 | 01:45 | 02:30 | 03:15 | 04:00 | 04:45 | 05:30 |
|-------|-------|-------|-------|-------|-------|-------|
| CEDER | RDN | BER | CEA | CARTIF | REA | ENGREEN |

If an export runs past the next slot, the next partner waits in the queue. It
still runs as long as it starts within `PILOT_EXPORT_MISFIRE_GRACE_TIME`.

A failed partner does not stop the others. If a partner's database has no
`public.f_tsdata` table, its export fails and the previous export stays in
place.

The scheduler does not start if `DATALAKE_PASSWORD` is empty. On shutdown it
waits for a running export to finish.

### Startup catch-up

When the scheduler container starts, it queues a one-off export for every
partner whose file is missing or older than `PILOT_EXPORT_MAX_AGE_HOURS`. The
exports start after `PILOT_EXPORT_STARTUP_DELAY_SECONDS`. This way a first
deploy, or a restart after the VM was down overnight, produces data without
waiting for 01:00. Partners with a recent export are skipped, so a routine
restart or redeploy does not export anything.

Catch-up jobs use the same single worker executor as the nightly jobs and never
run at the same time as a scheduled export. Set `PILOT_EXPORT_ON_STARTUP=false`
to turn catch-up off.

Catch-up runs only in the scheduler container. Restarting the API container
(`data-management-server`) does not trigger exports.

### Access

JupyterHub's `pre_spawn_hook` bind-mounts `pilot_datasets/` read-only into
every singleuser container at `/home/jovyan/.pilot`. The symlinks created by
`POST /api/v1/provision/pilot` point into that mount.

JupyterHub users get Parquet files. Every partner's CSV is too large for the
JupyterLab CSV viewer (CEDER is about 9 GB), while Parquet is typed, much
smaller, and can be read one sensor at a time. In a notebook

```python
df = pd.read_parquet('datasets/REA Pilot Data/REA.parquet')

# One sensor only, skipping the rest of the file
pd.read_parquet('datasets/CEDER Pilot Data/CEDER.parquet',
                filters=[('sensor_id', '==', 'ACTARIS')])
```

MinIO, and so the dashboard download, keeps the gzipped CSV.

### File format

Both copies have the same columns. The Parquet types are in brackets.

| Column | Source | Notes |
|--------|--------|-------|
| `datetime` (timestamp, s) | `f_tsdata.calendar_id` | `YYYY-MM-DD HH:MM:SS` in the CSV |
| `sensor_id` (string) | `f_tsdata.sensor_id` | - |
| `values` (float64) | `f_tsdata.f_value` | - |
| `corrected` (bool) | `f_tsdata.corrected` | `true` if the data quality corrector imputed the value, `false` if it was measured |

Rows are sorted by `sensor_id`, then `datetime`. 
time.

## Configuration

Both containers read their configuration from environment variables, loaded
from `.env`.

| Variable | Default | Description |
|----------|---------|-------------|
| `API_KEY` | _(required)_ | Key expected in the `X-API-Key` header |
| `MINIO_ENDPOINT` | `minio-backend.energy-guard.eu` | MinIO hostname |
| `MINIO_ACCESS_KEY` | _(required)_ | MinIO access key |
| `MINIO_SECRET_KEY` | _(required)_ | MinIO secret key |
| `MINIO_SECURE` | `true` | Use HTTPS for MinIO |
| `DATASETS_BUCKET` | `datasets` | MinIO bucket for datasets |
| `NOTEBOOKS_BUCKET` | `notebooks` | MinIO bucket for notebooks |
| `PILOT_PREFIX` | `user_pilot` | Currently unused |
| `PILOT_DATASETS_PREFIX` | `pilot_datasets` | MinIO prefix and shared directory name for the pilot exports. The dashboard and JupyterHub use the same value |
| `PILOT_MOUNT_PATH` | `/home/jovyan/.pilot` | Where pilot data is mounted in singleuser containers. Must match JupyterHub's `pre_spawn_hook` |
| `DATALAKE_HOST` | `_(required)_` | data lake host (this VM's IP is allow-listed) |
| `DATALAKE_PORT` | `_(required)_` | data lake port |
| `DATALAKE_USER` | `_(required)_` | data lake account |
| `DATALAKE_PASSWORD` | _(required for exports)_ | Data lake password |
| `DATALAKE_CONNECT_TIMEOUT` | `30` | Data lake connection timeout in seconds |
| `DATALAKE_STATEMENT_TIMEOUT_MS` | `21600000` | Maximum run time of one partner's query (6 hours) |
| `PILOT_EXPORT_GZIP_LEVEL` | `6` | gzip level for the CSV uploaded to MinIO |
| `PILOT_EXPORT_HOUR` / `PILOT_EXPORT_MINUTE` | `1` / `0` | Start time of the first export of the night |
| `PILOT_EXPORT_STAGGER_MINUTES` | `45` | Gap between the start times of consecutive partners |
| `PILOT_EXPORT_MISFIRE_GRACE_TIME` | `14400` | How many seconds late a queued or missed export may still start. Should be longer than a full CEDER run |
| `PILOT_EXPORT_ON_STARTUP` | `true` | Export missing or stale partners when the scheduler container starts |
| `PILOT_EXPORT_MAX_AGE_HOURS` | `36` | Age in hours after which a partner's export counts as stale. Should be above 24 |
| `PILOT_EXPORT_STARTUP_DELAY_SECONDS` | `60` | Delay before catch-up exports start |
| `JUPYTERHUB_DATA_PATH` | `/jupyterhub_data` | Container path of the shared JupyterHub data directory |
| `LOG_LEVEL` | `INFO` | Logging level |
| `TZ` | `Europe/Athens` | Time zone of the scheduler container, used for the export times. Set in `docker-compose.yaml` |

## Deployment

### 1. Create the shared data directory on the host

```bash
sudo mkdir -p /mnt/datadisk/volumes/jupyterhub_data/datasets \
              /mnt/datadisk/volumes/jupyterhub_data/notebooks \
              /mnt/datadisk/volumes/jupyterhub_data/pilot_datasets
```

### 2. Generate an API key and set it in `.env`

```bash
openssl rand -hex 32
# Paste the result as API_KEY in data_managment_server/.env
```

### 3. Set `DATALAKE_PASSWORD` in `.env`

The export scheduler needs it. `.env` is ignored by git.

### 4. Build and start the services

This starts the API and the `pilot-export-scheduler` container. Both join the
external Docker network `nginxproxy_energyguard_net`, and the API listens on
port 6060. Other containers reach it at `http://data-management-server:6060`.

```bash
cd path/to/data_managment_server
docker compose up -d --build
```

### 5. Restart JupyterHub to pick up the new config and volumes

This adds the read-only `/home/jovyan/.pilot` mount needed for pilot datasets.
Users whose server is already running must restart it once to get the mount.
After that, new pilot datasets appear without a restart.

```bash
cd path/to/energyguard/JupyterHub
docker compose up -d --build
```

### 6. Run the first pilot export

The nightly schedule and the startup catch-up fill in the exports on their own.
To run the first export by hand and follow its progress

```bash
docker compose exec pilot-export-scheduler python -m app.export_cli --all
docker compose logs -f pilot-export-scheduler
```

The CLI also takes one or more partner codes in place of `--all`, prints a
summary table, and exits with a non-zero code if any partner failed.
