"""Tests for review API endpoints."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from evalforge_runtime.config import load_config
from evalforge_runtime.db import (
    close_db,
    create_execution,
    get_session_factory,
    init_db,
    update_execution,
)
from evalforge_runtime.executor import Executor
from evalforge_runtime.pipeline import Pipeline
from evalforge_runtime.server import create_app
from evalforge_runtime.storage import LocalStorage


@pytest.fixture
def review_config_yaml(tmp_path: Path) -> Path:
    """Config with a review-enabled process."""
    config_content = """
project:
  id: "test_proj"
  version: "0.1.0"

secrets:
  provider: env

auth:
  methods:
    - type: api_key
      header: "X-API-Key"

llm:
  model: "test-model"

database:
  url: "sqlite+aiosqlite://"

storage:
  type: local
  path: "{storage_path}"

processes:
  review-process:
    process_id: "proc_review"
    instructions: "Test instructions. Return JSON."
    trigger:
      type: webhook
    review:
      enabled: true
      timeout: 24h

  normal-process:
    process_id: "proc_normal"
    instructions: "Test instructions. Return JSON."
    trigger:
      type: webhook
""".replace("{storage_path}", str(tmp_path / "files").replace("\\", "/"))
    config_file = tmp_path / "evalforge.config.yaml"
    config_file.write_text(config_content)
    return config_file


@pytest.fixture
def review_config(review_config_yaml: Path):
    config = load_config(review_config_yaml)
    config.database.url = "sqlite+aiosqlite://"
    return config


@pytest.fixture
async def review_client(review_config, tmp_path):
    """Create a test client with pipeline properly initialized."""
    app = create_app(review_config)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await init_db("sqlite+aiosqlite://")

        # Initialize pipeline and inject into app.state (bypassing lifespan)
        storage = LocalStorage(str(tmp_path / "files"))
        executor = Executor("test-model")
        pipeline = Pipeline(review_config, executor, storage, secrets={})
        app.state.pipeline = pipeline

        yield ac, pipeline
        await close_db()


def _mock_llm_response(content: str = '{"result": "ok"}'):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.model = "test-model"
    response.usage = MagicMock()
    response.usage.prompt_tokens = 10
    response.usage.completion_tokens = 5
    return response


async def _create_pending_review(pipeline: Pipeline) -> str:
    """Create a pending review execution via the pipeline."""
    from evalforge_runtime.types import TriggerContext

    trigger = TriggerContext(type="webhook", ref="test-ref")

    with (
        patch(
            "evalforge_runtime.executor.litellm.acompletion",
            new_callable=AsyncMock,
        ) as mock_llm,
        patch("evalforge_runtime.executor.litellm.completion_cost", return_value=0.001),
    ):
        mock_llm.return_value = _mock_llm_response('{"draft": "response"}')
        await pipeline.execute_process(
            "review-process", {"email": "test@test.com"}, trigger
        )

    # Find the pending review execution
    session_factory = get_session_factory()
    async with session_factory() as session:
        from evalforge_runtime.db import list_executions

        execs = await list_executions(session, status=["pending_review"])
        assert len(execs) >= 1
        return execs[0].id


@pytest.mark.asyncio
async def test_review_list(review_client) -> None:
    """GET /reviews lists pending reviews."""
    client, pipeline = review_client
    await _create_pending_review(pipeline)

    resp = await client.get(
        "/reviews",
        headers={"X-API-Key": "test-key-123"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["reviews"]) >= 1
    assert data["reviews"][0]["status"] == "pending_review"


@pytest.mark.asyncio
async def test_review_detail(review_client) -> None:
    """GET /reviews/:id returns review detail with timeout."""
    client, pipeline = review_client
    exec_id = await _create_pending_review(pipeline)

    resp = await client.get(
        f"/reviews/{exec_id}",
        headers={"X-API-Key": "test-key-123"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == exec_id
    assert data["status"] == "pending_review"
    assert data["timeout"] == "24h"


@pytest.mark.asyncio
async def test_approve_review(review_client) -> None:
    """POST /reviews/:id/approve transitions to success."""
    client, pipeline = review_client
    exec_id = await _create_pending_review(pipeline)

    resp = await client.post(
        f"/reviews/{exec_id}/approve",
        json={"reviewed_by": "tester"},
        headers={"X-API-Key": "test-key-123"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    assert data["output"] == {"draft": "response"}


@pytest.mark.asyncio
async def test_approve_with_modified_output(review_client) -> None:
    """Approving with modified output uses the new output."""
    client, pipeline = review_client
    exec_id = await _create_pending_review(pipeline)

    modified = {"draft": "human-edited response"}
    resp = await client.post(
        f"/reviews/{exec_id}/approve",
        json={"output": modified, "reviewed_by": "editor"},
        headers={"X-API-Key": "test-key-123"},
    )
    assert resp.status_code == 200
    assert resp.json()["output"] == modified


@pytest.mark.asyncio
async def test_reject_review(review_client) -> None:
    """POST /reviews/:id/reject transitions to rejected."""
    client, pipeline = review_client
    exec_id = await _create_pending_review(pipeline)

    resp = await client.post(
        f"/reviews/{exec_id}/reject",
        json={"reason": "Output quality too low"},
        headers={"X-API-Key": "test-key-123"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"

    # Verify execution status
    detail_resp = await client.get(
        f"/executions/{exec_id}",
        headers={"X-API-Key": "test-key-123"},
    )
    assert detail_resp.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_approve_non_review_execution(review_client) -> None:
    """Approving a non-review execution returns 400."""
    client, pipeline = review_client

    # Create a non-review execution directly in the DB
    from uuid import uuid4

    exec_id = str(uuid4())
    session_factory = get_session_factory()
    async with session_factory() as session:
        await create_execution(
            session,
            execution_id=exec_id,
            process_name="normal-process",
            process_id="proc_normal",
            trigger_type="webhook",
            input_data={"data": "test"},
        )
        await update_execution(session, exec_id, status="success")

    resp = await client.post(
        f"/reviews/{exec_id}/approve",
        headers={"X-API-Key": "test-key-123"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reviews_require_auth(review_client) -> None:
    """Review endpoints require authentication."""
    client, _ = review_client
    resp = await client.get("/reviews")
    assert resp.status_code == 401
