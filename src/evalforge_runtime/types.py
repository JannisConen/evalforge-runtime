"""Shared types for the EvalForge runtime."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, create_model


class FileRef(BaseModel):
    """Reference to a stored file. Never embed file content in JSON."""

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["local", "s3"]
    key: str
    filename: str
    size: int
    mime_type: str = Field(alias="mimeType")
    extension: str


class FileInput(BaseModel):
    """File input from the EvalForge test runner or trigger.

    Contains file metadata and optionally base64-encoded content.
    Use ``file_content()`` to decode text or ``file_bytes()`` for raw bytes.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: str | None = None  # "local", "s3", or "url" — absent after runtime resolution
    key: str | None = None  # storage path — absent after runtime resolution
    filename: str  # original filename
    size: int | None = None
    mime_type: str | None = Field(default=None, alias="mimeType")
    extension: str | None = None
    data: str | None = None  # base64-encoded file content (sent by EvalForge test runner)
    url: str | None = None  # absolute download URL
    path: str | None = None  # local filesystem path (set by runtime after file resolution)
    content: str | None = None  # pre-decoded text content (populated by runtime for text files)

    def file_content(self, encoding: str = "utf-8") -> str:
        """Decode base64 data to string."""
        if not self.data:
            raise ValueError(f"No data in FileInput for {self.filename}")
        import base64

        return base64.b64decode(self.data).decode(encoding)

    def file_bytes(self) -> bytes:
        """Decode base64 data to bytes."""
        if not self.data:
            raise ValueError(f"No data in FileInput for {self.filename}")
        import base64

        return base64.b64decode(self.data)


class ExecutionResult(BaseModel):
    """Result from the LLM executor."""

    output: dict[str, Any]
    llm_model: str
    llm_tokens_in: int | None = None
    llm_tokens_out: int | None = None
    llm_cost_usd: float | None = None
    llm_latency_ms: int | None = None
    instructions_version: str | None = None


class HumanReviewRequested(Exception):
    """Raised from execution code to request human review."""

    def __init__(
        self,
        output: dict[str, Any],
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        self.output = output
        self.reason = reason
        self.metadata = metadata
        super().__init__(f"Human review requested: {reason or 'no reason given'}")


class TriggerContext(BaseModel):
    """Context about what triggered an execution."""

    type: str  # "webhook", "manual", "schedule", "process_chain"
    ref: str | None = None
    source_execution_id: str | None = None
    request_id: str | None = None  # Groups related executions across chains


# --- Three-step process model base classes ---

TInput = TypeVar("TInput", bound=BaseModel)
TOutput = TypeVar("TOutput", bound=BaseModel)


class Before(ABC, Generic[TInput, TOutput]):
    """Prepares input for a process by reshaping upstream output.

    Used in process chaining: maps the output of an upstream process
    to the input of a downstream process.
    """

    @abstractmethod
    def prepare(self, source: TInput) -> TOutput:
        """Map source process result to target process input."""
        ...

    def condition(self, source: TInput) -> bool:
        """Return False to skip the downstream process for this item.
        Default: always execute.
        """
        return True


class ExecutionContext:
    """Provided by the runtime to every Execution.run() call."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        prompts: dict[str, str],
        output_schema: type[BaseModel] | None = None,
        secrets: dict[str, str],
        storage: Any,
        process_name: str,
        process_id: str,
        trigger: TriggerContext,
        execution_id: str | None = None,
        request_id: str | None = None,
    ):
        self.llm = llm
        self.prompts = prompts
        self.output_schema = output_schema
        self.secrets = secrets
        self.storage = storage
        self.process_name = process_name
        self.process_id = process_id
        self.trigger = trigger
        self.execution_id = execution_id
        self.request_id = request_id

    @property
    def instructions(self) -> str | None:
        """Backward compat: returns prompts['system'] if it exists."""
        return self.prompts.get("system")

    async def request_review(
        self,
        output: dict[str, Any],
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Request human review. Raises HumanReviewRequested which the pipeline catches."""
        raise HumanReviewRequested(output=output, reason=reason, metadata=metadata)


class Execution(ABC):
    """Core execution logic for a process."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}

    @abstractmethod
    async def run(self, input_data: dict, context: ExecutionContext) -> dict:
        """Execute the process logic.

        Args:
            input_data: The prepared input (after before.py, if any).
            context: Runtime context with access to LLM, instructions,
                     secrets, storage, and other runtime services.

        Returns:
            The output dict for this process.
        """
        ...


class After(ABC):
    """Post-processing executed after the process produces output."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}

    @abstractmethod
    async def execute(self, trigger: TriggerContext, output: dict) -> None:
        """Execute the post-processing.

        Args:
            trigger: Context about what triggered this execution.
            output: The process output. If review was enabled,
                    this is the (possibly edited) approved output.
        """
        ...


_SCHEMA_TYPE_MAP: dict[str, type] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "file": FileInput,
    "FileRef": FileInput,
}


def _resolve_type(type_str: str) -> type:
    """Resolve a schema type string to a Python type."""
    if type_str.endswith("[]"):
        inner = _SCHEMA_TYPE_MAP.get(type_str[:-2], Any)
        return list[inner]  # type: ignore[valid-type]
    return _SCHEMA_TYPE_MAP.get(type_str, Any)


