"""
azure/blob_tags | Azure Blob Storage tag operations: set, get, get single tag | needs:azure-storage-blob | set_blob_tags(),get_blob_tags(),get_blob_tag()
"""
from azure.storage.blob import BlobClient


def set_blob_tags(blob: BlobClient, tags: dict) -> bool:
    """Set tags on a BlobClient instance.

    Converts all key/value pairs to strings before calling the SDK.
    Returns True on success, False on error.
    """
    try:
        str_tags = {str(k): str(v) for k, v in tags.items()}
        blob.set_blob_tags(str_tags)
        return True
    except Exception as e:
        print(f"[blob_tags] set_blob_tags error: {e}")
        return False


def get_blob_tags(blob: BlobClient) -> dict:
    """Return all tags set on a BlobClient instance.

    Returns an empty dict on error.
    """
    try:
        return blob.get_blob_tags()
    except Exception as e:
        print(f"[blob_tags] get_blob_tags error: {e}")
        return {}


def get_blob_tag(blob: BlobClient, key: str) -> str | None:
    """Return the value of a single tag by key, or None if absent or on error."""
    try:
        return blob.get_blob_tags().get(key)
    except Exception as e:
        print(f"[blob_tags] get_blob_tag error: {e}")
        return None
