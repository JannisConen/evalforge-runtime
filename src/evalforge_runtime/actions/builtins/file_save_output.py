"""Save process output to a file."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from evalforge_runtime.actions.base import BaseAction


class FileSaveOutputAction(BaseAction):
    type = "file.save_output"

    async def run(self, *, trigger: Any, output: dict, secrets: dict[str, str], **kwargs: Any) -> None:
        filename = self.render_template(self.config.get("filename", "output"), output)
        fmt = self.config.get("format", "json")

        if fmt == "csv":
            content = self._to_csv(output)
            ext = "csv"
        else:
            content = json.dumps(output, indent=2, ensure_ascii=False)
            ext = "json"

        full_path = f"outputs/{filename}.{ext}"

        # Write to local storage
        from pathlib import Path
        path = Path(full_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _to_csv(self, output: dict[str, Any]) -> str:
        """Convert flat output dict to CSV."""
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(output.keys()))
        writer.writeheader()
        writer.writerow(output)
        return buf.getvalue()
