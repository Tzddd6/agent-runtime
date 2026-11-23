from aios.hooks.stores._global import (
    global_storage_req_queue,
    global_storage_req_queue_get_message,
    global_storage_req_queue_add_message,
    global_storage_req_queue_is_empty,
)


def useStorageRequestQueue():
    return (
        global_storage_req_queue,
        global_storage_req_queue_get_message,
        global_storage_req_queue_add_message,
        global_storage_req_queue_is_empty,
    )


def useStorageManager(
    root_dir: str = "root_storage",
    use_vector_db: bool = False,
    filesystem_type: str = "lsfs",
):
    from aios.storage.storage import StorageManager
    return StorageManager(
        root_dir=root_dir,
        use_vector_db=use_vector_db,
        filesystem_type=filesystem_type,
    )
