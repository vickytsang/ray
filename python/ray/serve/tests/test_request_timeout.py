import asyncio
import os
import sys
from typing import Generator, Set

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import StreamingResponse

import ray
from ray import serve
from ray._common.test_utils import SignalActor, wait_for_condition
from ray.dashboard.modules.serve.sdk import ServeSubmissionClient
from ray.serve._private.test_utils import (
    get_application_url,
    send_signal_on_cancellation,
)
from ray.serve.schema import ApplicationStatus, ServeInstanceDetails
from ray.util.state import list_tasks


@ray.remote
def do_request():
    # Set a timeout to 10 because some test use RAY_SERVE_REQUEST_PROCESSING_TIMEOUT_S = 5
    # and httpx default timeout is 5 seconds.
    return httpx.get(get_application_url(use_localhost=True), timeout=10)


@pytest.fixture
def shutdown_serve():
    yield
    serve.shutdown()


@pytest.mark.parametrize(
    "ray_instance", [{"RAY_SERVE_REQUEST_PROCESSING_TIMEOUT_S": "5"}], indirect=True
)
def test_normal_operation(ray_instance, shutdown_serve):
    """
    Verify that a moderate timeout doesn't affect normal operation.
    """

    @serve.deployment(num_replicas=2)
    def f(*args):
        return "Success!"

    serve.run(f.bind())

    assert all(
        response.text == "Success!"
        for response in ray.get([do_request.remote() for _ in range(10)])
    )


@pytest.mark.parametrize(
    "ray_instance", [{"RAY_SERVE_REQUEST_PROCESSING_TIMEOUT_S": "0.1"}], indirect=True
)
def test_request_hangs_in_execution(ray_instance, shutdown_serve):
    """
    Verify that requests are timed out if they take longer than the timeout to execute.
    """

    @ray.remote
    class PidTracker:
        def __init__(self):
            self.pids = set()

        def add_pid(self, pid: int) -> None:
            self.pids.add(pid)

        def get_pids(self) -> Set[int]:
            return self.pids

    pid_tracker = PidTracker.remote()
    signal_actor = SignalActor.remote()

    @serve.deployment(num_replicas=2, graceful_shutdown_timeout_s=0)
    class HangsOnFirstRequest:
        def __init__(self):
            self._saw_first_request = False

        async def __call__(self):
            ray.get(pid_tracker.add_pid.remote(os.getpid()))
            if not self._saw_first_request:
                self._saw_first_request = True
                await asyncio.sleep(10)

            return "Success!"

    serve.run(HangsOnFirstRequest.bind())

    response = httpx.get(get_application_url(use_localhost=True))
    assert response.status_code == 408

    ray.get(signal_actor.send.remote())


@serve.deployment(graceful_shutdown_timeout_s=0)
class HangsOnFirstRequest:
    def __init__(self):
        self._saw_first_request = False
        self.signal_actor = SignalActor.remote()

    async def __call__(self):
        if not self._saw_first_request:
            self._saw_first_request = True
            await self.signal_actor.wait.remote()
        else:
            ray.get(self.signal_actor.send.remote())
        return "Success!"


hangs_on_first_request_app = HangsOnFirstRequest.bind()


def test_with_rest_api(ray_instance, shutdown_serve):
    """Verify the REST API can configure the request timeout."""
    config = {
        "proxy_location": "EveryNode",
        "http_options": {"request_timeout_s": 1},
        "applications": [
            {
                "name": "app",
                "route_prefix": "/",
                "import_path": (
                    "ray.serve.tests.test_request_timeout:hangs_on_first_request_app"
                ),
            }
        ],
    }
    ServeSubmissionClient("http://localhost:8265").deploy_applications(config)

    def application_running():
        response = httpx.get(
            "http://localhost:8265/api/serve/applications/", timeout=15
        )
        assert response.status_code == 200

        serve_details = ServeInstanceDetails(**response.json())
        return serve_details.applications["app"].status == ApplicationStatus.RUNNING

    wait_for_condition(application_running, timeout=15)
    print("Application has started running. Testing requests...")

    response = httpx.get(get_application_url(app_name="app", use_localhost=True))
    assert response.status_code == 408

    response = httpx.get(get_application_url(app_name="app", use_localhost=True))
    assert response.status_code == 200
    print("Requests succeeded! Deleting application.")
    ServeSubmissionClient("http://localhost:8265").delete_applications()


