import uuid


def generator_tool_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:12]}"
