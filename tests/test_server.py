"""Integration tests for the FastAPI server."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from evalforge_runtime.config import load_config
from evalforge_runtime.db import close_db, init_db
from evalforge_runtime.server import create_app


@pytest.fixture
def app(config_yaml: Path):
    """Create a test app with in-memory DB."""
    config = load_config(config_yaml)
    # Force in-memory SQLite for tests
    config.database.url = "sqlite+aiosqlite://"
    return create_app(config)


@pytest.fixture
async def client(app):
    """Create an async test client with manually initialized DB."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Manually init DB since we're not going through ASGI lifespan in tests
        await init_db("sqlite+aiosqlite://")
        yield ac
        await close_db()


def _mock_llm_response(content: str = '{"result": "ok"}'):
    """Create a mock LiteLLM response."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.model = "claude-haiku-4-5"
    response.usage = MagicMock()
    response.usage.prompt_tokens = 50
    response.usage.completion_tokens = 20
    return response


@pytest.mark.asyncio
async def test_health_no_auth(client: AsyncClient) -> None:
    """Health endpoint requires no authentication."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["project_id"] == "test_proj"
    assert data["runtime_version"] == "0.1.0"
    assert "test-process" in data["processes"]


@pytest.mark.asyncio
async def test_process_requires_auth(client: AsyncClient) -> None:
    """Process endpoint rejects unauthenticated requests."""
    resp = await client.post(
        "/process/test-process",
        json={"subject": "Hello"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_process_with_valid_key(client: AsyncClient) -> None:
    """Process endpoint accepts valid API key."""
    mock_resp = _mock_llm_response('{"result": "processed"}')

    with (
        patch("evalforge_runtime.executor.litellm.acompletion", new_callable=AsyncMock) as mock_llm,
        patch("evalforge_runtime.executor.litellm.completion_cost", return_value=0.001),
    ):
        mock_llm.return_value = mock_resp

        resp = await client.post(
            "/process/test-process",
            json={"subject": "Test email"},
            headers={"X-API-Key": "test-key-123"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"result": "processed"}


@pytest.mark.asyncio
async def test_process_invalid_key(client: AsyncClient) -> None:
    """Process endpoint rejects invalid API key."""
    resp = await client.post(
        "/process/test-process",
        json={"subject": "Hello"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_process_unknown_route(client: AsyncClient) -> None:
    """Unknown process name returns 404."""
    resp = await client.post(
        "/process/nonexistent",
        json={"data": "test"},
        headers={"X-API-Key": "test-key-123"},
    )
    assert resp.status_code in (404, 405)


@pytest.mark.asyncio
async def test_executions_list(client: AsyncClient) -> None:
    """List executions after running a process."""
    mock_resp = _mock_llm_response()

    with (
        patch("evalforge_runtime.executor.litellm.acompletion", new_callable=AsyncMock) as mock_llm,
        patch("evalforge_runtime.executor.litellm.completion_cost", return_value=0.001),
    ):
        mock_llm.return_value = mock_resp

        # Execute a process first
        await client.post(
            "/process/test-process",
            json={"subject": "Test"},
            headers={"X-API-Key": "test-key-123"},
        )

    # List executions
    resp = await client.get(
        "/executions",
        headers={"X-API-Key": "test-key-123"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["executions"]) >= 1
    exec_data = data["executions"][0]
    assert exec_data["process_name"] == "test-process"
    assert exec_data["status"] == "success"
    assert exec_data["llm_cost_usd"] == 0.001


@pytest.mark.asyncio
async def test_execution_detail(client: AsyncClient) -> None:
    """Get single execution detail."""
    mock_resp = _mock_llm_response('{"answer": "42"}')

    with (
        patch("evalforge_runtime.executor.litellm.acompletion", new_callable=AsyncMock) as mock_llm,
        patch("evalforge_runtime.executor.litellm.completion_cost", return_value=0.002),
    ):
        mock_llm.return_value = mock_resp

        await client.post(
            "/process/test-process",
            json={"question": "What is the meaning of life?"},
            headers={"X-API-Key": "test-key-123"},
        )

    # Get executions list to find the ID
    list_resp = await client.get(
        "/executions",
        headers={"X-API-Key": "test-key-123"},
    )
    exec_id = list_resp.json()["executions"][0]["id"]

    # Get detail
    resp = await client.get(
        f"/executions/{exec_id}",
        headers={"X-API-Key": "test-key-123"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["output"] == {"answer": "42"}
    assert data["llm_model"] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_execution_stats(client: AsyncClient) -> None:
    """Get execution stats."""
    mock_resp = _mock_llm_response()

    with (
        patch("evalforge_runtime.executor.litellm.acompletion", new_callable=AsyncMock) as mock_llm,
        patch("evalforge_runtime.executor.litellm.completion_cost", return_value=0.001),
    ):
        mock_llm.return_value = mock_resp

        # Run a couple of processes
        for _ in range(3):
            await client.post(
                "/process/test-process",
                json={"data": "test"},
                headers={"X-API-Key": "test-key-123"},
            )

    resp = await client.get(
        "/executions/stats",
        headers={"X-API-Key": "test-key-123"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_executions"] >= 3
    assert data["success_rate"] > 0


@pytest.mark.asyncio
async def test_execution_not_found(client: AsyncClient) -> None:
    """404 for nonexistent execution."""
    resp = await client.get(
        "/executions/nonexistent-id",
        headers={"X-API-Key": "test-key-123"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_process_llm_error(client: AsyncClient) -> None:
    """LLM errors result in 500 and logged execution."""
    with patch(
        "evalforge_runtime.executor.litellm.acompletion",
        new_callable=AsyncMock,
        side_effect=Exception("LLM unavailable"),
    ):
        resp = await client.post(
            "/process/test-process",
            json={"data": "test"},
            headers={"X-API-Key": "test-key-123"},
        )

    assert resp.status_code == 500

    # Verify error was logged
    list_resp = await client.get(
        "/executions?status=error",
        headers={"X-API-Key": "test-key-123"},
    )
    errors = [e for e in list_resp.json()["executions"] if e["status"] == "error"]
    assert len(errors) >= 1
    assert "LLM unavailable" in errors[0]["error"]
