# Project module contract / 项目模块说明
# 职责：管理受控存储、索引和文档；外部文件内容不作为执行指令。
# 主要接口：StorageManager。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
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
