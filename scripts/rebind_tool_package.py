"""Relocate existing editable-install metadata without downloading dependencies."""
from __future__ import annotations

import ast
import base64
import csv
import hashlib
import importlib.metadata
import io
import json
from pathlib import Path
import shutil
import sysconfig


def rebind() -> None:
    project = Path(__file__).resolve().parents[1]
    package = project / "tools/redbook_tools"
    if not (package / "pyproject.toml").is_file():
        raise RuntimeError("Bundled redbook_tools is missing")
    distribution = importlib.metadata.distribution("redbook-tools")
    site = Path(sysconfig.get_path("purelib")).resolve()
    if not site.is_relative_to(project / ".venv"):
        raise RuntimeError("Run with this agent's .venv Python")
    files = list(distribution.files or ())
    finder_name = next(str(f) for f in files if str(f).startswith("__editable___") and str(f).endswith("_finder.py"))
    direct_name = next(str(f) for f in files if str(f).endswith(".dist-info/direct_url.json"))
    record_name = next(str(f) for f in files if str(f).endswith(".dist-info/RECORD"))
    finder = site / finder_name
    backup = project / "data/backups/local-tools/editable-install"
    backup.mkdir(parents=True, exist_ok=True)
    for name in (finder_name, direct_name, record_name):
        target = backup / Path(name).name
        if not target.exists():
            shutil.copy2(site / name, target)
    tree = ast.parse(finder.read_text(encoding="utf-8"))
    updated = set()
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "MAPPING":
                mapping = ast.literal_eval(node.value)
                if set(mapping) != {"apps", "src", "redbook_tools"}:
                    raise RuntimeError("Unexpected editable package mapping")
                node.value = ast.parse(repr({key: str(package / key) for key in mapping}), mode="eval").body
                updated.add("MAPPING")
            elif node.target.id == "NAMESPACES":
                node.value = ast.parse(repr({"apps": [str(package / "apps")]}), mode="eval").body
                updated.add("NAMESPACES")
    if updated != {"MAPPING", "NAMESPACES"}:
        raise RuntimeError("Editable finder format is not supported")
    finder.write_text(ast.unparse(ast.fix_missing_locations(tree)) + "\n", encoding="utf-8")
    direct = site / direct_name
    metadata = json.loads(direct.read_text(encoding="utf-8"))
    metadata["url"] = package.as_uri()
    direct.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
    record = site / record_name
    rows = list(csv.reader(io.StringIO(record.read_text(encoding="utf-8"))))
    for row in rows:
        if row[0] in {finder_name, direct_name}:
            content = (site / row[0]).read_bytes()
            row[1] = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
            row[2] = str(len(content))
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    record.write_text(output.getvalue(), encoding="utf-8")
    print(f"Editable tool package rebound to {package}")


if __name__ == "__main__":
    rebind()
