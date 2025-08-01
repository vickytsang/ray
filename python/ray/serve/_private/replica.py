import asyncio
import concurrent.futures
import functools
import inspect
import logging
import os
import pickle
import threading
import time
import traceback
import warnings
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from importlib import import_module
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Dict,
    Generator,
    Optional,
    Tuple,
    Union,
)

import starlette.responses
from anyio import to_thread
from fastapi import Request
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

import ray
from ray import cloudpickle
from ray._common.utils import get_or_create_event_loop
from ray.actor import ActorClass, ActorHandle
from ray.remote_function import RemoteFunction
from ray.serve import metrics
from ray.serve._private.common import (
    DeploymentID,
    ReplicaID,
    ReplicaQueueLengthInfo,
    RequestMetadata,
    ServeComponentType,
    StreamingHTTPRequest,
    gRPCRequest,
)
from ray.serve._private.config import DeploymentConfig
from ray.serve._private.constants import (
    GRPC_CONTEXT_ARG_NAME,
    HEALTH_CHECK_METHOD,
    RAY_SERVE_COLLECT_AUTOSCALING_METRICS_ON_HANDLE,
    RAY_SERVE_METRICS_EXPORT_INTERVAL_MS,
    RAY_SERVE_REPLICA_AUTOSCALING_METRIC_RECORD_PERIOD_S,
    RAY_SERVE_REQUEST_PATH_LOG_BUFFER_SIZE,
    RAY_SERVE_RUN_SYNC_IN_THREADPOOL,
    RAY_SERVE_RUN_SYNC_IN_THREADPOOL_WARNING,
    RAY_SERVE_RUN_USER_CODE_IN_SEPARATE_THREAD,
    RECONFIGURE_METHOD,
    REQUEST_LATENCY_BUCKETS_MS,
    REQUEST_ROUTING_STATS_METHOD,
    SERVE_CONTROLLER_NAME,
    SERVE_LOGGER_NAME,
    SERVE_NAMESPACE,
)
from ray.serve._private.default_impl import (
    create_replica_impl,
    create_replica_metrics_manager,
)
from ray.serve._private.http_util import (
    ASGIAppReplicaWrapper,
    ASGIArgs,
    ASGIReceiveProxy,
    MessageQueue,
    Response,
)
from ray.serve._private.logging_utils import (
    access_log_msg,
    configure_component_logger,
    configure_component_memory_profiler,
    get_component_logger_file_path,
)
from ray.serve._private.metrics_utils import InMemoryMetricsStore, MetricsPusher
from ray.serve._private.thirdparty.get_asgi_route_name import get_asgi_route_name
from ray.serve._private.utils import (
    Semaphore,
    get_component_file_name,  # noqa: F401
    parse_import_path,
)
from ray.serve._private.version import DeploymentVersion
from ray.serve.config import AutoscalingConfig
from ray.serve.context import _get_in_flight_requests
from ray.serve.deployment import Deployment
from ray.serve.exceptions import (
    BackPressureError,
    DeploymentUnavailableError,
    RayServeException,
)
from ray.serve.schema import LoggingConfig

logger = logging.getLogger(SERVE_LOGGER_NAME)


ReplicaMetadata = Tuple[
    DeploymentConfig,
    DeploymentVersion,
    Optional[float],
    Optional[int],
    Optional[str],
]


def _load_deployment_def_from_import_path(import_path: str) -> Callable:
    module_name, attr_name = parse_import_path(import_path)
    deployment_def = getattr(import_module(module_name), attr_name)

    # For ray or serve decorated class or function, strip to return
    # original body.
    if isinstance(deployment_def, RemoteFunction):
        deployment_def = deployment_def._function
    elif isinstance(deployment_def, ActorClass):
        deployment_def = deployment_def.__ray_metadata__.modified_class
    elif isinstance(deployment_def, Deployment):
        logger.warning(
            f'The import path "{import_path}" contains a '
            "decorated Serve deployment. The decorator's settings "
            "are ignored when deploying via import path."
        )
        deployment_def = deployment_def.func_or_class

    return deployment_def