def schema_to_model(name: str, schema: dict[str, str]) -> type[BaseModel]:
    """Dynamically create a Pydantic model from a schema dict.

    Schema maps field names to type strings:
        {"category": "string", "priority": "number", "tags": "string[]"}
    """
    fields: dict[str, Any] = {}
    for field_name, type_str in schema.items():
        fields[field_name] = (_resolve_type(type_str), ...)

    return create_model(name, **fields)


def _get_max_output_tokens(model: str) -> int:
    """Return the max *output* tokens for a model.

    litellm.get_max_tokens() returns the context window (e.g. 200k for Claude),
    NOT the output limit. We use get_model_info()['max_output_tokens'] instead,
    capped at 16384 as a safety ceiling, with 8192 as the fallback default.
    """
    try:
        import litellm
        info = litellm.get_model_info(model)
        out = info.get("max_output_tokens")
        if out and isinstance(out, int) and out > 0:
            return min(out, 16384)
    except Exception:
        pass
    return 8192


class LLMClient:
    """Wrapper for LLM calls available inside ExecutionContext."""

    def __init__(self, default_model: str):
        self._default_model = default_model
        # Tracking: accumulated across all calls in one execution
        self.last_model: str | None = None
        self.total_tokens_in: int = 0
        self.total_tokens_out: int = 0
        self.total_cost_usd: float = 0.0
        self.total_latency_ms: int = 0

    async def complete(
        self,
        instructions: str,
        input_data: dict[str, Any],
        model: str | None = None,
        response_format: type[BaseModel] | dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Call an LLM and return parsed JSON output.

        Args:
            response_format: Either a Pydantic model class for structured output,
                a dict like {"type": "json_object"}, or None for default behavior.
        """
        import json
        import time

        import litellm

        _model = model or self._default_model

        import logging as _logging
        _log = _logging.getLogger(__name__)

        # Resolve max output tokens (get_max_tokens returns context window, not output limit).
        max_tokens = _get_max_output_tokens(_model)

        # Build completion kwargs based on response_format type.
        #
        # For Pydantic models we use explicit tool_use (tools + tool_choice) instead of
        # response_format=PydanticClass. With LiteLLM 1.83+ / Anthropic, passing a Pydantic
        # model as response_format triggers Anthropic's native structured output mode, which
        # returns a plain JSON string in message.content. The model then self-terminates the
        # JSON string when the content contains ASCII " characters (e.g. quoted error messages),
        # producing a valid but truncated output.
        #
        # With tool_use the Anthropic API encodes the arguments server-side, so all characters
        # including " are properly escaped regardless of what the model generates.
        completion_kwargs: dict[str, Any] = {"max_tokens": max_tokens, "num_retries": 3}

        if response_format is None or isinstance(response_format, dict):
            completion_kwargs["response_format"] = response_format or {"type": "json_object"}
            system_content = instructions + "\n\nRespond with valid JSON only."
        else:
            # Pydantic model — use explicit tool_use for server-enforced JSON encoding
            schema = response_format.model_json_schema()
            completion_kwargs["tools"] = [
                {"type": "function", "function": {"name": "structured_output", "parameters": schema}}
            ]
            completion_kwargs["tool_choice"] = {"type": "function", "function": {"name": "structured_output"}}
            system_content = instructions

        _log.info(
            "LLMClient.complete: model=%s max_tokens=%d mode=%s",
            _model, max_tokens, "tool_use" if "tools" in completion_kwargs else "json_object",
        )

        start = time.monotonic()
        response = await litellm.acompletion(
            model=_model,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": json.dumps(input_data)},
            ],
            **completion_kwargs,
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        message = response.choices[0].message
        content = message.content

        # Track usage
        self.last_model = response.model or _model
        usage = response.usage
        if usage:
            self.total_tokens_in += usage.prompt_tokens or 0
            self.total_tokens_out += usage.completion_tokens or 0
        self.total_latency_ms += latency_ms
        try:
            cost = litellm.completion_cost(completion_response=response)
            if cost:
                self.total_cost_usd += cost
        except Exception:
            pass

        finish_reason = response.choices[0].finish_reason
        _log.info(
            "LLMClient.complete: finish_reason=%s tokens_out=%s",
            finish_reason,
            response.usage.completion_tokens if response.usage else "?",
        )

        # 1. Try .parsed (litellm structured output via Pydantic model)
        parsed = getattr(message, "parsed", None)
        if parsed is not None:
            _log.info("LLMClient.complete: using .parsed path")
            return parsed.model_dump() if hasattr(parsed, "model_dump") else dict(parsed)

        # 2. Try tool_calls (litellm maps Anthropic tool-use structured output here)
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            raw = tool_calls[0].function.arguments
            _log.info("LLMClient.complete: using tool_calls path, raw=%r", raw[:200] if raw else None)
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                _log.error("LLMClient.complete: tool_calls JSON parse failed: %s\nFull arguments: %r", e, raw)
                raise ValueError(f"LLM returned malformed JSON in tool_call arguments: {e}") from e

        # 3. Plain content — strip markdown fences if present
        if content:
            raw = content.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1]
                raw = raw.rsplit("```", 1)[0]
                raw = raw.strip()
            _log.info("LLMClient.complete: using content path, raw=%r", raw[:200])
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                _log.error("LLMClient.complete: content JSON parse failed: %s\nFull content: %r", e, raw)
                raise ValueError(f"LLM returned malformed JSON in content: {e}") from e

        raise ValueError(f"LLM returned no content (finish_reason={finish_reason!r})")
