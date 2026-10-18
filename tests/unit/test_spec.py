"""The committed OpenAPI documents must match the ones generated from the pydantic models."""

import importlib.util
from pathlib import Path

GENERATOR = Path(__file__).resolve().parents[2] / 'scripts' / 'gen_openapi.py'


def test_committed_openapi_documents_are_current() -> None:
    """Run `uv run python scripts/gen_openapi.py` if this fails."""
    spec = importlib.util.spec_from_file_location('gen_openapi', GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for path, text in module.documents().items():
        assert path.read_text() == text, f'{path.name} is stale'
