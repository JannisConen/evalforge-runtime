"""Tests for the Gradio demo UI module."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from evalforge_runtime.config import AppConfig


@pytest.fixture
def ui_config() -> AppConfig:
    """Create a config with UI enabled and test processes."""
    return AppConfig(
        project={"id": "test-ui"},
        processes={
            "doc-analyzer": {
                "process_id": "proc_1",
                "instructions": "Analyze the document.",
                "trigger": {"type": "webhook"},
                "output_schema": {"summary": "string", "category": "string"},
            },
            "email-classifier": {
                "process_id": "proc_2",
                "instructions": "Classify the email.",
                "trigger": {"type": "webhook"},
                "input_schema": {
                    "email_subject": "string",
                    "email_body": "string",
                    "sender_email": "string",
                    "priority": "number",
                },
            },
        },
        ui={"enabled": True},
    )


@pytest.fixture
def ui_config_with_files() -> AppConfig:
    """Config with a process that takes file + form fields."""
    return AppConfig(
        project={"id": "test-mixed"},
        processes={
            "invoice-processor": {
                "process_id": "proc_3",
                "instructions": "Extract data from the invoice.",
                "trigger": {"type": "webhook"},
                "input_schema": {
                    "file": "file",
                    "vendor_name": "string",
                    "amount": "number",
                },
                "output_schema": {"total": "number", "line_items": "string[]"},
            },
        },
        ui={"enabled": True},
    )


@pytest.fixture
def ui_config_disabled() -> AppConfig:
    """Config with UI disabled."""
    return AppConfig(
        project={"id": "test-no-ui"},
        processes={
            "test": {
                "process_id": "proc_1",
                "instructions": "Test.",
                "trigger": {"type": "webhook"},
            },
        },
        ui={"enabled": False},
    )


class TestUIConfig:
    """Test UIConfig in AppConfig."""

    def test_ui_enabled_by_default(self) -> None:
        config = AppConfig(project={"id": "t"}, processes={})
        assert config.ui.enabled is True
        assert config.ui.path == "/ui"

    def test_ui_disabled(self, ui_config_disabled: AppConfig) -> None:
        assert ui_config_disabled.ui.enabled is False

    def test_input_schema_on_process(self) -> None:
        config = AppConfig(
            project={"id": "t"},
            processes={
                "test": {
                    "process_id": "p1",
                    "trigger": {"type": "webhook"},
                    "input_schema": {"email": "string", "file": "file"},
                },
            },
        )
        assert config.processes["test"].input_schema == {
            "email": "string",
            "file": "file",
        }


class TestAnalyzeProcesses:
    """Test process analysis for UI rendering."""

    def test_no_schema_defaults_to_file_only(self, ui_config: AppConfig) -> None:
        from evalforge_runtime.ui import _analyze_processes

        info = _analyze_processes(ui_config)
        # doc-analyzer has no input_schema -> file_only
        assert info["doc-analyzer"]["mode"] == "file_only"

    def test_form_fields_detected(self, ui_config: AppConfig) -> None:
        from evalforge_runtime.ui import _analyze_processes

        info = _analyze_processes(ui_config)
        # email-classifier has input_schema with text fields -> form
        assert info["email-classifier"]["mode"] == "form"
        assert "email_subject" in info["email-classifier"]["form_fields"]
        assert "email_body" in info["email-classifier"]["form_fields"]
        assert "priority" in info["email-classifier"]["form_fields"]

    def test_mixed_form_and_file(self, ui_config_with_files: AppConfig) -> None:
        from evalforge_runtime.ui import _analyze_processes

        info = _analyze_processes(ui_config_with_files)
        assert info["invoice-processor"]["mode"] == "form_and_file"
        assert info["invoice-processor"]["has_file_fields"] is True
        # "file" field should not be in form_fields
        assert "file" not in info["invoice-processor"]["form_fields"]
        # non-file fields should be in form_fields
        assert "vendor_name" in info["invoice-processor"]["form_fields"]
        assert "amount" in info["invoice-processor"]["form_fields"]

    def test_output_fields_extracted(self, ui_config: AppConfig) -> None:
        from evalforge_runtime.ui import _analyze_processes

        info = _analyze_processes(ui_config)
        assert "summary" in info["doc-analyzer"]["output_fields"]
        assert "category" in info["doc-analyzer"]["output_fields"]

    def test_instructions_summary(self, ui_config: AppConfig) -> None:
        from evalforge_runtime.ui import _analyze_processes

        info = _analyze_processes(ui_config)
        assert "Analyze the document" in info["doc-analyzer"]["instructions_summary"]


class TestBuildDescription:
    """Test process description generation."""

    def test_file_only_description(self) -> None:
        from evalforge_runtime.ui import _build_description

        desc = _build_description("test", {
            "mode": "file_only",
            "instructions_summary": "Process documents.",
            "output_fields": ["result"],
        })
        assert "Upload a file" in desc
        assert "Process documents" in desc
        assert "`result`" in desc

    def test_form_description(self) -> None:
        from evalforge_runtime.ui import _build_description

        desc = _build_description("test", {
            "mode": "form",
            "instructions_summary": "",
            "output_fields": [],
        })
        assert "form fields" in desc


class TestHumanize:
    """Test field name humanization."""

    def test_snake_case(self) -> None:
        from evalforge_runtime.ui import _humanize

        assert _humanize("email_subject") == "Email Subject"

    def test_camel_case(self) -> None:
        from evalforge_runtime.ui import _humanize

        assert _humanize("emailSubject") == "Email Subject"

    def test_kebab_case(self) -> None:
        from evalforge_runtime.ui import _humanize

        assert _humanize("email-subject") == "Email Subject"


class TestFieldDescription:
    """Test auto-generated field descriptions."""

    def test_email_field(self) -> None:
        from evalforge_runtime.ui import _field_description

        assert "Email" in _field_description("sender_email", "string")

    def test_url_field(self) -> None:
        from evalforge_runtime.ui import _field_description

        assert "URL" in _field_description("page_url", "string")

    def test_generic_string(self) -> None:
        from evalforge_runtime.ui import _field_description

        assert "Text" in _field_description("foo", "string")

    def test_number_type(self) -> None:
        from evalforge_runtime.ui import _field_description

        assert "Numeric" in _field_description("count", "number")


class TestGetGradioAuth:
    """Test auth compatibility between Gradio UI and API key auth."""

    def test_no_keys_returns_none(self) -> None:
        from evalforge_runtime.ui import get_gradio_auth

        with patch.dict(os.environ, {"EVALFORGE_API_KEYS": ""}, clear=False):
            auth = get_gradio_auth(MagicMock())
            assert auth is None

    def test_with_keys_returns_checker(self) -> None:
        from evalforge_runtime.ui import get_gradio_auth

        with patch.dict(
            os.environ, {"EVALFORGE_API_KEYS": "key1,key2"}, clear=False
        ):
            auth = get_gradio_auth(MagicMock())
            assert auth is not None
            assert auth("anyuser", "key1") is True
            assert auth("admin", "key2") is True
            assert auth("admin", "wrong") is False

    def test_keys_trimmed(self) -> None:
        from evalforge_runtime.ui import get_gradio_auth

        with patch.dict(
            os.environ, {"EVALFORGE_API_KEYS": " key1 , key2 "}, clear=False
        ):
            auth = get_gradio_auth(MagicMock())
            assert auth("u", "key1") is True
            assert auth("u", "key2") is True


class TestBatchHelpers:
    """Test batch queue management functions."""

    def test_add_to_batch(self) -> None:
        from evalforge_runtime.ui import _add_to_batch

        items, preview = _add_to_batch([], '{"email": "test@example.com"}')
        assert len(items) == 1
        assert items[0] == {"email": "test@example.com"}
        assert len(preview) == 1
        assert preview[0][0] == "1"

    def test_add_to_batch_invalid_json(self) -> None:
        from evalforge_runtime.ui import _add_to_batch

        existing = [{"a": 1}]
        items, preview = _add_to_batch(existing, "not json")
        assert items == existing

    def test_add_to_batch_non_dict(self) -> None:
        from evalforge_runtime.ui import _add_to_batch

        items, preview = _add_to_batch([], '"just a string"')
        assert items == []

    def test_clear_batch(self) -> None:
        from evalforge_runtime.ui import _clear_batch

        items, preview = _clear_batch()
        assert items == []
        assert preview == []

    def test_preview_items(self) -> None:
        from evalforge_runtime.ui import _preview_items

        items = [{"a": 1}, {"b": 2}]
        preview = _preview_items(items)
        assert len(preview) == 2
        assert preview[0][0] == "1"
        assert preview[1][0] == "2"


class TestHTTPHelpers:
    """Test HTTP helper functions for API calls."""

    def test_get_base_url_default(self) -> None:
        from evalforge_runtime.ui import _get_base_url

        with patch.dict(os.environ, {}, clear=False):
            # Remove UVICORN_PORT if set
            os.environ.pop("UVICORN_PORT", None)
            assert _get_base_url() == "http://127.0.0.1:8000"

    def test_get_base_url_custom_port(self) -> None:
        from evalforge_runtime.ui import _get_base_url

        with patch.dict(os.environ, {"UVICORN_PORT": "9000"}, clear=False):
            assert _get_base_url() == "http://127.0.0.1:9000"

    def test_get_api_headers_no_keys(self) -> None:
        from evalforge_runtime.ui import _get_api_headers

        with patch.dict(os.environ, {"EVALFORGE_API_KEYS": ""}, clear=False):
            assert _get_api_headers() == {}

    def test_get_api_headers_with_keys(self) -> None:
        from evalforge_runtime.ui import _get_api_headers

        with patch.dict(
            os.environ, {"EVALFORGE_API_KEYS": "key1,key2"}, clear=False
        ):
            headers = _get_api_headers()
            assert headers == {"X-API-Key": "key1"}


class TestFormatSchemaInfo:
    """Test schema info formatting."""

    def test_format_with_fields(self) -> None:
        from evalforge_runtime.ui import _format_schema_info

        schemas = {
            "doc-analyzer": {
                "output_fields": ["summary", "category"],
            }
        }
        result = _format_schema_info("doc-analyzer", schemas)
        assert "doc-analyzer" in result
        assert "`summary`" in result
        assert "`category`" in result

    def test_format_unknown_process(self) -> None:
        from evalforge_runtime.ui import _format_schema_info

        result = _format_schema_info("unknown", {})
        assert "unknown" in result
        assert "no schema" in result


class TestCreateDemo:
    """Test Gradio demo creation (requires gradio installed)."""

    @pytest.fixture
    def _skip_no_gradio(self):
        pytest.importorskip("gradio")

    @pytest.mark.usefixtures("_skip_no_gradio")
    def test_create_demo_returns_blocks(self, ui_config: AppConfig) -> None:
        import gradio as gr

        from evalforge_runtime.ui import create_demo

        demo = create_demo(ui_config, pipeline=None)
        assert isinstance(demo, gr.Blocks)

    @pytest.mark.usefixtures("_skip_no_gradio")
    def test_create_demo_no_processes(self) -> None:
        import gradio as gr

        from evalforge_runtime.ui import create_demo

        config = AppConfig(project={"id": "empty"}, processes={})
        demo = create_demo(config, pipeline=None)
        assert isinstance(demo, gr.Blocks)
