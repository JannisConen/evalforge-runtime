"""LLM execution via LiteLLM with cost and latency tracking."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any

import litellm

from evalforge_runtime.config import ObservabilityConfig
from evalforge_runtime.types import ExecutionResult, schema_to_model

logger = logging.getLogger(__name__)


class Executor:
    """Calls an LLM via LiteLLM and tracks cost/latency."""

    def __init__(
        self,
        default_model: str,
        observability: ObservabilityConfig | None = None,
    ):
        self.default_model = default_model
        if observability and observability.langfuse.enabled:
            self._setup_langfuse(observability)

    def _setup_langfuse(self, observability: ObservabilityConfig) -> None:
        """Configure LiteLLM Langfuse callbacks for observability."""
        langfuse_cfg = observability.langfuse

        if langfuse_cfg.host:
            os.environ.setdefault("LANGFUSE_HOST", langfuse_cfg.host)

        # LiteLLM reads LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY from env
        # (injected by SecretManager before Executor is created)
        litellm.success_callback = list(set(litellm.success_callback + ["langfuse"]))
        litellm.failure_callback = list(set(litellm.failure_callback + ["langfuse"]))
        logger.info("Langfuse observability enabled via LiteLLM callbacks")

    async def execute(
        self,
        instructions: str,
        input_data: dict,
        process_name: str,
        model_override: str | None = None,
        output_schema: type | dict[str, str] | None = None,
    ) -> ExecutionResult:
        """Execute an LLM call and return the result with tracking data.

        Args:
            output_schema: Either a Pydantic model class (from output_schema.py),
                a dict mapping field names to type strings (legacy), or None.
        """
        model = model_override or self.default_model
        instructions_hash = hashlib.sha256(instructions.encode()).hexdigest()[:16]

        # Build response_format and system prompt
        if output_schema is not None:
            if isinstance(output_schema, type):
                # Pydantic model class — pass directly to LiteLLM
                response_format: Any = output_schema
            else:
                # Legacy dict format — convert to Pydantic model
                response_format = schema_to_model(
                    f"{process_name.replace('-', '_').title().replace('_', '')}Output",
                    output_schema,
                )
            system_content = instructions
        else:
            response_format = {"type": "json_object"}
            system_content = instructions + "\n\nRespond with valid JSON only."

        start = time.monotonic()
        response = await litellm.acompletion(
            model=model,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": json.dumps(input_data)},
            ],
            response_format=response_format,
            metadata={
                "process_name": process_name,
                "instructions_version": instructions_hash,
            },
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        content = response.choices[0].message.content
        output = json.loads(content)
        usage = response.usage

        # Cost from LiteLLM's built-in model pricing
        cost: float | None = None
        try:
            cost = litellm.completion_cost(completion_response=response)
        except Exception:
            pass  # model may not have pricing info

        return ExecutionResult(
            output=output,
            llm_model=response.model or model,
            llm_tokens_in=usage.prompt_tokens if usage else None,
            llm_tokens_out=usage.completion_tokens if usage else None,
            llm_cost_usd=cost,
            llm_latency_ms=latency_ms,
            instructions_version=instructions_hash,
        )