class ReplicaMetricsManager:
    """Manages metrics for the replica.

    A variety of metrics are managed:
        - Fine-grained metrics are set for every request.
        - Autoscaling statistics are periodically pushed to the controller.
        - Queue length metrics are periodically recorded as user-facing gauges.
    """

    PUSH_METRICS_TO_CONTROLLER_TASK_NAME = "push_metrics_to_controller"
    RECORD_METRICS_TASK_NAME = "record_metrics"
    SET_REPLICA_REQUEST_METRIC_GAUGE_TASK_NAME = "set_replica_request_metric_gauge"

    def __init__(
        self,
        replica_id: ReplicaID,
        event_loop: asyncio.BaseEventLoop,
        autoscaling_config: Optional[AutoscalingConfig],
        ingress: bool,
    ):
        self._replica_id = replica_id
        self._metrics_pusher = MetricsPusher()
        self._metrics_store = InMemoryMetricsStore()
        self._autoscaling_config = autoscaling_config
        self._ingress = ingress
        self._controller_handle = ray.get_actor(
            SERVE_CONTROLLER_NAME, namespace=SERVE_NAMESPACE
        )
        self._num_ongoing_requests = 0

        # If the interval is set to 0, eagerly sets all metrics.
        self._cached_metrics_enabled = RAY_SERVE_METRICS_EXPORT_INTERVAL_MS != 0
        self._cached_metrics_interval_s = RAY_SERVE_METRICS_EXPORT_INTERVAL_MS / 1000

        # Request counter (only set on replica startup).
        self._restart_counter = metrics.Counter(
            "serve_deployment_replica_starts",
            description=(
                "The number of times this replica has been restarted due to failure."
            ),
        )
        self._restart_counter.inc()

        # Per-request metrics.
        self._request_counter = metrics.Counter(
            "serve_deployment_request_counter",
            description=(
                "The number of queries that have been processed in this replica."
            ),
            tag_keys=("route",),
        )
        if self._cached_metrics_enabled:
            self._cached_request_counter = defaultdict(int)

        self._error_counter = metrics.Counter(
            "serve_deployment_error_counter",
            description=(
                "The number of exceptions that have occurred in this replica."
            ),
            tag_keys=("route",),
        )
        if self._cached_metrics_enabled:
            self._cached_error_counter = defaultdict(int)

        # log REQUEST_LATENCY_BUCKET_MS
        logger.debug(f"REQUEST_LATENCY_BUCKETS_MS: {REQUEST_LATENCY_BUCKETS_MS}")
        self._processing_latency_tracker = metrics.Histogram(
            "serve_deployment_processing_latency_ms",
            description="The latency for queries to be processed.",
            boundaries=REQUEST_LATENCY_BUCKETS_MS,
            tag_keys=("route",),
        )
        if self._cached_metrics_enabled:
            self._cached_latencies = defaultdict(deque)

        self._num_ongoing_requests_gauge = metrics.Gauge(
            "serve_replica_processing_queries",
            description="The current number of queries being processed.",
        )

        self.set_autoscaling_config(autoscaling_config)

        if self._cached_metrics_enabled:
            event_loop.create_task(self._report_cached_metrics_forever())

    def _report_cached_metrics(self):
        for route, count in self._cached_request_counter.items():
            self._request_counter.inc(count, tags={"route": route})
        self._cached_request_counter.clear()

        for route, count in self._cached_error_counter.items():
            self._error_counter.inc(count, tags={"route": route})
        self._cached_error_counter.clear()

        for route, latencies in self._cached_latencies.items():
            for latency_ms in latencies:
                self._processing_latency_tracker.observe(
                    latency_ms, tags={"route": route}
                )
        self._cached_latencies.clear()

        self._num_ongoing_requests_gauge.set(self._num_ongoing_requests)

    async def _report_cached_metrics_forever(self):
        assert self._cached_metrics_interval_s > 0

        consecutive_errors = 0
        while True:
            try:
                await asyncio.sleep(self._cached_metrics_interval_s)
                self._report_cached_metrics()
                consecutive_errors = 0
            except Exception:
                logger.exception("Unexpected error reporting metrics.")

                # Exponential backoff starting at 1s and capping at 10s.
                backoff_time_s = min(10, 2**consecutive_errors)
                consecutive_errors += 1
                await asyncio.sleep(backoff_time_s)

    async def shutdown(self):
        """Stop periodic background tasks."""

        await self._metrics_pusher.graceful_shutdown()

    def should_collect_metrics(self) -> bool:
        return (
            not RAY_SERVE_COLLECT_AUTOSCALING_METRICS_ON_HANDLE
            and self._autoscaling_config
        )

    def set_autoscaling_config(self, autoscaling_config: Optional[AutoscalingConfig]):
        """Dynamically update autoscaling config."""

        self._autoscaling_config = autoscaling_config

        if self.should_collect_metrics():
            self._metrics_pusher.start()

            # Push autoscaling metrics to the controller periodically.
            self._metrics_pusher.register_or_update_task(
                self.PUSH_METRICS_TO_CONTROLLER_TASK_NAME,
                self._push_autoscaling_metrics,
                self._autoscaling_config.metrics_interval_s,
            )
            # Collect autoscaling metrics locally periodically.
            self._metrics_pusher.register_or_update_task(
                self.RECORD_METRICS_TASK_NAME,
                self._add_autoscaling_metrics_point,
                min(
                    RAY_SERVE_REPLICA_AUTOSCALING_METRIC_RECORD_PERIOD_S,
                    self._autoscaling_config.metrics_interval_s,
                ),
            )

    def inc_num_ongoing_requests(self, request_metadata: RequestMetadata) -> int:
        """Increment the current total queue length of requests for this replica."""
        self._num_ongoing_requests += 1
        if not self._cached_metrics_enabled:
            self._num_ongoing_requests_gauge.set(self._num_ongoing_requests)

    def dec_num_ongoing_requests(self, request_metadata: RequestMetadata) -> int:
        """Decrement the current total queue length of requests for this replica."""
        self._num_ongoing_requests -= 1
        if not self._cached_metrics_enabled:
            self._num_ongoing_requests_gauge.set(self._num_ongoing_requests)

    def get_num_ongoing_requests(self) -> int:
        """Get current total queue length of requests for this replica."""
        return self._num_ongoing_requests

    def record_request_metrics(self, *, route: str, latency_ms: float, was_error: bool):
        """Records per-request metrics."""
        if self._cached_metrics_enabled:
            self._cached_latencies[route].append(latency_ms)
            if was_error:
                self._cached_error_counter[route] += 1
            else:
                self._cached_request_counter[route] += 1
        else:
            self._processing_latency_tracker.observe(latency_ms, tags={"route": route})
            if was_error:
                self._error_counter.inc(tags={"route": route})
            else:
                self._request_counter.inc(tags={"route": route})

    def _push_autoscaling_metrics(self) -> Dict[str, Any]:
        look_back_period = self._autoscaling_config.look_back_period_s
        self._controller_handle.record_autoscaling_metrics.remote(
            replica_id=self._replica_id,
            window_avg=self._metrics_store.window_average(
                self._replica_id, time.time() - look_back_period
            ),
            send_timestamp=time.time(),
        )

    def _add_autoscaling_metrics_point(self) -> None:
        self._metrics_store.add_metrics_point(
            {self._replica_id: self._num_ongoing_requests},
            time.time(),
        )


StatusCodeCallback = Callable[[str], None]


