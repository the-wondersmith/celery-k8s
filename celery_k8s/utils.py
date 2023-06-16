"""Utility functions."""

# Future Imports
from __future__ import annotations

# Standard Library Imports
import atexit
import base64
import binascii
import copy
import importlib as il
import http.client
import inspect
import json
import os
import re
import traceback as tb
import types
from functools import partial
from itertools import repeat
from pathlib import Path
from types import SimpleNamespace
from typing import (
    Any,
    Optional,
    Type,
    TypeVar,
    cast,
)

# Third-Party Imports
import anyio
import celery
import celery.app.amqp
import celery.backends.redis
import celery.exceptions
import celery.states
import celery.worker.request
import kombu.compression
import kombu.serialization
import kombu.transport.pyamqp
import kombu.transport.virtual
import kubernetes as k8s
import yaml
from kombu.serialization import (
    enable_insecure_serializers,
    register_pickle,
)
from dotenv import dotenv_values

try:
    # Third-Party Imports
    import kombu.transport.librabbitmq
except ImportError:
    kombu = kombu or SimpleNamespace()
    kombu.transport = getattr(kombu, "transport", SimpleNamespace())
    kombu.transport.librabbitmq = SimpleNamespace(Message=None)

try:
    # Third-Party Imports
    from loguru import logger as logging
except ImportError:
    # Standard Library Imports
    import logging


__all__ = (
    "get_task_request",
    "run_task_in_k8s_pod",
    "current_k8s_namespace",
)


register_pickle()
enable_insecure_serializers()
register_pickle()


Imported = TypeVar("Imported")
K8sAnnotationKey = "celeryq.dev/task-request"
K8sJobDataFile = Path("/var/run/job-data/annotations")
K8sNamespaceFile = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
Missing = types.new_class(
    "Missing",
    tuple(),
    dict(
        metaclass=type(
            "_",
            (type,),
            dict(
                zip(
                    ("__bool__", "__nonzero__"),
                    repeat(lambda *_: False),
                )
            ),
        )
    ),
)


def _permissive_getattr(self: SimpleNamespace, name: str) -> Optional[Any]:
    """..."""
    try:
        return object.__getattribute__(self, name)
    except (AttributeError, ValueError, KeyError):
        return None


PermissiveObject = type("PermissiveObject", (SimpleNamespace,), {"__getattribute__": _permissive_getattr})


def terminate_istio() -> None:
    """Propagate exit signals to any Istio sidecars present in the current container."""
    try:
        sidecar_port = int(os.getenv("ISTIO_SIDECAR_PORT") or 15020)
        sidecar_host = os.getenv("ISTIO_SIDECAR_HOST") or "127.0.0.1"

        http.client.HTTPConnection(sidecar_host, sidecar_port).request("POST", "/quitquitquit")
        logging.info("istio sidecar termination request sent")
    except Exception:  # noqa
        logging.debug("no istio sidecar attached")


def import_from_dot_path(dot_path: str) -> Imported | Type[Missing]:
    """Import the specified resource."""
    if not dot_path:
        return Missing

    dot_path = dot_path.split(".")
    mod_path, member_name = ".".join(dot_path[:-1]) or dot_path[-1], dot_path[-1]

    if mod_path == member_name:
        member_name = None

    module = il.import_module(mod_path)

    if not member_name:
        return module

    return getattr(module, member_name, Missing)


def current_k8s_namespace() -> str:
    """Get the Kubernetes namespace that is "active" according to the local
    configuration."""

    namespace = None if not K8sNamespaceFile.exists() else K8sNamespaceFile.read_text().strip()

    if namespace:
        return namespace

    _, current_context = k8s.config.list_kube_config_contexts()

    namespace = current_context.get("context", dict()).get("namespace") or os.getenv("K8S_NAMESPACE")

    if namespace:
        return namespace

    raise ValueError


def load_k8s_config() -> bool:
    """Load the appropriate Kubernetes client configuration for the local
    environment."""

    if not k8s.client.configuration.Configuration._default:
        try:
            k8s.config.load_incluster_config()
        except k8s.config.config_exception.ConfigException:
            logging.warning(
                "Could not load in-cluster configuration! "  # prevent-black-formatting
                "Attempting to fall back to local kubectl configuration ..."
            )
            k8s.config.load_kube_config()

    return bool(k8s.client.configuration.Configuration._default)


