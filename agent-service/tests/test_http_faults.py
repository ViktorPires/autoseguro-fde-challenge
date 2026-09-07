import asyncio
import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from test_quote_client import CATALOGUE

from agent_service.config import Settings
from agent_service.quote_client import DependencyError, QuoteClient


@asynccontextmanager
async def stub(responses, delay=0):
    calls, tasks = [], set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            await reader.readuntil(b"\r\n\r\n")
            calls.append(1)
            await asyncio.sleep(delay)
            status, body = responses[min(len(calls) - 1, len(responses) - 1)]
            content = body if isinstance(body, bytes) else json.dumps(body).encode()
            writer.write(
                f"HTTP/1.1 {status} Result\r\nContent-Length: {len(content)}\r\nConnection: close\r\n\r\n".encode()
                + content
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", calls
    finally:
        server.close()
        await server.wait_closed()
        active = list(tasks)
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)


async def test_real_transport_5xx_then_success():
    async with (
        stub([(503, {}), (502, {}), (200, CATALOGUE)]) as (url, calls),
        httpx.AsyncClient(
            base_url=url, transport=httpx.AsyncHTTPTransport(retries=0)
        ) as http,
    ):
        client = QuoteClient(
            http, Settings(hmac_secret="s" * 32), jitter=lambda a, b: 0
        )
        result = await client.catalogue(client.deadline(), str(uuid4()))
        assert result.moeda == "BRL"
        assert len(calls) == 3


@pytest.mark.parametrize("body", [b"not json private sentinel", {"moeda": "BRL"}])
async def test_invalid_success_not_retried(body):
    async with (
        stub([(200, body)]) as (url, calls),
        httpx.AsyncClient(base_url=url) as http,
    ):
        client = QuoteClient(http, Settings(hmac_secret="s" * 32))
        with pytest.raises(DependencyError) as exc:
            await client.catalogue(client.deadline(), str(uuid4()))
        assert exc.value.code == "upstream_contract"
        assert len(calls) == 1


async def test_real_transport_deadline():
    async with (
        stub([(200, CATALOGUE)], delay=0.3) as (url, calls),
        httpx.AsyncClient(base_url=url) as http,
    ):
        client = QuoteClient(
            http,
            Settings(hmac_secret="s" * 32, quote_deadline=0.08, attempt_timeout=0.04),
            jitter=lambda a, b: 0,
        )
        start = asyncio.get_running_loop().time()
        with pytest.raises(DependencyError):
            await client.catalogue(client.deadline(), str(uuid4()))
        assert asyncio.get_running_loop().time() - start < 0.2
        assert 1 <= len(calls) <= 3
