"""Celery-compatible, bare-bones kombu `Channel` and `Transport` for speaking
directly to the Kubernetes API."""

# Future Imports
from __future__ import annotations

# Standard Library Imports
import copy
import json
import logging
import os
import re
from functools import partial
from queue import Empty
from typing import (
    Any,
    Optional,
)

# Third-Party Imports
import kubernetes as k8s
from kombu.transport.filesystem import (
    Channel,
    Transport,
)
from more_itertools import consume

# Package-Level Imports
from celery_k8s.utils import (
    task_template_crd,
    current_k8s_namespace,
    load_k8s_config,
    K8sJobDataFile,
)


class K8sAccessor(type):
    """Metaclass for types that need access to the Kubernetes API."""

    __client__: Optional[k8s.client.ApiClient] = None

    @property
    def api_client(cls) -> k8s.client.ApiClient:
        """A pseudo-global Kubernetes API client instance."""
        if K8sAccessor.__client__ is None:
            load_k8s_config()

            K8sAccessor.__client__ = k8s.client.ApiClient()

        return K8sAccessor.__client__


class K8sChannel(Channel, metaclass=K8sAccessor):
    """Custom kombu `Channel` for talking to Kubernetes."""

    api_client: k8s.client.ApiClient

    def basic_publish(
        self,
        message: dict[str, Any],
        exchange: str,
        routing_key: str,
        **kwargs: Any,
    ) -> None:
        """Publish message."""
        self._inplace_augment_message(message, exchange, routing_key)

        message.setdefault("task", message["headers"]["task"])
        message.setdefault("message_cls", "celery.app.amqp.task_message")

        self.connection.dispatch_k8s_job(task=message)

    def _get(self, queue: str) -> None:
        """Get next message from `queue`."""
        raise Empty()

    def _purge(self, queue: str) -> None:
        """Remove all messages from `queue`."""
        pass

    def _size(self, queue: str) -> int:
        """Return the number of messages in `queue`."""
        return 0


