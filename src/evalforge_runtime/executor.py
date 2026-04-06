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


def _get_max_output_tokens(model: str) -> int:
    """Return the max *output* tokens for a model.

    litellm.get_max_tokens() returns the context window (e.g. 200k for Claude),
    NOT the output limit. We use get_model_info()['max_output_tokens'] instead,
    capped at 16384 as a safety ceiling, with 8192 as the fallback default.
    """
    try:
        info = litellm.get_model_info(model)
        out = info.get("max_output_tokens")
        if out and isinstance(out, int) and out > 0:
            return min(out, 16384)
    except Exception:
        pass
    return 8192


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

        logger.info(
            "LLM execute: model=%s, process=%s, instructions_hash=%s, input_data=%s",
            model, process_name, instructions_hash, input_data,
        )
        logger.info(
            "LLM system prompt (first 500 chars): %.500s", instructions,
        )

        # Resolve max output tokens for this model.
        # litellm.get_max_tokens() returns the context window (e.g. 200k for Claude),
        # NOT the max output tokens. We need get_model_info()['max_output_tokens'] instead.
        max_tokens = _get_max_output_tokens(model)

        # Build completion kwargs. For Pydantic output schemas we use explicit tool_use
        # instead of response_format=PydanticClass. See LLMClient.complete() for the
        # full explanation — same issue applies here.
        completion_kwargs: dict[str, Any] = {
            "max_tokens": max_tokens,
            "num_retries": 3,
            "metadata": {"process_name": process_name, "instructions_version": instructions_hash},
        }

        if output_schema is not None:
            if not isinstance(output_schema, type):
                # Legacy dict format — convert to Pydantic model first
                output_schema = schema_to_model(
                    f"{process_name.replace('-', '_').title().replace('_', '')}Output",
                    output_schema,
                )
            schema = output_schema.model_json_schema()
            completion_kwargs["tools"] = [
                {"type": "function", "function": {"name": "structured_output", "parameters": schema}}
            ]
            completion_kwargs["tool_choice"] = {"type": "function", "function": {"name": "structured_output"}}
            system_content = instructions
        else:
            completion_kwargs["response_format"] = {"type": "json_object"}
            system_content = instructions + "\n\nRespond with valid JSON only."

        logger.info("LLM max_tokens for model '%s': %d, mode=%s", model, max_tokens, "tool_use" if "tools" in completion_kwargs else "json_object")

        start = time.monotonic()
        response = await litellm.acompletion(
            model=model,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": json.dumps(input_data)},
            ],
            **completion_kwargs,
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        message = response.choices[0].message
        content = message.content
        finish_reason = response.choices[0].finish_reason
        logger.info(
            "LLM response for '%s' (%dms): finish_reason=%s tokens_out=%s has_content=%s has_parsed=%s has_tool_calls=%s",
            process_name, latency_ms, finish_reason,
            response.usage.completion_tokens if response.usage else "?",
            content is not None,
            getattr(message, "parsed", None) is not None,
            bool(getattr(message, "tool_calls", None)),
        )

        if hasattr(message, "parsed") and message.parsed is not None:
            # Structured outputs API (OpenAI-style): parsed Pydantic object in message.parsed
            parsed = message.parsed
            output = parsed.model_dump() if hasattr(parsed, "model_dump") else dict(parsed)
        elif getattr(message, "tool_calls", None):
            # litellm converts Pydantic response_format to tool calls for some providers (e.g. Claude).
            raw = message.tool_calls[0].function.arguments
            logger.info("LLM tool_call arguments for '%s': %r", process_name, raw[:500] if raw else None)
            try:
                output = json.loads(raw)
            except json.JSONDecodeError as e:
                logger.error("LLM tool_call JSON parse failed for '%s': %s\nFull arguments: %r", process_name, e, raw)
                raise ValueError(f"LLM returned malformed JSON in tool_call arguments for '{process_name}': {e}") from e
        elif content is not None:
            logger.info("LLM content for '%s': %r", process_name, content[:500])
            try:
                output = json.loads(content)
            except json.JSONDecodeError as e:
                logger.error("LLM content JSON parse failed for '%s': %s\nFull content: %r", process_name, e, content)
                raise ValueError(f"LLM returned malformed JSON in content for '{process_name}': {e}") from e
        else:
            raise ValueError(
                f"LLM returned no content for '{process_name}'. "
                f"finish_reason={finish_reason!r}. "
                "Check that the model supports the requested response_format."
            )
        usage = response.usage

        logger.info(
            "LLM parsed output for '%s': %s | tokens_in=%s, tokens_out=%s",
            process_name, output,
            usage.prompt_tokens if usage else None,
            usage.completion_tokens if usage else None,
        )

        # Cost from LiteLLM's built-in model pricing
        cost: float | None = None
        try:
            cost = litellm.completion_cost(completion_response=response)
        except Exception:
            pass  # model may not have pricing info

        logger.info("LLM cost for '%s': $%s", process_name, cost)

        return ExecutionResult(
            output=output,
            llm_model=response.model or model,
            llm_tokens_in=usage.prompt_tokens if usage else None,
            llm_tokens_out=usage.completion_tokens if usage else None,
            llm_cost_usd=cost,
            llm_latency_ms=latency_ms,
            instructions_version=instructions_hash,
        )
