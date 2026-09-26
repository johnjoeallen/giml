"""Hard rule 4 (spec 2): no AI API calls, ever. giml's code imports no AI client and names no AI endpoint."""

import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "giml"
FORBIDDEN = re.compile(r"\b(anthropic|openai|google\.generativeai|cohere|mistralai|ollama|langchain|litellm)\b|api\.anthropic\.com|api\.openai\.com",
                       re.IGNORECASE)  # fmt: skip


def test_no_source_file_imports_an_ai_client_or_names_an_ai_endpoint():
    offenders = []
    for path in sorted(SOURCE.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN.search(line):
                offenders.append(f"{path.relative_to(SOURCE)}:{number}: {line.strip()}")
    assert offenders == []


def test_the_declared_dependencies_include_no_ai_client():
    project = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert not FORBIDDEN.search(project)
