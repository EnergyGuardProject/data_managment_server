from pydantic import BaseModel
from typing import Optional


class ProvisionRequest(BaseModel):
    """Sent by the dashboard backend when a user opens a JupyterHub session."""

    username: str
    # Names of datasets (from any source) to make available in the user's
    # JupyterHub home
    datasets: list[str] = []
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