class ReplicaBase(ABC):
    def __init__(
        self,
        replica_id: ReplicaID,
        deployment_def: Callable,
        init_args: Tuple,
        init_kwargs: Dict,
        deployment_config: DeploymentConfig,
        version: DeploymentVersion,
        ingress: bool,
        route_prefix: str,
    ):
        self._version = version
        self._replica_id = replica_id
        self._deployment_id = replica_id.deployment_id
        self._deployment_config = deployment_config
        self._ingress = ingress
        self._route_prefix = route_prefix
        self._component_name = f"{self._deployment_id.name}"
        if self._deployment_id.app_name:
            self._component_name = (
                f"{self._deployment_id.app_name}_" + self._component_name
            )

        self._component_id = self._replica_id.unique_id
        self._configure_logger_and_profilers(self._deployment_config.logging_config)
        self._event_loop = get_or_create_event_loop()

        self._user_callable_wrapper = UserCallableWrapper(
            deployment_def,
            init_args,
            init_kwargs,
            deployment_id=self._deployment_id,
            run_sync_methods_in_threadpool=RAY_SERVE_RUN_SYNC_IN_THREADPOOL,
            run_user_code_in_separate_thread=RAY_SERVE_RUN_USER_CODE_IN_SEPARATE_THREAD,
            local_testing_mode=False,
        )
        self._semaphore = Semaphore(lambda: self.max_ongoing_requests)

        # Guards against calling the user's callable constructor multiple times.
        self._user_callable_initialized = False
        self._user_callable_initialized_lock = asyncio.Lock()
        self._initialization_latency: Optional[float] = None

        # Flipped to `True` when health checks pass and `False` when they fail. May be
        # used by replica subclass implementations.
        self._healthy = False
        # Flipped to `True` once graceful shutdown is initiated. May be used by replica
        # subclass implementations.
        self._shutting_down = False

        # Will be populated with the wrapped ASGI app if the user callable is an
        # `ASGIAppReplicaWrapper` (i.e., they are using the FastAPI integration).
        self._user_callable_asgi_app: Optional[ASGIApp] = None

        # Set metadata for logs and metrics.
        # servable_object will be populated in `initialize_and_get_metadata`.
        self._set_internal_replica_context(servable_object=None)

        self._metrics_manager = create_replica_metrics_manager(
            replica_id=replica_id,
            event_loop=self._event_loop,
            autoscaling_config=self._deployment_config.autoscaling_config,
            ingress=ingress,
        )

        self._port: Optional[int] = None
        self._docs_path: Optional[str] = None

    @property
    def max_ongoing_requests(self) -> int:
        return self._deployment_config.max_ongoing_requests

    def get_num_ongoing_requests(self) -> int:
        return self._metrics_manager.get_num_ongoing_requests()

    def get_metadata(self) -> ReplicaMetadata:
        return (
            self._version.deployment_config,
            self._version,
            self._initialization_latency,
            self._port,
            self._docs_path,
        )

    def _set_internal_replica_context(self, *, servable_object: Callable = None):
        ray.serve.context._set_internal_replica_context(
            replica_id=self._replica_id,
            servable_object=servable_object,
            _deployment_config=self._deployment_config,
        )

    def _configure_logger_and_profilers(
        self, logging_config: Union[None, Dict, LoggingConfig]
    ):

        if logging_config is None:
            logging_config = {}
        if isinstance(logging_config, dict):
            logging_config = LoggingConfig(**logging_config)

        configure_component_logger(
            component_type=ServeComponentType.REPLICA,
            component_name=self._component_name,
            component_id=self._component_id,
            logging_config=logging_config,
            buffer_size=RAY_SERVE_REQUEST_PATH_LOG_BUFFER_SIZE,
        )
        configure_component_memory_profiler(
            component_type=ServeComponentType.REPLICA,
            component_name=self._component_name,
            component_id=self._component_id,
        )

    def _can_accept_request(self, request_metadata: RequestMetadata) -> bool:
        # This replica gates concurrent request handling with an asyncio.Semaphore.
        # Each in-flight request acquires the semaphore. When the number of ongoing
        # requests reaches max_ongoing_requests, the semaphore becomes locked.
        # A new request can be accepted if the semaphore is currently unlocked.
        return not self._semaphore.locked()

    def _maybe_get_http_route(
        self, request_metadata: RequestMetadata, request_args: Tuple[Any]
    ) -> Optional[str]:
        """Get the matched route string for ASGI apps to be used in logs & metrics.

        If this replica does not wrap an ASGI app or there is no matching for the
        request, returns the existing route from the request metadata.
        """
        route = request_metadata.route
        if self._user_callable_asgi_app is not None:
            req: StreamingHTTPRequest = request_args[0]
            try:
                matched_route = get_asgi_route_name(
                    self._user_callable_asgi_app, req.asgi_scope
                )
            except Exception:
                matched_route = None
                logger.exception(
                    "Failed unexpectedly trying to get route name for request. "
                    "Routes in metric tags and log messages may be inaccurate. "
                    "Please file a GitHub issue containing this traceback."
                )

            # If there is no match in the ASGI app, don't overwrite the route_prefix
            # from the proxy.
            if matched_route is not None:
                route = matched_route

        return route

    @contextmanager
    def _handle_errors_and_metrics(
        self, request_metadata: RequestMetadata
    ) -> Generator[StatusCodeCallback, None, None]:
        start_time = time.time()
        user_exception = None

        status_code = None

        def _status_code_callback(s: str):
            nonlocal status_code
            status_code = s

        try:
            yield _status_code_callback
        except asyncio.CancelledError as e:
            user_exception = e
            self._on_request_cancelled(request_metadata, e)
        except Exception as e:
            user_exception = e
            logger.exception("Request failed.")
            self._on_request_failed(request_metadata, e)

        latency_ms = (time.time() - start_time) * 1000
        self._record_errors_and_metrics(
            user_exception, status_code, latency_ms, request_metadata
        )

        if user_exception is not None:
            raise user_exception from None

    def _record_errors_and_metrics(
        self,
        user_exception: Optional[BaseException],
        status_code: Optional[str],
        latency_ms: float,
        request_metadata: RequestMetadata,
    ):
        http_method = request_metadata._http_method
        http_route = request_metadata.route
        call_method = request_metadata.call_method
        if user_exception is None:
            status_str = "OK"
        elif isinstance(user_exception, asyncio.CancelledError):
            status_str = "CANCELLED"
        else:
            status_str = "ERROR"

        # Set in _wrap_request.
        logger.info(
            access_log_msg(
                method=http_method or "CALL",
                route=http_route or call_method,
                # Prefer the HTTP status code if it was populated.
                status=status_code or status_str,
                latency_ms=latency_ms,
            ),
            extra={"serve_access_log": True},
        )
        self._metrics_manager.record_request_metrics(
            route=http_route,
            latency_ms=latency_ms,
            was_error=user_exception is not None,
        )

    def _unpack_proxy_args(
        self,
        request_metadata: RequestMetadata,
        request_args: Tuple[Any],
        request_kwargs: Dict[str, Any],
    ):
        if request_metadata.is_http_request:
            assert len(request_args) == 1 and isinstance(
                request_args[0], StreamingHTTPRequest
            )
            request: StreamingHTTPRequest = request_args[0]
            scope = request.asgi_scope
            receive = ASGIReceiveProxy(
                scope, request_metadata, request.receive_asgi_messages
            )

            request_metadata._http_method = scope.get("method", "WS")
            request_metadata.route = self._maybe_get_http_route(
                request_metadata, request_args
            )

            request_args = (scope, receive)
        elif request_metadata.is_grpc_request:
            assert len(request_args) == 1 and isinstance(request_args[0], gRPCRequest)
            request: gRPCRequest = request_args[0]

            method_info = self._user_callable_wrapper.get_user_method_info(
                request_metadata.call_method
            )
            request_args = (request.user_request_proto,)
            request_kwargs = (
                {GRPC_CONTEXT_ARG_NAME: request_metadata.grpc_context}
                if method_info.takes_grpc_context_kwarg
                else {}
            )

        return request_args, request_kwargs

    async def handle_request(
        self, request_metadata: RequestMetadata, *request_args, **request_kwargs
    ) -> Tuple[bytes, Any]:
        request_args, request_kwargs = self._unpack_proxy_args(
            request_metadata, request_args, request_kwargs
        )
        with self._wrap_request(request_metadata):
            async with self._start_request(request_metadata):
                return await self._user_callable_wrapper.call_user_method(
                    request_metadata, request_args, request_kwargs
                )

    async def handle_request_streaming(
        self, request_metadata: RequestMetadata, *request_args, **request_kwargs
    ) -> AsyncGenerator[Any, None]:
        """Generator that is the entrypoint for all `stream=True` handle calls."""
        request_args, request_kwargs = self._unpack_proxy_args(
            request_metadata, request_args, request_kwargs
        )
        with self._wrap_request(request_metadata) as status_code_callback:
            async with self._start_request(request_metadata):
                if request_metadata.is_http_request:
                    scope, receive = request_args
                    async for msgs in self._user_callable_wrapper.call_http_entrypoint(
                        request_metadata,
                        status_code_callback,
                        scope,
                        receive,
                    ):
                        yield pickle.dumps(msgs)
                else:
                    async for result in self._user_callable_wrapper.call_user_generator(
                        request_metadata,
                        request_args,
                        request_kwargs,
                    ):
                        yield result

    async def handle_request_with_rejection(
        self, request_metadata: RequestMetadata, *request_args, **request_kwargs
    ):
        # Check if the replica has capacity for the request.
        if not self._can_accept_request(request_metadata):
            limit = self.max_ongoing_requests
            logger.warning(
                f"Replica at capacity of max_ongoing_requests={limit}, "
                f"rejecting request {request_metadata.request_id}.",
                extra={"log_to_stderr": False},
            )
            yield ReplicaQueueLengthInfo(False, self.get_num_ongoing_requests())
            return

        request_args, request_kwargs = self._unpack_proxy_args(
            request_metadata, request_args, request_kwargs
        )
        with self._wrap_request(request_metadata) as status_code_callback:
            async with self._start_request(request_metadata):
                yield ReplicaQueueLengthInfo(
                    accepted=True,
                    # NOTE(edoakes): `_wrap_request` will increment the number
                    # of ongoing requests to include this one, so re-fetch the value.
                    num_ongoing_requests=self.get_num_ongoing_requests(),
                )

                if request_metadata.is_http_request:
                    scope, receive = request_args
                    async for msgs in self._user_callable_wrapper.call_http_entrypoint(
                        request_metadata,
                        status_code_callback,
                        scope,
                        receive,
                    ):
                        yield pickle.dumps(msgs)
                elif request_metadata.is_streaming:
                    async for result in self._user_callable_wrapper.call_user_generator(
                        request_metadata,
                        request_args,
                        request_kwargs,
                    ):
                        yield result
                else:
                    yield await self._user_callable_wrapper.call_user_method(
                        request_metadata, request_args, request_kwargs
                    )

    @abstractmethod
    async def _on_initialized(self):
        raise NotImplementedError

    async def initialize(self, deployment_config: DeploymentConfig):
        try:
            # Ensure that initialization is only performed once.
            # When controller restarts, it will call this method again.
            async with self._user_callable_initialized_lock:
                self._initialization_start_time = time.time()
                if not self._user_callable_initialized:
                    self._user_callable_asgi_app = (
                        await self._user_callable_wrapper.initialize_callable()
                    )
                    if self._user_callable_asgi_app:
                        self._docs_path = (
                            self._user_callable_wrapper._callable.docs_path
                        )
                    await self._on_initialized()
                    self._user_callable_initialized = True

                if deployment_config:
                    await self._user_callable_wrapper.set_sync_method_threadpool_limit(
                        deployment_config.max_ongoing_requests
                    )
                    await self._user_callable_wrapper.call_reconfigure(
                        deployment_config.user_config
                    )

            # A new replica should not be considered healthy until it passes
            # an initial health check. If an initial health check fails,
            # consider it an initialization failure.
            await self.check_health()
        except Exception:
            raise RuntimeError(traceback.format_exc()) from None

    async def reconfigure(self, deployment_config: DeploymentConfig):
        try:
            user_config_changed = (
                deployment_config.user_config != self._deployment_config.user_config
            )
            logging_config_changed = (
                deployment_config.logging_config
                != self._deployment_config.logging_config
            )
            self._deployment_config = deployment_config
            self._version = DeploymentVersion.from_deployment_version(
                self._version, deployment_config
            )

            self._metrics_manager.set_autoscaling_config(
                deployment_config.autoscaling_config
            )
            if logging_config_changed:
                self._configure_logger_and_profilers(deployment_config.logging_config)

            await self._user_callable_wrapper.set_sync_method_threadpool_limit(
                deployment_config.max_ongoing_requests
            )
            if user_config_changed:
                await self._user_callable_wrapper.call_reconfigure(
                    deployment_config.user_config
                )

            # We need to update internal replica context to reflect the new
            # deployment_config.
            self._set_internal_replica_context(
                servable_object=self._user_callable_wrapper.user_callable
            )
        except Exception:
            raise RuntimeError(traceback.format_exc()) from None

    @abstractmethod
    def _on_request_cancelled(
        self, request_metadata: RequestMetadata, e: asyncio.CancelledError
    ):
        pass

    @abstractmethod
    def _on_request_failed(self, request_metadata: RequestMetadata, e: Exception):
        pass

    @abstractmethod
    @contextmanager
    def _wrap_request(
        self, request_metadata: RequestMetadata
    ) -> Generator[StatusCodeCallback, None, None]:
        pass

    @asynccontextmanager
    async def _start_request(self, request_metadata: RequestMetadata):
        async with self._semaphore:
            try:
                self._metrics_manager.inc_num_ongoing_requests(request_metadata)
                yield
            finally:
                self._metrics_manager.dec_num_ongoing_requests(request_metadata)

    async def _drain_ongoing_requests(self):
        """Wait for any ongoing requests to finish.

        Sleep for a grace period before the first time we check the number of ongoing
        requests to allow the notification to remove this replica to propagate to
        callers first.
        """
        wait_loop_period_s = self._deployment_config.graceful_shutdown_wait_loop_s
        while True:
            await asyncio.sleep(wait_loop_period_s)

            num_ongoing_requests = self._metrics_manager.get_num_ongoing_requests()
            if num_ongoing_requests > 0:
                logger.info(
                    f"Waiting for an additional {wait_loop_period_s}s to shut down "
                    f"because there are {num_ongoing_requests} ongoing requests."
                )
            else:
                logger.info(
                    "Graceful shutdown complete; replica exiting.",
                    extra={"log_to_stderr": False},
                )
                break

    async def shutdown(self):
        try:
            await self._user_callable_wrapper.call_destructor()
        except:  # noqa: E722
            # We catch a blanket exception since the constructor may still be
            # running, so instance variables used by the destructor may not exist.
            if self._user_callable_initialized:
                logger.exception(
                    "__del__ ran before replica finished initializing, and "
                    "raised an exception."
                )
            else:
                logger.exception("__del__ raised an exception.")

        await self._metrics_manager.shutdown()

    async def perform_graceful_shutdown(self):
        self._shutting_down = True

        # If the replica was never initialized it never served traffic, so we
        # can skip the wait period.
        if self._user_callable_initialized:
            await self._drain_ongoing_requests()

        await self.shutdown()

    async def check_health(self):
        try:
            # If there's no user-defined health check, nothing runs on the user code event
            # loop and no future is returned.
            f = self._user_callable_wrapper.call_user_health_check()
            if f is not None:
                await f
            self._healthy = True
        except Exception as e:
            logger.warning("Replica health check failed.")
            self._healthy = False
            raise e from None

    async def record_routing_stats(self) -> Dict[str, Any]:
        try:
            f = self._user_callable_wrapper.call_user_record_routing_stats()
            if f is not None:
                return await f
            return {}
        except Exception as e:
            logger.warning("Replica record routing stats failed.")
            raise e from None


