from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from tfr.config import AgentsConfig, MainConfig, WorldsConfig

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "config.schema.json": MainConfig,
    "worlds.schema.json": WorldsConfig,
    "agents.schema.json": AgentsConfig,
}


def generate_schemas(destination: Path | str) -> tuple[Path, ...]:
    directory = Path(destination)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, model in SCHEMA_MODELS.items():
        path = directory / filename
        schema = model.model_json_schema(by_alias=True, mode="validation")
        path.write_text(
            json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return tuple(written)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate TFR JSON Schemas from config models.")
    parser.add_argument("destination", type=Path, nargs="?", default=Path("schemas"))
    args = parser.parse_args()
    for path in generate_schemas(args.destination):
        print(path)


if __name__ == "__main__":
    main()