@pytest.mark.parametrize(
    "ray_instance", [{"RAY_SERVE_REQUEST_PROCESSING_TIMEOUT_S": "0.5"}], indirect=True
)
def test_request_hangs_in_assignment(ray_instance, shutdown_serve):
    """
    Verify that requests are timed out if they take longer than the timeout while
    pending assignment (queued in the handle).
    """
    signal_actor = SignalActor.remote()

    @serve.deployment(graceful_shutdown_timeout_s=0, max_ongoing_requests=1)
    class HangsOnFirstRequest:
        def __init__(self):
            self._saw_first_request = False

        async def __call__(self):
            await signal_actor.wait.remote()
            return "Success!"

    serve.run(HangsOnFirstRequest.bind())

    # First request will hang executing, second pending assignment.
    response_ref1 = do_request.remote()
    response_ref2 = do_request.remote()

    # Streaming path does not retry on timeouts, so the requests should be failed.
    assert ray.get(response_ref1).status_code == 408
    assert ray.get(response_ref2).status_code == 408
    ray.get(signal_actor.send.remote())
    assert ray.get(do_request.remote()).status_code == 200


@pytest.mark.parametrize(
    "ray_instance", [{"RAY_SERVE_REQUEST_PROCESSING_TIMEOUT_S": "5"}], indirect=True
)
def test_streaming_request_already_sent_and_timed_out(ray_instance, shutdown_serve):
    """
    Verify that streaming requests are timed out even if some chunks have already
    been sent.
    """
    signal_actor = SignalActor.remote()

    @serve.deployment(graceful_shutdown_timeout_s=0, max_ongoing_requests=1)
    class BlockOnSecondChunk:
        async def generate_numbers(self) -> Generator[str, None, None]:
            for i in range(2):
                yield f"generated {i}"
                await signal_actor.wait.remote()

        def __call__(self, request: Request) -> StreamingResponse:
            gen = self.generate_numbers()
            return StreamingResponse(gen, status_code=200, media_type="text/plain")

    serve.run(BlockOnSecondChunk.bind())

    def health_check():
        response = httpx.get(f"{get_application_url(use_localhost=True)}/-/healthz")
        assert response.status_code == 200
        return True

    # Wait for the server to start by doing health check.
    wait_for_condition(health_check, timeout=10)

    with httpx.stream("GET", get_application_url(use_localhost=True), timeout=10) as r:
        iterator = r.iter_text()

        # The first chunk should be received successfully.
        assert next(iterator) == "generated 0"
        assert r.status_code == 200

        # The second chunk should time out and raise error.
        with pytest.raises(httpx.RemoteProtocolError) as request_error:
            next(iterator)
            assert "peer closed connection" in str(request_error.value)


@pytest.mark.parametrize(
    "ray_instance",
    [
        {
            "RAY_SERVE_REQUEST_PROCESSING_TIMEOUT_S": "0.5",
            "RAY_SERVE_ENABLE_TASK_EVENTS": "1",
        }
    ],
    indirect=True,
)
def test_request_timeout_does_not_leak_tasks(ray_instance, shutdown_serve):
    """Verify that the ASGI-related tasks exit when a request is timed out.

    See https://github.com/ray-project/ray/issues/38368 for details.
    """

    @serve.deployment
    class Hang:
        async def __call__(self):
            await asyncio.sleep(1000000)

    serve.run(Hang.bind())

    def get_num_running_tasks():
        return len(
            list_tasks(
                address=ray_instance["gcs_address"],
                filters=[
                    ("NAME", "!=", "ServeController.listen_for_change"),
                    ("TYPE", "=", "ACTOR_TASK"),
                    ("STATE", "=", "RUNNING"),
                ],
            )
        )

    wait_for_condition(lambda: get_num_running_tasks() == 0)

    # Send a number of requests that all will be timed out.
    results = ray.get([do_request.remote() for _ in range(10)])
    assert all(r.status_code == 408 for r in results)

    # The tasks should all be cancelled.
    wait_for_condition(lambda: get_num_running_tasks() == 0)