class Replica(ReplicaBase):
    async def _on_initialized(self):
        self._set_internal_replica_context(
            servable_object=self._user_callable_wrapper.user_callable
        )

        # Save the initialization latency if the replica is initializing
        # for the first time.
        if self._initialization_latency is None:
            self._initialization_latency = time.time() - self._initialization_start_time

    def _on_request_cancelled(
        self, metadata: RequestMetadata, e: asyncio.CancelledError
    ):
        """Recursively cancels child requests."""
        requests_pending_assignment = (
            ray.serve.context._get_requests_pending_assignment(
                metadata.internal_request_id
            )
        )
        for task in requests_pending_assignment.values():
            task.cancel()

        # Cancel child requests that have already been assigned.
        in_flight_requests = _get_in_flight_requests(metadata.internal_request_id)
        for replica_result in in_flight_requests.values():
            replica_result.cancel()

    def _on_request_failed(self, request_metadata: RequestMetadata, e: Exception):
        if ray.util.pdb._is_ray_debugger_post_mortem_enabled():
            ray.util.pdb._post_mortem()

    @contextmanager
    def _wrap_request(
        self, request_metadata: RequestMetadata
    ) -> Generator[StatusCodeCallback, None, None]:
        """Context manager that wraps user method calls.

        1) Sets the request context var with appropriate metadata.
        2) Records the access log message (if not disabled).
        3) Records per-request metrics via the metrics manager.
        """
        ray.serve.context._serve_request_context.set(
            ray.serve.context._RequestContext(
                route=request_metadata.route,
                request_id=request_metadata.request_id,
                _internal_request_id=request_metadata.internal_request_id,
                app_name=self._deployment_id.app_name,
                multiplexed_model_id=request_metadata.multiplexed_model_id,
                grpc_context=request_metadata.grpc_context,
            )
        )

        with self._handle_errors_and_metrics(request_metadata) as status_code_callback:
            yield status_code_callback


