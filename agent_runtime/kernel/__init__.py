"""Workflow-scoped kernel services, inspired by AIOS's architectural split.

Independent implementation. No AIOS/Cerebrum runtime dependency or source copy.
All actions retain the existing Worker, capability and resource boundaries.
"""
from .coordinator import KernelCoordinator
from .syscall import service_for

__all__=['KernelCoordinator','service_for']