class K8sTransport(Transport, metaclass=K8sAccessor):
    """Custom kombu `Transport` for talking directly to Kubernetes."""

    # Kombu Attributes
    Channel: type[K8sChannel] = K8sChannel
    default_connection_params: dict[str, Any] = {"namespace": "default"}

    # Class-Specific Attributes
    _crd_exists: bool = False
    _crd_name: str = "tasktemplates.celeryq.dev"
    __templates__: dict[str, Any] = dict()

    _mount_name: str = "job-data"
    _job_data_mount: dict[str, Any] = {
        "name": _mount_name,
        "subPath": str(K8sJobDataFile.name),
        "mountPath": str(K8sJobDataFile),
    }
    _job_data_volume: dict[str, Any] = {
        "name": _mount_name,
        "downwardAPI": {
            "items": [
                {
                    "fieldRef": {
                        "fieldPath": "metadata.annotations",
                    },
                    "path": "annotations",
                },
            ],
        },
    }

    api_client: k8s.client.ApiClient

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if not self.ensure_crd(install=kwargs.pop("install_crds", True)):
            raise RuntimeError("The `TaskTemplate` CRD doesn't exist in the local cluster!")

        super().__init__(*args, **kwargs)

    @classmethod
    def _template_cache(cls, force: bool = False) -> dict[str, Any]:
        """The local cache of `TaskTemplate`s from the current cluster."""
        if force or not cls.__templates__:
            cluster = k8s.client.CustomObjectsApi(api_client=cls.api_client)
            templates = (
                cluster.list_namespaced_custom_object(
                    version="v1alpha1",
                    group="celeryq.dev",
                    plural="tasktemplates",
                    namespace=current_k8s_namespace(),
                )
                or dict()
            ).get("items") or tuple()

            cache = cls.__templates__.__setitem__
            reformat = partial(re.sub, r"(((?<!^)\.{2,})|:+)", ".")

            consume(
                (
                    cache(reformat(template["metadata"]["name"]), template),
                    cache(reformat(template["spec"]["task_path"]), template),
                )
                for template in templates  # map(snake_keys, templates)
            )

        return cls.__templates__

    @classmethod
    def ensure_crd(cls, install: bool = True) -> bool:
        """Check that the `TaskTemplate` CRD exists in the current cluster."""

        cluster: Optional[k8s.client.ApiextensionsV1Api] = None

        if not cls._crd_exists:
            cluster = k8s.client.ApiextensionsV1Api(api_client=cls.api_client)

            try:
                cls._crd_exists = bool(cluster.read_custom_resource_definition(cls._crd_name))
            except k8s.client.exceptions.ApiException:
                if not install:
                    return False

        if install and not cls._crd_exists and cluster:
            crd = task_template_crd()
            cluster.create_custom_resource_definition(body=crd)

            return cls.ensure_crd(install=False)

        return cls._crd_exists

    @staticmethod
    def format_k8s_name(value: str) -> str:
        """Format the supplied string value to meet the Kubernetes `Job` / `Container` name requirements."""
        return re.sub(r"[^a-z0-9.-]", "-", value, flags=re.I)[:63]

    @staticmethod
    def validate_job_template(template: dict[str, Any]) -> bool:
        """Ensure the supplied template does not contain any forbidden
        values."""
        # TODO(the-wondersmith): Implement template validation
        raise NotImplementedError

    def get_job_template(self, task: str) -> Optional[dict[str, Any]]:
        """Retrieve the matching `TaskTemplate` from the current cluster for
        the specified task."""
        if not (template := self._template_cache(force=False).get(task)):
            template = self._template_cache(force=True).get(task) or dict()
        return copy.deepcopy(template.get("spec") or template)

    def dispatch_k8s_job(self, task: dict[str, Any]) -> bool:
        """Dispatch a Kubernetes `Job` for the specified task."""

        task_name = task.get("task", task["headers"]["task"])

        template = self.get_job_template(task=task_name)

        if not template:
            logging.error(
                f"No `TaskTemplate` found in namespace '%s' for task: {task_name}" % current_k8s_namespace()
            )
            return False

        task_id = task["headers"]["id"]
        task_name = task_name.split(".")
        task_path, task_name = ".".join(task_name[:-1]), task_name[-1]

        logging.info(f"Dispatching '{task_name}' as Kubernetes `Job` for task id: {task_id}")

        manifest = k8s.client.V1Job(
            kind="Job",
            spec=template["spec"],
            api_version="batch/v1",
            metadata=template["metadata"],
        )

        metadata = manifest.metadata
        template = manifest.spec.setdefault("template", dict())

        metadata.update(
            namespace=current_k8s_namespace(),
            name=self.format_k8s_name(f"{task_name}-{task_id}"),
        )
        metadata.setdefault("labels", dict())["tags.datadoghq.com/task-id"] = str(task_id)

        metadata = template.setdefault("metadata", dict())
        metadata.setdefault("labels", dict())["tags.datadoghq.com/task-id"] = str(task_id)
        metadata.setdefault("annotations", dict())["celeryq.dev/task-request"] = json.dumps(task)

        spec = template.setdefault("spec", dict())
        spec.setdefault("volumes", list()).append(self._job_data_volume)

        container = (
            next(
                filter(
                    lambda con: con.get("name") == "celery-task",
                    (spec.get("containers") or tuple()),
                ),
                None,
            )
            or dict()
        )
        container["name"] = self.format_k8s_name(task_name)
        container.setdefault("volumeMounts", list()).append(self._job_data_mount)

        task_script = "; ".join(
            (
                "import sys",
                # Import the actual task function so that the
                # "main" Celery app instance is (virtually)
                # guaranteed to also be loaded / initialized
                f"from {task_path} import {task_name}",
                "import celery_k8s",
                f"sys.exit(not celery_k8s.run({task_name}, task_id={task_id!r}))",
            )
        )

        container.pop("args", None)
        container["command"].clear()
        container["command"].extend(("python3", "-c", task_script))

        del container

        cluster = k8s.client.BatchV1Api(api_client=type(self).api_client)

        try:
            cluster.create_namespaced_job(body=manifest, namespace=current_k8s_namespace())
            logging.info(f"Dispatch successful for '{task_name}' as Kubernetes `Job` for task id: {task_id}")
            return True

        except Exception as error:
            err = f"Error dispatching '{task_name}' as Kubernetes `Job` for task id: {task_id}"
            logger, info = (
                (logging.opt(exception=True), dict())
                if hasattr(logging, "opt")
                else (logging, dict(exc_info=error))
            )
            logger.error(err, **info)

            return False
