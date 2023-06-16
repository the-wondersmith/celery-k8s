"""Test the k8s pool w/ Celery's pytest contrib."""

# Future Imports
from __future__ import annotations

# Standard Library Imports
import asyncio as aio  # noqa: F401
from typing import (  # noqa: F401
    Any,
    Callable,
    Coroutine,
)

# Third-Party Imports
import celery  # noqa: F401
import celery.apps.worker  # noqa: F401
import celery.canvas  # noqa: F401
import celery.contrib.testing.worker  # noqa: F401
import celery.result  # noqa: F401
import pytest  # noqa: F401

__all__ = tuple()