def snake_case(value: str) -> str:
    """Convert a camel-cased string to snake-case."""
    return (
        re.sub(
            r"([a-z\d])([A-Z])",
            r"\1_\2",
            re.sub(
                "__([A-Z])",
                r"_\1",
                re.sub(
                    "(.)([A-Z][a-z]+)",
                    r"\1_\2",
                    str(value),
                ),
            ),
        )
        .casefold()
        .replace("-", "_")
    )


def task_template_crd() -> k8s.client.V1CustomResourceDefinition:
    """Load the `TaskTemplate` CRD from the packaged YAML file."""
    crd_path = Path(__file__).parent / "manifests" / "task-template-crd.yaml"
    crd = yaml.safe_load(crd_path.read_bytes())
    crd = k8s.client.V1CustomResourceDefinition(
        **{snake_case(key): value for key, value in crd.items()},
    )
    return crd


def reconstruct_request(data: str | bytes | Path | dict[str, Any]) -> celery.worker.request.Request:
    """Reconstruct a Celery task execution request from the supplied JSON
    data."""

    if isinstance(data, Path):
        data = data.read_bytes()

    if isinstance(data, (str, bytes)):
        data = json.loads(data)

    if not isinstance(data, dict):
        raise TypeError

    task = import_from_dot_path(data.pop("task", data.setdefault("headers", dict()).get("task")))

    if task is Missing:
        raise ImportError

    body = data.pop("body", None) or ""
    body = (
        body.encode("ascii")
        if isinstance(body, str)
        else bytes(body)
        if isinstance(body, (list, tuple, bytes))
        else b""
    )

    message = None
    message_cls = import_from_dot_path(data.pop("message_cls", None))
    connection_errors = tuple(
        item
        for cls in data.get("connection_errors", tuple())
        if (item := import_from_dot_path(cls)) is not Missing
    )

    if message_cls is celery.app.amqp.task_message:
        data["message"] = data.get("message") or data

    if (message_data := data.get("message")) and message_cls is not Missing:
        message_data["body"] = (
            bytes(body_data)
            if isinstance(
                (body_data := message_data.pop("body", None)),
                (list, tuple),
            )
            else body_data
        )

        match message_cls:
            case celery.app.amqp.task_message:
                sent_event = message_data.pop("sent_event", None)
                headers = message_data.pop("headers", None) or dict()
                properties = message_data.pop("properties", None) or dict()

                try:
                    body = base64.b64decode(body)
                except binascii.Error:
                    pass

                if compression_type := headers.get("compression"):
                    try:
                        body = kombu.compression.decompress(body, compression_type)
                    except Exception:  # noqa
                        pass

                try:
                    body = kombu.serialization.registry.loads(
                        body,
                        message_data.get("content-type"),
                        message_data.get("content-encoding"),
                    )
                except Exception:  # noqa
                    pass

                message_cls = PermissiveObject
                args, kwargs = (
                    (),
                    {
                        **{
                            snake_case(key): value
                            for (
                                key,
                                value,
                            ) in data.items()
                        },
                        "body": body,
                        "headers": headers,
                        "properties": properties,
                        "sent_event": sent_event,
                    },
                )
                data.update(decoded=True, headers=headers)

            case kombu.transport.pyamqp.Message:
                message_data["delivery_tag"] = message_data.pop("delivery_tag", None)
                message_data["properties"] = message_data.pop("properties", None) or dict()
                message_data["delivery_info"] = message_data.pop("delivery_info", None) or dict()

                args, kwargs = (
                    (
                        SimpleNamespace(**message_data),
                        message_data.pop("channel", None),
                    ),
                    {
                        key: value
                        for key, value in message_data.items()
                        if key
                        not in (
                            "body",
                            "channel",
                            "delivery_tag",
                            "content_type",
                            "content_encoding",
                            "delivery_info",
                            "properties",
                            "headers",
                        )
                    },
                )

            case kombu.transport.librabbitmq.Message:
                channel, props, body, info = (
                    message_data.pop("channel", None),
                    message_data.pop("properties", None),
                    message_data.pop("body", None),
                    copy.deepcopy(message_data),
                )
                args, kwargs = (channel, props, info, body), dict()

            case kombu.transport.virtual.base.Message:
                args, kwargs = (
                    (
                        message_data.pop("payload", None),
                        message_data.pop("channel", None),
                    ),
                    {
                        key: value
                        for key, value in message_data.items()
                        if key
                        not in (
                            "body",
                            "channel",
                            "delivery_tag",
                            "content_type",
                            "content_encoding",
                            "header",
                            "properties",
                            "delivery_info",
                            "postencode",
                        )
                    },
                )

            case _:
                args, kwargs = (), message_data

        message = message_cls(*args, **kwargs)

    if not message:
        raise ValueError(f"Supplied request data contained no usable `message`: {data}")

    elif message_cls is not PermissiveObject:
        message.body = body or message.body

    request = celery.worker.request.Request(
        task=task,
        body=body,
        app=task._app,
        message=message,
        connection_errors=connection_errors,
        **{
            key: value
            for key, value in dict(
                utc=data.get("utc", Missing),
                decoded=data.get("decoded", Missing),
                headers=data.get("headers", Missing),
                hostname=data.get("hostname", Missing),
            ).items()
            if value is not Missing
        },
    )

    logging.info(f"Successfully reconstructed original Celery `Request` for task: {request.id}")

    return request


