import json
import os
from typing import Any
from cerebrum.storage.apis import StorageResponse
from aios.storage.filesystem.lsfs import LSFS


class StorageManager:
    """AIOS Kernel Storage Manager backed by LSFS."""

    def __init__(self, root_dir: str = "root_storage", use_vector_db: bool = False, filesystem_type: str = "lsfs"):
        self.use_vector_db = use_vector_db
        self.filesystem_type = filesystem_type
        self.root_dir = root_dir
        os.makedirs(self.root_dir, exist_ok=True)
        self.filesystem = LSFS(root_dir, use_vector_db)

    def address_request(self, agent_request: Any) -> StorageResponse:
        try:
            result = self.filesystem.address_request(agent_request)
            if isinstance(result, (dict, list, tuple)):
                if isinstance(result, (list, tuple)) and len(result) == 0:
                    result_str = "No documents found"
                else:
                    result_str = json.dumps(result, ensure_ascii=False)
            elif result is None:
                result_str = "Operation completed with no result"
            elif result == "":
                result_str = ""
            else:
                result_str = str(result)
            return StorageResponse(response_message=result_str, finished=True, status_code=200)
        except Exception as exc:
            return StorageResponse(
                response_message=str(exc),
                finished=True,
                error=str(exc),
                status_code=500,
            )
