from __future__ import annotations

import json
from pathlib import Path

from tfr.schema import SCHEMA_MODELS, generate_schemas


def test_generates_strict_schemas_with_schema_directive(tmp_path: Path) -> None:
    paths = generate_schemas(tmp_path)

    assert {path.name for path in paths} == set(SCHEMA_MODELS)
    for path in paths:
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema["additionalProperties"] is False
        assert "$schema" in schema["properties"]


def test_checked_in_schemas_match_models(tmp_path: Path) -> None:
    generated = generate_schemas(tmp_path)

    for generated_path in generated:
        checked_in_path = Path("schemas") / generated_path.name
        assert checked_in_path.read_text(encoding="utf-8") == generated_path.read_text(
            encoding="utf-8"
        )