def get_task_request(
    task_id: Optional[str] = None,
    raw: bool = False,
) -> Optional[celery.worker.request.Request]:
    """Fetch the `ConfigMap` from the local Kubernetes cluster containing the
    serialized data for the supplied task id and use it to reconstruct a Celery
    worker execution request."""

    log_line = "Attempting to fetch original Celery `Request` for task%s" % (
        f": {task_id}" if task_id else ""
    )

    logging.info(log_line)

    data = None

    if K8sJobDataFile.exists():
        try:
            data = dotenv_values(K8sJobDataFile).get(K8sAnnotationKey) or ""
        except Exception as error:  # noqa
            logger, info = (
                (logging.opt(exception=True), dict())
                if hasattr(logging, "opt")
                else (logging, dict(exc_info=error))
            )
            logger.warning(
                "Couldn't load request data as dotenv file, attempting fallback parsing...", **info
            )

            data = next(
                filter(
                    lambda line: line.startswith("celeryq.dev/task-request"),
                    K8sJobDataFile.read_text().splitlines(),
                ),
                "",
            ).split("=")[-1]

        data = yaml.safe_load(data)

    if isinstance(data, str):
        data = yaml.safe_load(data)

    if isinstance(data, dict):
        pass

    elif not isinstance(task_id, str):
        raise ValueError("No `task_id` provided")

    else:
        # Create a Kubernetes API client
        load_k8s_config()
        client = k8s.client.CoreV1Api()

        try:
            config_map: k8s.client.V1ConfigMap = client.read_namespaced_config_map(
                name=f"celery-task-request-{task_id}",
                namespace=current_k8s_namespace(),
            )
        except k8s.client.exceptions.ApiException as error:
            if error.status == 404:
                return None

            raise error

        data = config_map.data or config_map.binary_data

    if raw:
        return cast(celery.worker.request.Request, data)

    if not isinstance(data, dict):
        raise TypeError(f"Expected dict[str, Any], got: {type(data)} -> {data!r}")

    request = reconstruct_request(data=data.get("request") or data)

    return request


def run_task_in_k8s_pod(task: celery.Task, task_id: Optional[str] = None) -> bool:
    """Run a Celery-decorated task inside a Kubernetes pod."""

    atexit.register(terminate_istio)

    request: Optional[celery.worker.request.Request] = None

    if task_id or K8sJobDataFile.exists():
        # Get the original Request object
        request = get_task_request(task_id=task_id)

    if request:
        task.request_stack.push(request)

    run_task = partial(
        task.run,
        *(task.request.args or tuple()),
        **(task.request.kwargs or dict()),
    )

    # Determine how we'll need to run the function
    if inspect.iscoroutinefunction(task.run):
        logging.debug(f"Function for task '{task_id}' is a coroutine")
        run_task = partial(
            anyio.run,
            run_task,
            backend="asyncio",
            backend_options=dict(use_uvloop=True),
        )

    state, result, traceback = celery.states.PENDING, None, None

    try:
        task.backend.store_result(
            result=result,
            request=task.request,
            task_id=task.request.id,
            state=celery.states.STARTED,
        )

        logging.info(f"Task '{task_id}' status changed to 'STARTED' in Celery backend")

        result, state = run_task(), celery.states.SUCCESS

    except Exception as error:
        state, result, traceback = (
            celery.states.FAILURE,
            error,
            tb.format_exc(chain=True, limit=None),
        )
        logger, err, info = (
            (logging.opt(exception=True), str(error), dict())
            if hasattr(logging, "opt")
            else (logging, error, dict(exc_info=error))
        )

        logger.error(err, **info)

    task.backend.store_result(
        state=state,
        result=result,
        traceback=traceback,
        request=task.request,
        task_id=task.request.id,
    )

    logging.info(f"Task '{task_id}' status changed to '{state}' in Celery backend")

    return state is celery.states.SUCCESS
