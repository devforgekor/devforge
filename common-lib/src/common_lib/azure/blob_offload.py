"""
Blob offload helper — store large context chunks to Azure Blob Storage.

Integrated into generate_response() in llm_client.py. When context_str exceeds
2000 chars, store_context_blob() is called and the blob path is stored in
InteractionLogs.reference_blob_path via llm_result["blob_path"].
"""
import os
import time

_BLOB_CONN_STR = os.getenv("AZURE_BLOB_CONNECTION_STRING")
_BLOB_CONTAINER = os.getenv("BLOB_ARCHIVE_CONTAINER", "seedling-raw-inbox")


async def store_context_blob(content: str, user_id: str, turn_id: str, label: str = "ctx") -> str | None:
    """Store context chunk to Blob, return path. Returns None if Blob unavailable."""
    if not _BLOB_CONN_STR or len(content) < 2000:
        return None

    try:
        from azure.storage.blob import BlobServiceClient

        ts = time.time()
        blob_name = f"ctx/{user_id}/{turn_id}/{label}_{int(ts)}.txt"

        service = BlobServiceClient.from_connection_string(_BLOB_CONN_STR)
        container = service.get_container_client(_BLOB_CONTAINER)
        try:
            container.create_container()
        except Exception:
            pass

        blob = container.get_blob_client(blob_name)
        blob.upload_blob(content, overwrite=True)
        return blob_name
    except Exception:
        return None
