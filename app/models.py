from pydantic import BaseModel, Field
from typing import Optional


class ProvisionRequest(BaseModel):
    """Sent by the dashboard backend when a user opens a JupyterHub session."""

    username: str
    # Mapping of ``dataset_minio_path`` -> ``dataset_name`` to provision into
    # the target user's JupyterHub volume.
    #
    # * ``dataset_minio_path`` is the bucket-relative prefix where the dataset
    #   actually lives in MinIO, i.e. ``user_<owner>/<original_dataset_name>``
    #   (the ``user_`` prefix is optional). It never changes.
    # * ``dataset_name`` is the name the user picked for the dataset (possibly
    #   renamed via the dashboard). It is the folder name the dataset will get
    #   under ``/home/jovyan/datasets/`` in JupyterHub.
    datasets: dict[str, str] = Field(default_factory=dict)
    # Notebook file-names to provision.  Pass None to provision ALL notebooks
    # from MinIO.  Pass an empty list to skip notebook provisioning.
    notebooks: Optional[list[str]] = None
    # Set to True to re-download notebooks even when they already exist in the
    # user's directory.
    force_notebook_refresh: bool = False


class DatasetUpdateRequest(BaseModel):
    """Trigger an in-place update of one dataset for every user that has it."""

    #a regular username
    dataset_owner: str
    dataset_name: str


class DatasetInfo(BaseModel):
    owner: str
    name: str
    files: list[str]
    size_bytes: int


class NotebookInfo(BaseModel):
    name: str
    size_bytes: int


class ProvisionResult(BaseModel):
    username: str
    datasets_provisioned: list[str]
    notebooks_provisioned: list[str]
    errors: list[str]


class UpdateResult(BaseModel):
    dataset_owner: str
    dataset_name: str
    users_updated: list[str]
    errors: list[str]