class ReplicaActor:
    """Actor definition for replicas of Ray Serve deployments.

    This class defines the interface that the controller and deployment handles
    (i.e., from proxies and other replicas) use to interact with a replica.

    All interaction with the user-provided callable is done via the
    `UserCallableWrapper` class.
    """

    async def __init__(
        self,
        replica_id: ReplicaID,
        serialized_deployment_def: bytes,
        serialized_init_args: bytes,
        serialized_init_kwargs: bytes,
        deployment_config_proto_bytes: bytes,
        version: DeploymentVersion,
        ingress: bool,
        route_prefix: str,
    ):
        deployment_config = DeploymentConfig.from_proto_bytes(
            deployment_config_proto_bytes
        )
        deployment_def = cloudpickle.loads(serialized_deployment_def)
        if isinstance(deployment_def, str):
            deployment_def = _load_deployment_def_from_import_path(deployment_def)
        self._replica_impl: ReplicaBase = create_replica_impl(
            replica_id=replica_id,
            deployment_def=deployment_def,
            init_args=cloudpickle.loads(serialized_init_args),
            init_kwargs=cloudpickle.loads(serialized_init_kwargs),
            deployment_config=deployment_config,
            version=version,
            ingress=ingress,
            route_prefix=route_prefix,
        )

    def push_proxy_handle(self, handle: ActorHandle):
        # NOTE(edoakes): it's important to call a method on the proxy handle to
        # initialize its state in the C++ core worker.
        handle.pong.remote()

    def get_num_ongoing_requests(self) -> int:
        """Fetch the number of ongoing requests at this replica (queue length).

        This runs on a separate thread (using a Ray concurrency group) so it will
        not be blocked by user code.
        """
        return self._replica_impl.get_num_ongoing_requests()

    async def is_allocated(self) -> str:
        """poke the replica to check whether it's alive.

        When calling this method on an ActorHandle, it will complete as
        soon as the actor has started running. We use this mechanism to
        detect when a replica has been allocated a worker slot.
        At this time, the replica can transition from PENDING_ALLOCATION
        to PENDING_INITIALIZATION startup state.

        Returns:
            The PID, actor ID, node ID, node IP, and log filepath id of the replica.
        """

        return (
            os.getpid(),
            ray.get_runtime_context().get_actor_id(),
            ray.get_runtime_context().get_worker_id(),
            ray.get_runtime_context().get_node_id(),
            ray.util.get_node_ip_address(),
            ray.util.get_node_instance_id(),
            get_component_logger_file_path(),
        )

    async def initialize_and_get_metadata(
        self, deployment_config: DeploymentConfig = None, _after: Optional[Any] = None
    ) -> ReplicaMetadata:
        """Handles initializing the replica.

        Returns: 5-tuple containing
            1. DeploymentConfig of the replica
            2. DeploymentVersion of the replica
            3. Initialization duration in seconds
            4. Port
            5. FastAPI `docs_path`, if relevant (i.e. this is an ingress deployment integrated with FastAPI).
        """
        # Unused `_after` argument is for scheduling: passing an ObjectRef
        # allows delaying this call until after the `_after` call has returned.
        await self._replica_impl.initialize(deployment_config)
        return self._replica_impl.get_metadata()

    async def check_health(self):
        await self._replica_impl.check_health()

    async def record_routing_stats(self) -> Dict[str, Any]:
        return await self._replica_impl.record_routing_stats()

    async def reconfigure(self, deployment_config) -> ReplicaMetadata:
        await self._replica_impl.reconfigure(deployment_config)
        return self._replica_impl.get_metadata()

    def _preprocess_request_args(
        self,
        pickled_request_metadata: bytes,
        request_args: Tuple[Any],
    ) -> Tuple[RequestMetadata, Tuple[Any]]:
        request_metadata = pickle.loads(pickled_request_metadata)
        if request_metadata.is_http_request or request_metadata.is_grpc_request:
            request_args = (pickle.loads(request_args[0]),)

        return request_metadata, request_args

    async def handle_request(
        self,
        pickled_request_metadata: bytes,
        *request_args,
        **request_kwargs,
    ) -> Tuple[bytes, Any]:
        """Entrypoint for `stream=False` calls."""
        request_metadata, request_args = self._preprocess_request_args(
            pickled_request_metadata, request_args
        )
        result = await self._replica_impl.handle_request(
            request_metadata, *request_args, **request_kwargs
        )
        if request_metadata.is_grpc_request:
            result = (request_metadata.grpc_context, result.SerializeToString())

        return result

    async def handle_request_streaming(
        self,
        pickled_request_metadata: bytes,
        *request_args,
        **request_kwargs,
    ) -> AsyncGenerator[Any, None]:
        """Generator that is the entrypoint for all `stream=True` handle calls."""
        request_metadata, request_args = self._preprocess_request_args(
            pickled_request_metadata, request_args
        )
        async for result in self._replica_impl.handle_request_streaming(
            request_metadata, *request_args, **request_kwargs
        ):
            if request_metadata.is_grpc_request:
                result = (request_metadata.grpc_context, result.SerializeToString())

            yield result

    async def handle_request_with_rejection(
        self,
        pickled_request_metadata: bytes,
        *request_args,
        **request_kwargs,
    ) -> AsyncGenerator[Any, None]:
        """Entrypoint for all requests with strict max_ongoing_requests enforcement.

        The first response from this generator is always a system message indicating
        if the request was accepted (the replica has capacity for the request) or
        rejected (the replica is already at max_ongoing_requests).

        For non-streaming requests, there will only be one more message, the unary
        result of the user request handler.

        For streaming requests, the subsequent messages will be the results of the
        user request handler (which must be a generator).
        """
        request_metadata, request_args = self._preprocess_request_args(
            pickled_request_metadata, request_args
        )
        async for result in self._replica_impl.handle_request_with_rejection(
            request_metadata, *request_args, **request_kwargs
        ):
            if isinstance(result, ReplicaQueueLengthInfo):
                yield pickle.dumps(result)
            else:
                if request_metadata.is_grpc_request:
                    result = (request_metadata.grpc_context, result.SerializeToString())

                yield result

    async def handle_request_from_java(
        self,
        proto_request_metadata: bytes,
        *request_args,
        **request_kwargs,
    ) -> Any:
        from ray.serve.generated.serve_pb2 import (
            RequestMetadata as RequestMetadataProto,
        )

        proto = RequestMetadataProto.FromString(proto_request_metadata)
        request_metadata: RequestMetadata = RequestMetadata(
            request_id=proto.request_id,
            internal_request_id=proto.internal_request_id,
            call_method=proto.call_method,
            multiplexed_model_id=proto.multiplexed_model_id,
            route=proto.route,
        )
        return await self._replica_impl.handle_request(
            request_metadata, *request_args, **request_kwargs
        )

    async def perform_graceful_shutdown(self):
        await self._replica_impl.perform_graceful_shutdown()


@dataclass
class UserMethodInfo:
    """Wrapper for a user method and its relevant metadata."""

    callable: Callable
    name: str
    is_asgi_app: bool
    takes_any_args: bool
    takes_grpc_context_kwarg: bool

    @classmethod
    def from_callable(cls, c: Callable, *, is_asgi_app: bool) -> "UserMethodInfo":
        params = inspect.signature(c).parameters
        return cls(
            callable=c,
            name=c.__name__,
            is_asgi_app=is_asgi_app,
            takes_any_args=len(params) > 0,
            takes_grpc_context_kwarg=GRPC_CONTEXT_ARG_NAME in params,
        )


