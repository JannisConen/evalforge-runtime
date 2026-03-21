"""Gradio-based demo UI for EvalForge generated applications.

Dynamically builds forms from process input schemas,
supports file uploads (single and batch), and uses the same
API key auth as the REST API.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

# Type strings that indicate file input
_FILE_TYPES = {"file", "fileref", "file_ref", "binary", "document", "image", "pdf"}


def create_demo(
    config: Any,
    pipeline: Any | None = None,
    port: int = 8000,
) -> Any:
    """Create a Gradio Blocks demo from the app config.

    Calls the app's own /process/{name} endpoint via HTTP,
    reusing the same auth, file handling, and error handling
    as the REST API.

    Args:
        config: AppConfig instance with process definitions.
        pipeline: Unused (kept for backward compat). Calls HTTP instead.
        port: Port the FastAPI server is running on.

    Returns:
        A gr.Blocks instance ready to be mounted on FastAPI.
    """
    import gradio as gr

    process_names = list(config.processes.keys())
    if not process_names:
        with gr.Blocks(title="EvalForge Demo", analytics_enabled=False) as demo:
            gr.Markdown("# No processes configured")
        return demo

    # Analyze each process to determine its input mode
    process_info = _analyze_processes(config)

    with gr.Blocks(title="EvalForge Demo", analytics_enabled=False) as demo:
        gr.Markdown("# EvalForge Demo UI")

        with gr.Tabs():
            # --- Single execution tab ---
            with gr.Tab("Single Execution"):
                process_dropdown = gr.Dropdown(
                    choices=process_names,
                    value=process_names[0],
                    label="Process",
                    interactive=True,
                )

                first_info = process_info[process_names[0]]

                # Expandable instructions
                first_instructions = first_info.get("instructions_full", "")
                with gr.Accordion(
                    "Instructions",
                    open=False,
                    visible=bool(first_instructions),
                ) as instructions_accordion:
                    instructions_md = gr.Markdown(
                        value=_format_instructions(first_instructions),
                    )

                with gr.Row(equal_height=False):
                    with gr.Column(scale=1):
                        # --- Form fields (shown when process has input_schema) ---
                        form_group = gr.Group(
                            visible=first_info["mode"] == "form"
                            or first_info["mode"] == "form_and_file",
                        )
                        form_fields: dict[str, Any] = {}
                        with form_group:
                            for field_name, field_type in first_info.get(
                                "form_fields", {}
                            ).items():
                                desc = _field_description(field_name, field_type)
                                if field_type in ("number", "integer", "float"):
                                    form_fields[field_name] = gr.Number(
                                        label=_humanize(field_name),
                                        info=desc,
                                    )
                                elif field_type == "boolean":
                                    form_fields[field_name] = gr.Checkbox(
                                        label=_humanize(field_name),
                                        info=desc,
                                    )
                                else:
                                    form_fields[field_name] = gr.Textbox(
                                        label=_humanize(field_name),
                                        info=desc,
                                        lines=2 if "text" in field_name.lower()
                                        or "body" in field_name.lower()
                                        or "content" in field_name.lower()
                                        or "description" in field_name.lower()
                                        or "message" in field_name.lower()
                                        else 1,
                                    )

                        # --- File upload (shown when process expects files) ---
                        file_input = gr.File(
                            label="Upload File(s)",
                            file_count="multiple",
                            type="filepath",
                            visible=first_info["mode"] in (
                                "file_only", "form_and_file",
                            ),
                        )

                        # --- JSON fallback (hidden in accordion) ---
                        with gr.Accordion("Advanced: Raw JSON", open=False,
                                          visible=first_info["mode"] == "json"):
                            json_input = gr.Textbox(
                                label="JSON Input",
                                value="{}",
                                lines=6,
                            )

                        submit_btn = gr.Button(
                            "Execute", variant="primary", size="lg"
                        )

                    with gr.Column(scale=1):
                        output_status = gr.HTML(
                            value="",
                            label="Status",
                        )
                        with gr.Tabs():
                            with gr.Tab("Result"):
                                output_pretty = gr.Markdown(
                                    label="Result",
                                    value="*Results will appear here*",
                                )
                            with gr.Tab("JSON"):
                                output_json = gr.JSON(label="Raw JSON")

                # Collect form field components in order for the handler
                form_field_names = list(first_info.get("form_fields", {}).keys())
                form_field_components = [form_fields[n] for n in form_field_names]

                # Wire up single execution
                submit_btn.click(
                    fn=_make_execute_fn(config, form_field_names),
                    inputs=[
                        process_dropdown,
                        json_input,
                        file_input,
                        *form_field_components,
                    ],
                    outputs=[output_pretty, output_json, output_status],
                )

                # Update instructions when process changes
                def _on_process_change(name):
                    info = process_info.get(name, {})
                    full = info.get("instructions_full", "")
                    return (
                        _format_instructions(full),
                        gr.update(visible=bool(full)),
                    )

                process_dropdown.change(
                    fn=_on_process_change,
                    inputs=[process_dropdown],
                    outputs=[instructions_md, instructions_accordion],
                )

            # --- Batch execution tab ---
            with gr.Tab("Batch Execution"):
                batch_process = gr.Dropdown(
                    choices=process_names,
                    value=process_names[0],
                    label="Process",
                    interactive=True,
                )
                gr.Markdown(
                    "Upload multiple files — each file becomes one execution."
                )
                batch_files = gr.File(
                    label="Upload Files",
                    file_count="multiple",
                    type="filepath",
                )
                batch_file_btn = gr.Button(
                    "Execute All Files", variant="primary"
                )
                batch_file_output = gr.Dataframe(
                    label="Results",
                    headers=["File", "Status", "Output"],
                    interactive=False,
                )

                batch_file_btn.click(
                    fn=_make_batch_file_fn(config),
                    inputs=[batch_process, batch_files],
                    outputs=[batch_file_output],
                )

            # --- Execution history tab ---
            with gr.Tab("History"):
                refresh_btn = gr.Button("Refresh")
                history_table = gr.Dataframe(
                    label="Recent Executions",
                    headers=[
                        "ID", "Process", "Status", "Started", "Duration (ms)",
                    ],
                    interactive=False,
                )
                refresh_btn.click(
                    fn=_make_history_fn(),
                    inputs=[],
                    outputs=[history_table],
                )

    return demo


def _analyze_processes(config: Any) -> dict[str, dict[str, Any]]:
    """Analyze each process to determine input mode and fields.

    Returns a dict of process_name -> {
        "mode": "file_only" | "form" | "form_and_file" | "json",
        "form_fields": {field_name: field_type, ...},  # non-file fields
        "has_file_fields": bool,
        "instructions_summary": str,
        "output_fields": [...],
    }
    """
    from evalforge_runtime.pipeline import _load_prompts, _load_output_schema

    info: dict[str, dict[str, Any]] = {}

    for name, proc_config in config.processes.items():
        module_base = name.replace("-", "_")
        input_schema = proc_config.input_schema or {}
        output_schema_model = _load_output_schema(module_base)

        # Separate file fields from form fields
        form_fields: dict[str, str] = {}
        has_file_fields = False

        for field_name, field_type in input_schema.items():
            if field_type.lower() in _FILE_TYPES or field_name.lower() in (
                "file", "files", "document", "attachment", "upload",
            ):
                has_file_fields = True
            else:
                form_fields[field_name] = field_type

        # Determine mode
        if not input_schema:
            # No schema defined — default to file upload (most common demo case)
            mode = "file_only"
        elif form_fields and has_file_fields:
            mode = "form_and_file"
        elif form_fields:
            mode = "form"
        else:
            mode = "file_only"

        # Extract output field names
        output_fields: list[str] = []
        if output_schema_model:
            try:
                output_fields = list(output_schema_model.model_fields.keys())
            except Exception:
                pass
        elif proc_config.output_schema:
            output_fields = list(proc_config.output_schema.keys())

        # Get instructions summary
        prompts = _load_prompts(module_base)
        instructions = prompts.get("system") or proc_config.instructions
        summary = ""
        if instructions:
            # First sentence or first 150 chars
            first_line = instructions.strip().split("\n")[0]
            summary = first_line[:150]

        info[name] = {
            "mode": mode,
            "form_fields": form_fields,
            "has_file_fields": has_file_fields,
            "instructions_summary": summary,
            "instructions_full": instructions or "",
            "output_fields": output_fields,
        }

    return info


def _build_description(process_name: str, info: dict[str, Any]) -> str:
    """Build a markdown description for a process."""
    lines: list[str] = []

    summary = info.get("instructions_summary", "")
    if summary:
        lines.append(f"*{summary}*")

    output_fields = info.get("output_fields", [])
    if output_fields:
        fields_str = ", ".join(f"`{f}`" for f in output_fields)
        lines.append(f"**Output:** {fields_str}")

    mode = info.get("mode", "file_only")
    if mode == "file_only":
        lines.append("**Input:** Upload a file to process")
    elif mode == "form":
        lines.append("**Input:** Fill in the form fields below")
    elif mode == "form_and_file":
        lines.append("**Input:** Fill in the form fields and upload file(s)")

    return "\n\n".join(lines) if lines else ""


def _humanize(field_name: str) -> str:
    """Convert field_name to 'Field Name'."""
    # Split on _ and camelCase
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", field_name)
    return words.replace("_", " ").replace("-", " ").title()


def _field_description(field_name: str, field_type: str) -> str:
    """Generate a helpful description for a form field."""
    type_hints = {
        "string": "Text value",
        "number": "Numeric value",
        "integer": "Whole number",
        "float": "Decimal number",
        "boolean": "True or false",
        "string[]": "Comma-separated values",
    }
    base = type_hints.get(field_type, f"Type: {field_type}")

    # Add contextual hints based on field name
    name_lower = field_name.lower()
    if "email" in name_lower:
        return "Email address"
    elif "url" in name_lower or "link" in name_lower:
        return "URL / web address"
    elif "date" in name_lower:
        return "Date (e.g. 2024-01-15)"
    elif "phone" in name_lower:
        return "Phone number"
    elif "name" in name_lower:
        return "Full name"
    elif "subject" in name_lower:
        return "Subject line"
    elif "body" in name_lower or "content" in name_lower or "text" in name_lower:
        return "Main text content"
    elif "message" in name_lower:
        return "Message text"
    elif "description" in name_lower:
        return "Description text"

    return base


def get_gradio_auth(config: Any) -> Any | None:
    """Return a Gradio auth function compatible with the app's API key auth.

    When EVALFORGE_API_KEYS is set, the Gradio login form requires:
    - Username: anything (ignored)
    - Password: a valid API key

    When no keys are configured (local dev), returns None (no auth).
    """
    valid_keys = _get_valid_api_keys()
    if not valid_keys:
        return None

    def check_auth(username: str, password: str) -> bool:
        return password in valid_keys

    return check_auth


def _get_valid_api_keys() -> list[str]:
    """Get valid API keys from environment (same logic as auth.py)."""
    raw = os.environ.get("EVALFORGE_API_KEYS", "")
    return [k.strip() for k in raw.split(",") if k.strip()]


def _get_base_url() -> str:
    """Get the base URL for the local server."""
    port = os.environ.get("UVICORN_PORT", "8000")
    return f"http://127.0.0.1:{port}"


def _get_api_headers() -> dict[str, str]:
    """Get auth headers for internal API calls (uses first available key)."""
    keys = _get_valid_api_keys()
    if keys:
        return {"X-API-Key": keys[0]}
    return {}


def _make_execute_fn(config: Any, form_field_names: list[str]):
    """Create the single-execution handler that calls /process/{name} via HTTP.

    Uses synchronous httpx so Gradio runs it in a thread pool — avoids
    event-loop deadlocks when calling back into the same uvicorn server.
    """

    def execute(
        process_name: str,
        json_str: str,
        files: list[str] | None,
        *form_values,
    ) -> tuple[str, dict | None, str]:
        import httpx

        try:
            # Build input from form fields if provided
            input_data: dict[str, Any] = {}

            # Form fields take priority
            has_form_data = False
            for name, value in zip(form_field_names, form_values):
                if value is not None and value != "" and value != 0:
                    input_data[name] = value
                    has_form_data = True

            # Fall back to JSON if no form data
            if not has_form_data and json_str and json_str.strip() not in (
                "", "{}"
            ):
                try:
                    parsed = json.loads(json_str)
                    if isinstance(parsed, dict):
                        input_data = parsed
                except json.JSONDecodeError as e:
                    return "", None, _format_status(f"Invalid JSON: {e}", is_error=True)

            url = f"{_get_base_url()}/process/{process_name}"
            headers = _get_api_headers()

            with httpx.Client(timeout=300) as client:
                if files:
                    # Multipart: send files + form data as metadata
                    file_handles = []
                    multipart_files = []
                    try:
                        for filepath in files:
                            filename = os.path.basename(filepath)
                            fh = open(filepath, "rb")
                            file_handles.append(fh)
                            multipart_files.append(("file", (filename, fh)))
                        if input_data:
                            multipart_files.append(
                                ("metadata", (None, json.dumps(input_data)))
                            )
                        resp = client.post(
                            url, files=multipart_files, headers=headers
                        )
                    finally:
                        for fh in file_handles:
                            fh.close()
                elif input_data:
                    # JSON body
                    resp = client.post(
                        url, json=input_data, headers=headers
                    )
                else:
                    return "", None, _format_status("Please provide input (upload a file or fill in fields)", is_error=True)

            if resp.status_code == 200:
                result = resp.json()
                pretty = _format_result_pretty(result)
                return pretty, result, _format_status("Success")
            else:
                detail = resp.text[:300]
                try:
                    detail = resp.json().get("detail", detail)
                except Exception:
                    pass
                return "", None, _format_status(f"Error {resp.status_code}: {detail}", is_error=True)

        except httpx.ConnectError:
            return "", None, _format_status("Cannot connect to server", is_error=True)
        except Exception as e:
            logger.exception("UI execution failed")
            return "", None, _format_status(f"Error: {e}", is_error=True)

    return execute


def _make_batch_file_fn(config: Any):
    """Create the batch file execution handler that calls /process/{name} via HTTP.

    Uses synchronous httpx (same reason as single execute — thread pool).
    """

    def batch_execute(
        process_name: str,
        files: list[str] | None,
    ) -> list[list[str]]:
        import httpx

        if not files:
            return [["No files", "—", "—"]]

        url = f"{_get_base_url()}/process/{process_name}"
        headers = _get_api_headers()
        results: list[list[str]] = []

        with httpx.Client(timeout=300) as client:
            for filepath in files:
                filename = os.path.basename(filepath)
                try:
                    with open(filepath, "rb") as f:
                        resp = client.post(
                            url,
                            files=[("file", (filename, f))],
                            headers=headers,
                        )
                    if resp.status_code == 200:
                        output = json.dumps(resp.json(), default=str)[:500]
                        results.append([filename, "Success", output])
                    else:
                        detail = resp.text[:300]
                        try:
                            detail = resp.json().get("detail", detail)
                        except Exception:
                            pass
                        results.append([filename, f"Error {resp.status_code}", str(detail)])
                except Exception as e:
                    results.append([filename, "Error", str(e)[:500]])

        return results

    return batch_execute


def _make_history_fn():
    """Create the execution history handler.

    Uses the REST API endpoint to fetch history (same thread-pool approach).
    Falls back to direct DB query if the endpoint isn't available.
    """

    def get_history() -> list[list[str]]:
        import httpx

        try:
            url = f"{_get_base_url()}/executions"
            headers = _get_api_headers()
            with httpx.Client(timeout=10) as client:
                resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                executions = data if isinstance(data, list) else data.get("executions", [])
                return [
                    [
                        str(e.get("id", ""))[:8] + "...",
                        e.get("process_name", "—"),
                        e.get("status", "—"),
                        e.get("started_at", "—"),
                        str(e.get("duration_ms", "—")),
                    ]
                    for e in executions[:50]
                ]
            return [["Error", f"HTTP {resp.status_code}", "—", "—", "—"]]
        except Exception as e:
            return [["Error", str(e)[:200], "—", "—", "—"]]

    return get_history


# --- Helpers kept for test compatibility ---


def _add_to_batch(
    items: list[dict], json_str: str
) -> tuple[list[dict], list[list[str]]]:
    """Add an item to the batch queue."""
    try:
        item = json.loads(json_str) if json_str.strip() else {}
    except json.JSONDecodeError:
        return items, _preview_items(items)

    if not isinstance(item, dict):
        return items, _preview_items(items)

    new_items = items + [item]
    return new_items, _preview_items(new_items)


def _clear_batch() -> tuple[list, list[list[str]]]:
    """Clear the batch queue."""
    return [], []


def _preview_items(items: list[dict]) -> list[list[str]]:
    """Create a preview table of batch items."""
    return [
        [str(i + 1), json.dumps(item, default=str)[:200]]
        for i, item in enumerate(items)
    ]


def _format_schema_info(
    process_name: str, schemas: dict[str, dict[str, Any]]
) -> str:
    """Format process schema info as markdown."""
    info = schemas.get(process_name)
    if not info:
        return f"**{process_name}** — no schema information available"

    lines = [f"**Process: {process_name}**\n"]

    if info.get("output_fields"):
        lines.append("**Output fields:** " + ", ".join(
            f"`{f}`" for f in info["output_fields"]
        ))

    return "\n".join(lines)


def _format_instructions(instructions: str) -> str:
    """Format instructions as Markdown, showing first 500 chars with expandable rest."""
    if not instructions:
        return ""
    text = instructions.strip()
    if len(text) <= 500:
        return text
    preview = text[:500]
    rest = text[500:]
    return (
        f"{preview}...\n\n"
        f"<details><summary><b>Show full instructions</b></summary>\n\n"
        f"...{rest}\n\n</details>"
    )


def _format_result_pretty(data: Any) -> str:
    """Convert a JSON result dict into human-readable Markdown."""
    if data is None:
        return ""
    if not isinstance(data, dict):
        return str(data)

    lines: list[str] = []
    for key, value in data.items():
        label = _humanize(key)
        if isinstance(value, dict):
            # Nested object — render as indented block
            lines.append(f"**{label}:**")
            for k, v in value.items():
                lines.append(f"  - **{_humanize(k)}:** {v}")
        elif isinstance(value, list):
            lines.append(f"**{label}:**")
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    parts = ", ".join(f"{_humanize(k)}: {v}" for k, v in item.items())
                    lines.append(f"  {i + 1}. {parts}")
                else:
                    lines.append(f"  - {item}")
        else:
            lines.append(f"**{label}:** {value}")

    return "\n\n".join(lines) if lines else "*Empty result*"


def _format_status(status: str, is_error: bool = False) -> str:
    """Format status as styled HTML with badge."""
    if is_error:
        color = "#ef4444"
        bg = "rgba(239, 68, 68, 0.15)"
        icon = "\u2716"
    else:
        color = "#22c55e"
        bg = "rgba(34, 197, 94, 0.15)"
        icon = "\u2714"
    return (
        f'<div style="display:flex;align-items:center;gap:8px;padding:8px 12px;'
        f'border-radius:8px;background:{bg};border:1px solid {color}30;'
        f'font-weight:500;color:{color};font-size:14px">'
        f'{icon} {status}</div>'
    )


_CUSTOM_CSS = """
.gradio-container {
    max-width: 1000px !important;
}
"""
