#!/usr/bin/env python3
"""Check drift between RTBioScan parameter consumers, defaults, overrides, and registry."""

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN_NF = ROOT / "main.nf"
NEXTFLOW_CONFIG = ROOT / "nextflow.config"
CONF_DIR = ROOT / "conf"
REGISTRY_PATH = ROOT / "docs" / "params_reference.json"


PARAM_BLOCK_RE = re.compile(r"^\s*params\s*\{\s*$")
PARAM_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
PARAM_DOT_RE = re.compile(r"\bparams\.([A-Za-z_][A-Za-z0-9_]*)\b")
PARAM_CONTAINS_RE = re.compile(r"""params\.containsKey\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\)""")


def load_registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def parse_params_block(path: Path) -> set[str]:
    names: set[str] = set()
    in_params = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if PARAM_BLOCK_RE.match(line):
            in_params = True
            continue
        if in_params and stripped == "}":
            in_params = False
            continue
        if not in_params:
            continue
        if stripped.startswith("//"):
            continue
        match = PARAM_ASSIGN_RE.match(line)
        if match:
            names.add(match.group(1))
    return names


def parse_main_params(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    names = set(PARAM_DOT_RE.findall(text))
    names.update(PARAM_CONTAINS_RE.findall(text))
    names.discard("containsKey")
    return names


def main() -> int:
    registry = load_registry()
    registry_params = set(registry.get("params", {}).keys())
    derived_values = set(registry.get("derived_runtime_values", {}).keys())

    declared_defaults = parse_params_block(NEXTFLOW_CONFIG)
    declared_overrides: set[str] = set()
    for conf_path in sorted(CONF_DIR.glob("*.config")):
        declared_overrides.update(parse_params_block(conf_path))
    main_params = parse_main_params(MAIN_NF)

    errors: list[str] = []

    missing_from_registry = sorted(main_params - registry_params - derived_values)
    if missing_from_registry:
        errors.append(
            "main.nf uses params missing from docs/params_reference.json: "
            + ", ".join(missing_from_registry)
        )

    unknown_in_defaults = sorted(declared_defaults - registry_params)
    if unknown_in_defaults:
        errors.append(
            "nextflow.config declares params missing from docs/params_reference.json: "
            + ", ".join(unknown_in_defaults)
        )

    unknown_in_overrides = sorted(declared_overrides - registry_params)
    if unknown_in_overrides:
        errors.append(
            "conf/*.config overrides params missing from docs/params_reference.json: "
            + ", ".join(unknown_in_overrides)
        )

    missing_default_owners = sorted(
        name
        for name, metadata in registry.get("params", {}).items()
        if metadata.get("default_owner") == "nextflow.config" and name not in declared_defaults
    )
    if missing_default_owners:
        errors.append(
            "Registry params owned by nextflow.config but not declared there: "
            + ", ".join(missing_default_owners)
        )

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("Parameter registry drift check passed.")
    print(f"main.nf params: {len(main_params)}")
    print(f"nextflow.config params: {len(declared_defaults)}")
    print(f"conf override params: {len(declared_overrides)}")
    print(f"registry params: {len(registry_params)}")
    print(f"derived runtime values: {len(derived_values)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
