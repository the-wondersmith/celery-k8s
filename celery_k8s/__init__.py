"""Celery K8s Pool - Celery pool class for using Kubernetes `Job`s as a worker pool."""

# Future Imports
from __future__ import annotations

# Third-Party Imports
import kombu.transport

# Imports From Package Sub-Modules
from .transport import (
    K8sChannel,
    K8sTransport,
)
from .utils import run_task_in_k8s_pod as run

__pkg_name__ = "celery-k8s"
__version__ = "0.1.0-rc.1"  # x-release-please-version

__all__ = (
    "run",
    "K8sChannel",
    "K8sTransport",
)


kombu.transport._transport_cache.update(
    k8s=K8sTransport,
    kubernetes=K8sTransport,
)
