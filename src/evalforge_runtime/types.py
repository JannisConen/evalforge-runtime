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


class ExecutionResult(BaseModel):
    """Result from the LLM executor."""

    output: dict[str, Any]
    llm_model: str
    llm_tokens_in: int | None = None
    llm_tokens_out: int | None = None
    llm_cost_usd: float | None = None
    llm_latency_ms: int | None = None
    instructions_version: str | None = None


class TriggerContext(BaseModel):
    """Context about what triggered an execution."""

    type: str  # "webhook", "manual", "schedule", "process_chain"
    ref: str | None = None
    source_execution_id: str | None = None


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
    ):
        self.llm = llm
        self.prompts = prompts
        self.output_schema = output_schema
        self.secrets = secrets
        self.storage = storage
        self.process_name = process_name
        self.process_id = process_id
        self.trigger = trigger

    @property
    def instructions(self) -> str | None:
        """Backward compat: returns prompts['system'] if it exists."""
        return self.prompts.get("system")


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

        if response_format is None:
            _format: Any = {"type": "json_object"}
            system_content = instructions + "\n\nRespond with valid JSON only."
        elif isinstance(response_format, dict):
            _format = response_format
            system_content = instructions + "\n\nRespond with valid JSON only."
        else:
            # Pydantic model class — structured output
            _format = response_format
            system_content = instructions

        start = time.monotonic()
        response = await litellm.acompletion(
            model=_model,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": json.dumps(input_data)},
            ],
            response_format=_format,
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        content = response.choices[0].message.content

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

        return json.loads(content)
