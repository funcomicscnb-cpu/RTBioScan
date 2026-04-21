#!/usr/bin/env python3
"""Render the run-config README.html from a template, .conf file, and parameter registry."""

import argparse
import html
import json
import sys
from pathlib import Path


def load_registry() -> dict:
    registry_path = Path(__file__).resolve().parents[1] / "docs" / "params_reference.json"
    try:
        return json.loads(registry_path.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"ERROR: cannot read parameter registry {registry_path}: {exc}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid parameter registry {registry_path}: {exc}", file=sys.stderr)
        sys.exit(1)


def parse_user_params(cmd_line: str) -> dict[str, str]:
    """Extract --param value pairs from a Nextflow command line string."""
    params: dict[str, str] = {}
    tokens = cmd_line.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--") and len(tok) > 2:
            key = tok[2:]
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                params[key] = tokens[i + 1]
                i += 2
            else:
                params[key] = "true"
                i += 1
        else:
            i += 1
    return params


def extract_cmd_from_conf(conf_path: str) -> str:
    """Extract the command line recorded in the .conf file comment."""
    try:
        text = Path(conf_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("// Command:"):
            return line[len("// Command:"):].strip()
    return ""


def read_conf_params(conf_path: str) -> list[tuple[str, str]]:
    """Read key=value lines from inside the params { } block of the .conf file."""
    try:
        text = Path(conf_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    pairs: list[tuple[str, str]] = []
    in_params = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "params {":
            in_params = True
            continue
        if in_params and stripped == "}":
            in_params = False
            continue
        if in_params and "=" in stripped and not stripped.startswith("//"):
            key, _, val = stripped.partition("=")
            pairs.append((key.strip(), val.strip()))
    return pairs


def lifecycle_badge(param: str, phase_inventory: dict) -> str:
    if param in phase_inventory.get("remove_later", []):
        return ' <span class="user-badge">legacy</span>'
    if param in phase_inventory.get("rename_later", []):
        return ' <span class="user-badge">rename later</span>'
    return ""


def describe_param(param: str, metadata: dict, phase_inventory: dict) -> str:
    desc = metadata.get("description", "").strip()
    extras = []
    allowed = metadata.get("allowed_values") or []
    if allowed:
        extras.append("Allowed values: " + ", ".join(str(v) for v in allowed))
    list_policy = metadata.get("list_policy")
    if list_policy and list_policy != "scalar":
        extras.append(f"List policy: {list_policy}")
    if metadata.get("allow_empty_entries"):
        extras.append("Empty aligned entries allowed")
    if param in phase_inventory.get("remove_later", []):
        extras.append("Planned cleanup: remove later")
    elif param in phase_inventory.get("rename_later", []):
        extras.append("Planned cleanup: rename later")
    if extras:
        if desc:
            desc += " "
        desc += " ".join(extras) + "."
    return desc


def build_user_rows(user_params: dict[str, str], registry_params: dict, phase_inventory: dict) -> str:
    if not user_params:
        return '<tr><td colspan="3" style="color:#8a9b85;font-style:italic">No explicit CLI parameters detected.</td></tr>'
    rows = []
    for key in sorted(user_params):
        val = user_params[key]
        metadata = registry_params.get(key, {})
        desc = describe_param(key, metadata, phase_inventory)
        desc_html = html.escape(desc) if desc else '<span style="color:#b0b8aa">—</span>'
        rows.append(
            f'<tr><td><code>{html.escape(key)}</code>{lifecycle_badge(key, phase_inventory)}</td>'
            f'<td>{html.escape(val)}</td>'
            f'<td>{desc_html}</td></tr>'
        )
    return "\n".join(rows)


def build_ref_sections(registry: dict, user_params: dict[str, str]) -> str:
    registry_params = registry.get("params", {})
    categories = registry.get("category_order", [])
    phase_inventory = registry.get("phase_inventory", {})
    by_cat: dict[str, list[tuple[str, dict]]] = {category: [] for category in categories}
    for param, metadata in registry_params.items():
        category = metadata.get("category")
        if category in by_cat:
            by_cat[category].append((param, metadata))
    parts = []
    for category in categories:
        items = by_cat.get(category, [])
        if not items:
            continue
        rows = []
        for param, metadata in sorted(items, key=lambda item: item[0]):
            highlight = ' class="user-set-row"' if param in user_params else ""
            user_badge = ' <span class="user-badge">user-set</span>' if param in user_params else ""
            cleanup_badge = lifecycle_badge(param, phase_inventory)
            desc = describe_param(param, metadata, phase_inventory)
            rows.append(
                f'<tr{highlight}><td><code>{html.escape(param)}</code>{user_badge}{cleanup_badge}</td>'
                f'<td>{html.escape(desc)}</td></tr>'
            )
        parts.append(
            f'<details><summary>{html.escape(category)}</summary>'
            f'<table class="ref-table">'
            f'<thead><tr><th>Parameter</th><th>Description</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></details>'
        )
    return "\n".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True)
    ap.add_argument("--conf", required=True, help="Path to the generated .conf file")
    ap.add_argument("--out", required=True)
    ap.add_argument("--run-name", required=True)
    ap.add_argument("--state-id", required=True)
    ap.add_argument("--cmd-line", default="", help="workflow.commandLine string")
    args = ap.parse_args()

    registry = load_registry()
    registry_params = registry.get("params", {})
    phase_inventory = registry.get("phase_inventory", {})

    run_name = args.run_name
    state_id = args.state_id
    conf_path = args.conf
    conf_filename = Path(conf_path).name
    conf_rel = f"../{conf_filename}"

    cmd_line = args.cmd_line.strip()
    if not cmd_line:
        cmd_line = extract_cmd_from_conf(conf_path)
    user_params = parse_user_params(cmd_line)

    try:
        template = Path(args.template).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: cannot read template {args.template}: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        conf_content = Path(conf_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        conf_content = "# .conf file not found"

    replacements = {
        "{{RUN_NAME}}": html.escape(run_name),
        "{{STATE_ID}}": html.escape(state_id),
        "{{CONF_FILENAME}}": html.escape(conf_filename),
        "{{CONF_RELATIVE_PATH}}": html.escape(conf_rel),
        "{{CONF_CONTENT}}": html.escape(conf_content),
        "{{RUN_COMMAND}}": html.escape(cmd_line) if cmd_line else "(command not recorded)",
        "{{USER_PARAMS_ROWS}}": build_user_rows(user_params, registry_params, phase_inventory),
        "{{USER_COUNT}}": str(len(user_params)),
        "{{ALL_COUNT}}": str(len(read_conf_params(conf_path))),
        "{{REF_SECTIONS}}": build_ref_sections(registry, user_params),
    }

    out = template
    for placeholder, value in replacements.items():
        out = out.replace(placeholder, value)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out, encoding="utf-8")
    print(f"Wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
