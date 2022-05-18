import json
import typing as t
import asyncio
import inspect
from typing import TYPE_CHECKING
from json.decoder import JSONDecodeError
from urllib.parse import urlparse

from . import RunnerHandle
from ..container import Payload
from ...utils.uri import uri_to_path
from ....exceptions import RemoteException
from ...runner.utils import Params
from ...runner.utils import PAYLOAD_META_HEADER
from ...runner.utils import payload_params_to_multipart
from ...configuration.containers import DeploymentContainer

if TYPE_CHECKING:  # pragma: no cover
    from aiohttp import BaseConnector
    from aiohttp.client import ClientSession

    from ..runner import Runner


class RemoteRunnerClient(RunnerHandle):
    def __init__(  # pylint: disable=super-init-not-called
        self, runner: "Runner"
    ) -> None:
        self._runner = runner
        self._conn: t.Optional["BaseConnector"] = None
        self._client: t.Optional["ClientSession"] = None
        self._loop: t.Optional[asyncio.AbstractEventLoop] = None
        self._addr: t.Optional[str] = None

    @property
    def _remote_runner_server_map(self) -> t.Dict[str, str]:
        return DeploymentContainer.remote_runner_mapping.get()

    def _close_conn(self) -> None:
        if self._conn:
            self._conn.close()

    def _get_conn(self) -> "BaseConnector":
        import aiohttp

        if (
            self._loop is None
            or self._conn is None
            or self._conn.closed
            or self._loop.is_closed()
        ):
            self._loop = asyncio.get_event_loop()  # get the loop lazily
            bind_uri = self._remote_runner_server_map[self._runner.name]
            parsed = urlparse(bind_uri)
            if parsed.scheme == "file":
                path = uri_to_path(bind_uri)
                self._conn = aiohttp.UnixConnector(
                    path=path,
                    loop=self._loop,
                    limit=800,  # TODO(jiang): make it configurable
                    keepalive_timeout=1800.0,
                )
                self._addr = "http://127.0.0.1:8000"  # addr doesn't matter with UDS
            elif parsed.scheme == "tcp":
                self._conn = aiohttp.TCPConnector(
                    loop=self._loop,
                    verify_ssl=False,
                    limit=800,  # TODO(jiang): make it configurable
                    keepalive_timeout=1800.0,
                )
                self._addr = f"http://{parsed.netloc}"
            else:
                raise ValueError(f"Unsupported bind scheme: {parsed.scheme}")
        return self._conn

    def _get_client(
        self,
        timeout_sec: t.Optional[float] = None,
    ) -> "ClientSession":
        import aiohttp

        if (
            self._loop is None
            or self._client is None
            or self._client.closed
            or self._loop.is_closed()
        ):
            import yarl
            from opentelemetry.instrumentation.aiohttp_client import create_trace_config

            def strip_query_params(url: yarl.URL) -> str:
                return str(url.with_query(None))

            jar = aiohttp.DummyCookieJar()
            if timeout_sec is not None:
                timeout = aiohttp.ClientTimeout(total=timeout_sec)
            else:
                DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=5 * 60)
                timeout = DEFAULT_TIMEOUT
            self._client = aiohttp.ClientSession(
                trace_configs=[
                    create_trace_config(
                        # Remove all query params from the URL attribute on the span.
                        url_filter=strip_query_params,  # type: ignore
                        tracer_provider=DeploymentContainer.tracer_provider.get(),
                    )
                ],
                connector=self._get_conn(),
                auto_decompress=False,
                cookie_jar=jar,
                connector_owner=False,
                timeout=timeout,
                loop=self._loop,
            )
        return self._client

    async def async_run_method(
        self,
        __bentoml_method_name: str,
        *args: t.Any,
        **kwargs: t.Any,
    ) -> t.Any:
        runnable_method = getattr(
            self._runner.runnable_class,
            __bentoml_method_name,
        )

        signature = inspect.signature(runnable_method)
        # binding self parameter to None as we don't want to instantiate the runner
        bound_args = signature.bind(None, *args, **kwargs)
        bound_args.apply_defaults()

        from ...runner.container import AutoContainer

        for name, value in bound_args.arguments.items():
            bound_args.arguments[name] = AutoContainer.to_payload(value)
        params = Params(*bound_args.args[1:], **bound_args.kwargs)
        multipart = payload_params_to_multipart(params)
        client = self._get_client()
        async with client.post(
            f"{self._addr}/{__bentoml_method_name}",
            data=multipart,
        ) as resp:
            body = await resp.read()

        if resp.status != 200:
            raise RemoteException(
                f"An exception occurred in remote runner {self._runner.name}: [{resp.status}] {body.decode()}"
            )

        try:
            meta_header = resp.headers[PAYLOAD_META_HEADER]
        except KeyError:
            raise RemoteException(
                f"Bento payload decode error: {PAYLOAD_META_HEADER} header not set. "
                "An exception might have occurred in the remote server."
                f"[{resp.status}] {body.decode()}"
            ) from None

        try:
            content_type = resp.headers["Content-Type"]
        except KeyError:
            raise RemoteException(
                f"Bento payload decode error: Content-Type header not set. "
                "An exception might have occurred in the remote server."
                f"[{resp.status}] {body.decode()}"
            ) from None

        if not content_type.lower().startswith("application/vnd.bentoml."):
            raise RemoteException(
                f"Bento payload decode error: invalid Content-Type '{content_type}'."
            )

        container = content_type.strip("application/vnd.bentoml.")

        try:
            payload = Payload(
                data=body, meta=json.loads(meta_header), container=container
            )
        except JSONDecodeError:
            raise ValueError(f"Bento payload decode error: {meta_header}")

        return AutoContainer.from_payload(payload)

    def run_method(
        self,
        __bentoml_method_name: str,
        *args: t.Any,
        **kwargs: t.Any,
    ) -> t.Any:
        import anyio

        return anyio.from_thread.run(
            self.async_run_method,
            __bentoml_method_name,
            *args,
            **kwargs,
        )

    def __del__(self) -> None:
        self._close_conn()