@pytest.mark.parametrize(
    "ray_instance", [{"RAY_SERVE_REQUEST_PROCESSING_TIMEOUT_S": "0.5"}], indirect=True
)
@pytest.mark.parametrize("use_fastapi", [False, True])
def test_cancel_on_http_timeout_during_execution(
    ray_instance, shutdown_serve, use_fastapi: bool
):
    """Test the request timing out while the handler is executing."""
    inner_signal_actor = SignalActor.remote()
    outer_signal_actor = SignalActor.remote()

    @serve.deployment
    async def inner():
        async with send_signal_on_cancellation(inner_signal_actor):
            pass

    if use_fastapi:
        app = FastAPI()

        @serve.deployment
        @serve.ingress(app)
        class Ingress:
            def __init__(self, handle):
                self._handle = handle

            @app.get("/")
            async def wait_for_cancellation(self):
                _ = self._handle.remote()
                async with send_signal_on_cancellation(outer_signal_actor):
                    pass

    else:

        @serve.deployment
        class Ingress:
            def __init__(self, handle):
                self._handle = handle

            async def __call__(self, request: Request):
                _ = self._handle.remote()
                async with send_signal_on_cancellation(outer_signal_actor):
                    pass

    serve.run(Ingress.bind(inner.bind()))

    # Request should time out, causing the handler and handle call to be cancelled.
    assert httpx.get(get_application_url(use_localhost=True)).status_code == 408
    ray.get(inner_signal_actor.wait.remote(), timeout=10)
    ray.get(outer_signal_actor.wait.remote(), timeout=10)


@pytest.mark.parametrize(
    "ray_instance", [{"RAY_SERVE_REQUEST_PROCESSING_TIMEOUT_S": "0.5"}], indirect=True
)
def test_cancel_on_http_timeout_during_assignment(ray_instance, shutdown_serve):
    """Test the client disconnecting while the proxy is assigning the request."""
    signal_actor = SignalActor.remote()

    @serve.deployment(max_ongoing_requests=1)
    class Ingress:
        def __init__(self):
            self._num_requests = 0

        async def __call__(self, *args):
            self._num_requests += 1
            await signal_actor.wait.remote()

            return self._num_requests

    h = serve.run(Ingress.bind())

    # Send a request and wait for it to be ongoing so we know that further requests
    # will be blocking trying to assign a replica.
    initial_response = h.remote()
    wait_for_condition(lambda: ray.get(signal_actor.cur_num_waiters.remote()) == 1)

    # Request should time out, causing the handler and handle call to be cancelled.
    assert httpx.get(get_application_url(use_localhost=True)).status_code == 408

    # Now signal the initial request to finish and check that the request sent via HTTP
    # never reaches the replica.
    ray.get(signal_actor.send.remote())
    assert initial_response.result() == 1
    for i in range(2, 12):
        assert h.remote().result() == i


@pytest.mark.parametrize(
    "ray_instance", [{"RAY_SERVE_REQUEST_PROCESSING_TIMEOUT_S": "0.5"}], indirect=True
)
def test_timeout_error_in_child_deployment_of_fastapi(ray_instance, shutdown_serve):
    """Test that timeout error in child deployment returns 408 with FastAPI ingress."""
    app = FastAPI()
    signal = SignalActor.remote()

    @serve.deployment
    class Child:
        async def __call__(self):
            await signal.wait.remote()
            return "ok"

    @serve.deployment
    @serve.ingress(app)
    class Parent:
        def __init__(self, child):
            self.child = child

        @app.get("/")
        async def root(self):
            return await self.child.remote()

    serve.run(Parent.bind(Child.bind()))

    r = httpx.get(get_application_url(use_localhost=True))
    assert r.status_code == 408

    ray.get(signal.send.remote())


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", "-s", __file__]))