class UserCallableWrapper:
    """Wraps a user-provided callable that is used to handle requests to a replica."""

    service_unavailable_exceptions = (BackPressureError, DeploymentUnavailableError)

    def __init__(
        self,
        deployment_def: Callable,
        init_args: Tuple,
        init_kwargs: Dict,
        *,
        deployment_id: DeploymentID,
        run_sync_methods_in_threadpool: bool,
        run_user_code_in_separate_thread: bool,
        local_testing_mode: bool,
    ):
        if not (inspect.isfunction(deployment_def) or inspect.isclass(deployment_def)):
            raise TypeError(
                "deployment_def must be a function or class. Instead, its type was "
                f"{type(deployment_def)}."
            )

        self._deployment_def = deployment_def
        self._init_args = init_args
        self._init_kwargs = init_kwargs
        self._is_function = inspect.isfunction(deployment_def)
        self._deployment_id = deployment_id
        self._local_testing_mode = local_testing_mode
        self._destructor_called = False
        self._run_sync_methods_in_threadpool = run_sync_methods_in_threadpool
        self._run_user_code_in_separate_thread = run_user_code_in_separate_thread
        self._warned_about_sync_method_change = False
        self._cached_user_method_info: Dict[str, UserMethodInfo] = {}

        # Will be populated in `initialize_callable`.
        self._callable = None

        if self._run_user_code_in_separate_thread:
            # All interactions with user code run on this loop to avoid blocking the
            # replica's main event loop.
            self._user_code_event_loop: asyncio.AbstractEventLoop = (
                asyncio.new_event_loop()
            )

            def _run_user_code_event_loop():
                # Required so that calls to get the current running event loop work
                # properly in user code.
                asyncio.set_event_loop(self._user_code_event_loop)
                self._user_code_event_loop.run_forever()

            self._user_code_event_loop_thread = threading.Thread(
                daemon=True,
                target=_run_user_code_event_loop,
            )
            self._user_code_event_loop_thread.start()
        else:
            self._user_code_event_loop = asyncio.get_running_loop()

    @property
    def event_loop(self) -> asyncio.AbstractEventLoop:
        return self._user_code_event_loop

    def _run_user_code(f: Callable) -> Callable:
        """Decorator to run a coroutine method on the user code event loop.

        The method will be modified to be a sync function that returns a
        `asyncio.Future` if user code is running in a separate event loop.
        Otherwise, it will return the coroutine directly.
        """
        assert inspect.iscoroutinefunction(
            f
        ), "_run_user_code can only be used on coroutine functions."

        @functools.wraps(f)
        def wrapper(self, *args, **kwargs) -> Any:
            coro = f(self, *args, **kwargs)
            if self._run_user_code_in_separate_thread:
                fut = asyncio.run_coroutine_threadsafe(coro, self._user_code_event_loop)
                if self._local_testing_mode:
                    return fut

                return asyncio.wrap_future(fut)
            else:
                return coro

        return wrapper

    @_run_user_code
    async def set_sync_method_threadpool_limit(self, limit: int):
        # NOTE(edoakes): the limit is thread local, so this must
        # be run on the user code event loop.
        to_thread.current_default_thread_limiter().total_tokens = limit

    def get_user_method_info(self, method_name: str) -> UserMethodInfo:
        """Get UserMethodInfo for the provided call method name.

        This method is cached to avoid repeated expensive calls to `inspect.signature`.
        """
        if method_name in self._cached_user_method_info:
            return self._cached_user_method_info[method_name]

        if self._is_function:
            user_method = self._callable
        elif hasattr(self._callable, method_name):
            user_method = getattr(self._callable, method_name)
        else:
            # Filter to methods that don't start with '__' prefix.
            def callable_method_filter(attr):
                if attr.startswith("__"):
                    return False
                elif not callable(getattr(self._callable, attr)):
                    return False

                return True

            methods = list(filter(callable_method_filter, dir(self._callable)))
            raise RayServeException(
                f"Tried to call a method '{method_name}' "
                "that does not exist. Available methods: "
                f"{methods}."
            )

        info = UserMethodInfo.from_callable(
            user_method,
            is_asgi_app=isinstance(self._callable, ASGIAppReplicaWrapper),
        )
        self._cached_user_method_info[method_name] = info
        return info

    async def _send_user_result_over_asgi(
        self,
        result: Any,
        asgi_args: ASGIArgs,
    ):
        """Handle the result from user code and send it over the ASGI interface.

        If the result is already a Response type, it is sent directly. Otherwise, it
        is converted to a custom Response type that handles serialization for
        common Python objects.
        """
        scope, receive, send = asgi_args.to_args_tuple()
        if isinstance(result, starlette.responses.Response):
            await result(scope, receive, send)
        else:
            await Response(result).send(scope, receive, send)

    async def _call_func_or_gen(
        self,
        callable: Callable,
        *,
        args: Optional[Tuple[Any]] = None,
        kwargs: Optional[Dict[str, Any]] = None,
        is_streaming: bool = False,
        generator_result_callback: Optional[Callable] = None,
        run_sync_methods_in_threadpool_override: Optional[bool] = None,
    ) -> Tuple[Any, bool]:
        """Call the callable with the provided arguments.

        This is a convenience wrapper that will work for `def`, `async def`,
        generator, and async generator functions.

        Returns the result and a boolean indicating if the result was a sync generator
        that has already been consumed.
        """
        sync_gen_consumed = False
        args = args if args is not None else tuple()
        kwargs = kwargs if kwargs is not None else dict()
        run_sync_in_threadpool = (
            self._run_sync_methods_in_threadpool
            if run_sync_methods_in_threadpool_override is None
            else run_sync_methods_in_threadpool_override
        )
        is_sync_method = (
            inspect.isfunction(callable) or inspect.ismethod(callable)
        ) and not (
            inspect.iscoroutinefunction(callable)
            or inspect.isasyncgenfunction(callable)
        )

        if is_sync_method and run_sync_in_threadpool:
            is_generator = inspect.isgeneratorfunction(callable)
            if is_generator:
                sync_gen_consumed = True
                if not is_streaming:
                    # TODO(edoakes): make this check less redundant with the one in
                    # _handle_user_method_result.
                    raise TypeError(
                        f"Method '{callable.__name__}' returned a generator. "
                        "You must use `handle.options(stream=True)` to call "
                        "generators on a deployment."
                    )

            def run_callable():
                result = callable(*args, **kwargs)
                if is_generator:
                    for r in result:
                        generator_result_callback(r)

                    result = None

                return result

            # NOTE(edoakes): we use anyio.to_thread here because it's what Starlette
            # uses (and therefore FastAPI too). The max size of the threadpool is
            # set to max_ongoing_requests in the replica wrapper.
            # anyio.to_thread propagates ContextVars to the worker thread automatically.
            result = await to_thread.run_sync(run_callable)
        else:
            if (
                is_sync_method
                and not self._warned_about_sync_method_change
                and run_sync_methods_in_threadpool_override is None
            ):
                self._warned_about_sync_method_change = True
                warnings.warn(
                    RAY_SERVE_RUN_SYNC_IN_THREADPOOL_WARNING.format(
                        method_name=callable.__name__,
                    )
                )

            result = callable(*args, **kwargs)
            if inspect.iscoroutine(result):
                result = await result

        return result, sync_gen_consumed

    @property
    def user_callable(self) -> Optional[Callable]:
        return self._callable

    async def _initialize_asgi_callable(self) -> None:
        self._callable: ASGIAppReplicaWrapper

        app: Starlette = self._callable.app

        # The reason we need to do this is because BackPressureError is a serve internal exception
        # and FastAPI doesn't know how to handle it, so it treats it as a 500 error.
        # With same reasoning, we are not handling TimeoutError because it's a generic exception
        # the FastAPI knows how to handle. See https://www.starlette.io/exceptions/
        def handle_exception(_: Request, exc: Exception):
            return self.handle_exception(exc)

        for exc in self.service_unavailable_exceptions:
            app.add_exception_handler(exc, handle_exception)

        await self._callable._run_asgi_lifespan_startup()

    @_run_user_code
    async def initialize_callable(self) -> Optional[ASGIApp]:
        """Initialize the user callable.

        If the callable is an ASGI app wrapper (e.g., using @serve.ingress), returns
        the ASGI app object, which may be used *read only* by the caller.
        """
        if self._callable is not None:
            raise RuntimeError("initialize_callable should only be called once.")

        # This closure initializes user code and finalizes replica
        # startup. By splitting the initialization step like this,
        # we can already access this actor before the user code
        # has finished initializing.
        # The supervising state manager can then wait
        # for allocation of this replica by using the `is_allocated`
        # method. After that, it calls `reconfigure` to trigger
        # user code initialization.
        logger.info(
            "Started initializing replica.",
            extra={"log_to_stderr": False},
        )

        if self._is_function:
            self._callable = self._deployment_def
        else:
            # This allows deployments to define an async __init__
            # method (mostly used for testing).
            self._callable = self._deployment_def.__new__(self._deployment_def)
            await self._call_func_or_gen(
                self._callable.__init__,
                args=self._init_args,
                kwargs=self._init_kwargs,
                # Always run the constructor on the main user code thread.
                run_sync_methods_in_threadpool_override=False,
            )

            if isinstance(self._callable, ASGIAppReplicaWrapper):
                await self._initialize_asgi_callable()

        self._user_health_check = getattr(self._callable, HEALTH_CHECK_METHOD, None)
        self._user_record_routing_stats = getattr(
            self._callable, REQUEST_ROUTING_STATS_METHOD, None
        )

        logger.info(
            "Finished initializing replica.",
            extra={"log_to_stderr": False},
        )

        return (
            self._callable.app
            if isinstance(self._callable, ASGIAppReplicaWrapper)
            else None
        )

    def _raise_if_not_initialized(self, method_name: str):
        if self._callable is None:
            raise RuntimeError(
                f"`initialize_callable` must be called before `{method_name}`."
            )

    def call_user_health_check(self) -> Optional[concurrent.futures.Future]:
        self._raise_if_not_initialized("call_user_health_check")

        # If the user provided a health check, call it on the user code thread. If user
        # code blocks the event loop the health check may time out.
        #
        # To avoid this issue for basic cases without a user-defined health check, skip
        # interacting with the user callable entirely.
        if self._user_health_check is not None:
            return self._call_user_health_check()

        return None

    def call_user_record_routing_stats(self) -> Optional[concurrent.futures.Future]:
        self._raise_if_not_initialized("call_user_record_routing_stats")

        if self._user_record_routing_stats is not None:
            return self._call_user_record_routing_stats()

        return None

    @_run_user_code
    async def _call_user_health_check(self):
        await self._call_func_or_gen(self._user_health_check)

    @_run_user_code
    async def _call_user_record_routing_stats(self) -> Dict[str, Any]:
        result, _ = await self._call_func_or_gen(self._user_record_routing_stats)
        return result

    @_run_user_code
    async def call_reconfigure(self, user_config: Any):
        self._raise_if_not_initialized("call_reconfigure")

        # NOTE(edoakes): there is the possibility of a race condition in user code if
        # they don't have any form of concurrency control between `reconfigure` and
        # other methods. See https://github.com/ray-project/ray/pull/42159.
        if user_config is not None:
            if self._is_function:
                raise ValueError("deployment_def must be a class to use user_config")
            elif not hasattr(self._callable, RECONFIGURE_METHOD):
                raise RayServeException(
                    "user_config specified but deployment "
                    + self._deployment_id
                    + " missing "
                    + RECONFIGURE_METHOD
                    + " method"
                )
            await self._call_func_or_gen(
                getattr(self._callable, RECONFIGURE_METHOD),
                args=(user_config,),
            )

    async def _handle_user_method_result(
        self,
        result: Any,
        user_method_info: UserMethodInfo,
        *,
        is_streaming: bool,
        is_http_request: bool,
        sync_gen_consumed: bool,
        generator_result_callback: Optional[Callable],
        asgi_args: Optional[ASGIArgs],
    ) -> Any:
        """Postprocess the result of a user method.

        User methods can be regular unary functions or return a sync or async generator.
        This method will raise an exception if the result is not of the expected type
        (e.g., non-generator for streaming requests or generator for unary requests).

        Generator outputs will be written to the `generator_result_callback`.

        Note that HTTP requests are an exception: they are *always* streaming requests,
        but for ASGI apps (like FastAPI), the actual method will be a regular function
        implementing the ASGI `__call__` protocol.
        """
        result_is_gen = inspect.isgenerator(result)
        result_is_async_gen = inspect.isasyncgen(result)
        if is_streaming:
            if result_is_gen:
                for r in result:
                    generator_result_callback(r)
            elif result_is_async_gen:
                async for r in result:
                    generator_result_callback(r)
            elif is_http_request and not user_method_info.is_asgi_app:
                # For the FastAPI codepath, the response has already been sent over
                # ASGI, but for the vanilla deployment codepath we need to send it.
                await self._send_user_result_over_asgi(result, asgi_args)
            elif not is_http_request and not sync_gen_consumed:
                # If a unary method is called with stream=True for anything EXCEPT
                # an HTTP request, raise an error.
                # HTTP requests are always streaming regardless of if the method
                # returns a generator, because it's provided the result queue as its
                # ASGI `send` interface to stream back results.
                raise TypeError(
                    f"Called method '{user_method_info.name}' with "
                    "`handle.options(stream=True)` but it did not return a "
                    "generator."
                )
        else:
            assert (
                not is_http_request
            ), "All HTTP requests go through the streaming codepath."

            if result_is_gen or result_is_async_gen:
                raise TypeError(
                    f"Method '{user_method_info.name}' returned a generator. "
                    "You must use `handle.options(stream=True)` to call "
                    "generators on a deployment."
                )

        return result

    async def call_http_entrypoint(
        self,
        request_metadata: RequestMetadata,
        status_code_callback: StatusCodeCallback,
        scope: Scope,
        receive: Receive,
    ) -> Any:
        result_queue = MessageQueue()
        user_method_info = self.get_user_method_info(request_metadata.call_method)

        if self._run_user_code_in_separate_thread:
            # `asyncio.Event`s are not thread safe, so `call_soon_threadsafe` must be
            # used to interact with the result queue from the user callable thread.
            system_event_loop = asyncio.get_running_loop()

            async def enqueue(item: Any):
                system_event_loop.call_soon_threadsafe(result_queue.put_nowait, item)

            call_future = self._call_http_entrypoint(
                user_method_info, scope, receive, enqueue
            )
        else:

            async def enqueue(item: Any):
                result_queue.put_nowait(item)

            call_future = asyncio.create_task(
                self._call_http_entrypoint(user_method_info, scope, receive, enqueue)
            )

        first_message_peeked = False
        async for messages in result_queue.fetch_messages_from_queue(call_future):
            # HTTP (ASGI) messages are only consumed by the proxy so batch them
            # and use vanilla pickle (we know it's safe because these messages
            # only contain primitive Python types).
            # Peek the first ASGI message to determine the status code.
            if not first_message_peeked:
                msg = messages[0]
                first_message_peeked = True
                if msg["type"] == "http.response.start":
                    # HTTP responses begin with exactly one
                    # "http.response.start" message containing the "status"
                    # field. Other response types like WebSockets may not.
                    status_code_callback(str(msg["status"]))

            yield messages

    @_run_user_code
    async def _call_http_entrypoint(
        self,
        user_method_info: UserMethodInfo,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> Any:
        """Call an HTTP entrypoint.

        `send` is used to communicate the results of streaming responses.

        Raises any exception raised by the user code so it can be propagated as a
        `RayTaskError`.
        """
        self._raise_if_not_initialized("_call_http_entrypoint")

        logger.info(
            f"Started executing request to method '{user_method_info.name}'.",
            extra={"log_to_stderr": False, "serve_access_log": True},
        )

        if user_method_info.is_asgi_app:
            request_args = (scope, receive, send)
        elif not user_method_info.takes_any_args:
            # Edge case to support empty HTTP handlers: don't pass the Request
            # argument if the callable has no parameters.
            request_args = tuple()
        else:
            # Non-FastAPI HTTP handlers take only the starlette `Request`.
            request_args = (starlette.requests.Request(scope, receive, send),)

        receive_task = None
        try:
            if hasattr(receive, "fetch_until_disconnect"):
                receive_task = asyncio.create_task(receive.fetch_until_disconnect())

            result, sync_gen_consumed = await self._call_func_or_gen(
                user_method_info.callable,
                args=request_args,
                kwargs={},
                is_streaming=True,
                generator_result_callback=send,
            )
            final_result = await self._handle_user_method_result(
                result,
                user_method_info,
                is_streaming=True,
                is_http_request=True,
                sync_gen_consumed=sync_gen_consumed,
                generator_result_callback=send,
                asgi_args=ASGIArgs(scope, receive, send),
            )

            if receive_task is not None and not receive_task.done():
                receive_task.cancel()

            return final_result
        except Exception as e:
            if not user_method_info.is_asgi_app:
                response = self.handle_exception(e)
                await self._send_user_result_over_asgi(
                    response, ASGIArgs(scope, receive, send)
                )

            if receive_task is not None and not receive_task.done():
                receive_task.cancel()

            raise
        except asyncio.CancelledError:
            if receive_task is not None and not receive_task.done():
                # Do NOT cancel the receive task if the request has been
                # cancelled, but the call is a batched call. This is
                # because we cannot guarantee cancelling the batched
                # call, so in the case that the call continues executing
                # we should continue fetching data from the client.
                if not hasattr(user_method_info.callable, "set_max_batch_size"):
                    receive_task.cancel()

            raise

    async def call_user_generator(
        self,
        request_metadata: RequestMetadata,
        request_args: Tuple[Any],
        request_kwargs: Dict[str, Any],
    ) -> AsyncGenerator[Any, None]:
        """Calls a user method for a streaming call and yields its results.

        The user method is called in an asyncio `Task` and places its results on a
        `result_queue`. This method pulls and yields from the `result_queue`.
        """
        if not self._run_user_code_in_separate_thread:
            gen = await self._call_user_generator(
                request_metadata, request_args, request_kwargs
            )
            async for result in gen:
                yield result
        else:
            result_queue = MessageQueue()

            # `asyncio.Event`s are not thread safe, so `call_soon_threadsafe` must be
            # used to interact with the result queue from the user callable thread.
            system_event_loop = asyncio.get_running_loop()

            def _enqueue_thread_safe(item: Any):
                system_event_loop.call_soon_threadsafe(result_queue.put_nowait, item)

            call_future = self._call_user_generator(
                request_metadata,
                request_args,
                request_kwargs,
                enqueue=_enqueue_thread_safe,
            )

            async for messages in result_queue.fetch_messages_from_queue(call_future):
                for msg in messages:
                    yield msg

    @_run_user_code
    async def _call_user_generator(
        self,
        request_metadata: RequestMetadata,
        request_args: Tuple[Any],
        request_kwargs: Dict[str, Any],
        *,
        enqueue: Optional[Callable] = None,
    ) -> Optional[AsyncGenerator[Any, None]]:
        """Call a user generator.

        The `generator_result_callback` is used to communicate the results of generator
        methods.

        Raises any exception raised by the user code so it can be propagated as a
        `RayTaskError`.
        """
        self._raise_if_not_initialized("_call_user_generator")

        request_args = request_args if request_args is not None else tuple()
        request_kwargs = request_kwargs if request_kwargs is not None else dict()

        user_method_info = self.get_user_method_info(request_metadata.call_method)
        callable = user_method_info.callable
        is_sync_method = (
            inspect.isfunction(callable) or inspect.ismethod(callable)
        ) and not (
            inspect.iscoroutinefunction(callable)
            or inspect.isasyncgenfunction(callable)
        )

        logger.info(
            f"Started executing request to method '{user_method_info.name}'.",
            extra={"log_to_stderr": False, "serve_access_log": True},
        )

        async def _call_generator_async() -> AsyncGenerator[Any, None]:
            gen = callable(*request_args, **request_kwargs)
            if inspect.iscoroutine(gen):
                gen = await gen

            if inspect.isgenerator(gen):
                for result in gen:
                    yield result
            elif inspect.isasyncgen(gen):
                async for result in gen:
                    yield result
            else:
                raise TypeError(
                    f"Called method '{user_method_info.name}' with "
                    "`handle.options(stream=True)` but it did not return a generator."
                )

        def _call_generator_sync():
            gen = callable(*request_args, **request_kwargs)
            if inspect.isgenerator(gen):
                for result in gen:
                    enqueue(result)
            else:
                raise TypeError(
                    f"Called method '{user_method_info.name}' with "
                    "`handle.options(stream=True)` but it did not return a generator."
                )

        if enqueue and is_sync_method and self._run_sync_methods_in_threadpool:
            await to_thread.run_sync(_call_generator_sync)
        elif enqueue:

            async def gen_coro_wrapper():
                async for result in _call_generator_async():
                    enqueue(result)

            await gen_coro_wrapper()
        else:
            return _call_generator_async()

    @_run_user_code
    async def call_user_method(
        self,
        request_metadata: RequestMetadata,
        request_args: Tuple[Any],
        request_kwargs: Dict[str, Any],
    ) -> Any:
        """Call a (unary) user method.

        Raises any exception raised by the user code so it can be propagated as a
        `RayTaskError`.
        """
        self._raise_if_not_initialized("call_user_method")

        logger.info(
            f"Started executing request to method '{request_metadata.call_method}'.",
            extra={"log_to_stderr": False, "serve_access_log": True},
        )

        user_method_info = self.get_user_method_info(request_metadata.call_method)
        result, _ = await self._call_func_or_gen(
            user_method_info.callable,
            args=request_args,
            kwargs=request_kwargs,
            is_streaming=False,
        )
        if inspect.isgenerator(result) or inspect.isasyncgen(result):
            raise TypeError(
                f"Method '{user_method_info.name}' returned a generator. "
                "You must use `handle.options(stream=True)` to call "
                "generators on a deployment."
            )
        return result

    def handle_exception(self, exc: Exception):
        if isinstance(exc, self.service_unavailable_exceptions):
            return starlette.responses.Response(exc.message, status_code=503)
        else:
            return starlette.responses.Response(
                "Internal Server Error", status_code=500
            )

    @_run_user_code
    async def call_destructor(self):
        """Explicitly call the `__del__` method of the user callable.

        Calling this multiple times has no effect; only the first call will
        actually call the destructor.
        """
        if self._callable is None:
            logger.info(
                "This replica has not yet started running user code. "
                "Skipping __del__."
            )
            return

        # Only run the destructor once. This is safe because there is no `await` between
        # checking the flag here and flipping it to `True` below.
        if self._destructor_called:
            return

        self._destructor_called = True
        try:
            if hasattr(self._callable, "__del__"):
                # Make sure to accept `async def __del__(self)` as well.
                await self._call_func_or_gen(
                    self._callable.__del__,
                    # Always run the destructor on the main user callable thread.
                    run_sync_methods_in_threadpool_override=False,
                )

            if hasattr(self._callable, "__serve_multiplex_wrapper"):
                await getattr(self._callable, "__serve_multiplex_wrapper").shutdown()

        except Exception as e:
            logger.exception(f"Exception during graceful shutdown of replica: {e}")
