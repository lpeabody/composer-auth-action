#!/usr/bin/env python3
"""Detect drift between compose_auth.SCHEMA and Composer's composer-schema.json.

Modes:
  --pinned   Compare against the Composer release recorded in SCHEMA_VERSION.
             Any difference is a bug in this repository (the schema was edited
             without re-syncing, or SCHEMA_VERSION was bumped without updating
             SCHEMA). Exit 1 on difference.
  --latest   Compare against Composer's latest GitHub release. A difference
             means upstream moved and SCHEMA_VERSION needs a re-sync; the
             output classifies each change so the right release bump can be
             chosen. Exit 1 on difference.

Only the standard library is used. Set GITHUB_TOKEN to raise the GitHub API
rate limit when resolving the latest release.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import compose_auth  # noqa: E402

RAW_URL = "https://raw.githubusercontent.com/composer/composer/{ref}/{path}"
LATEST_URL = "https://api.github.com/repos/composer/composer/releases/latest"

# Keys in Composer's `config` section that have an object-valued
# `additionalProperties` (the structural signature of a per-host auth method)
# but are not authentication methods. A new key with that shape that is in
# neither this list nor SCHEMA is reported so a human can classify it.
NON_AUTH_CONFIG_KEYS = {"platform", "allow-plugins", "preferred-install", "policy"}

JSON_TYPE_NAMES = {"string": "str", "integer": "int"}


def read_schema_version(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    for required in ("composer", "schema-file"):
        if required not in values:
            raise SystemExit(f"SCHEMA_VERSION is missing '{required}='")
    return values


def fetch_json(url: str) -> Any:
    headers = {"User-Agent": "composer-auth-action schema drift check"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def latest_composer_tag() -> str:
    return fetch_json(LATEST_URL)["tag_name"]


def normalise_local() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for method, spec in compose_auth.SCHEMA.items():
        entry: dict[str, Any] = {"kind": spec["kind"]}
        if spec["kind"] in ("object", "string-or-object"):
            entry["required"] = {k: t.__name__ for k, t in spec["required"].items()}
            entry["optional"] = {k: t.__name__ for k, t in spec["optional"].items()}
        out[method] = entry
    return out


def normalise_composer(method: str, schema: dict[str, Any]) -> dict[str, Any]:
    ap = schema.get("additionalProperties")
    if not isinstance(ap, dict):
        raise SystemExit(f"{method}: unexpected shape in Composer schema (no additionalProperties object)")
    types = ap.get("type", [])
    types = [types] if isinstance(types, str) else list(types)
    if types == ["string"]:
        return {"kind": "string"}
    if types == ["array"]:
        item_type = ap.get("items", {}).get("type")
        if item_type != "string":
            raise SystemExit(f"{method}: array items are {item_type!r}, not string; update the drift check")
        return {"kind": "string-list"}
    if "object" not in types:
        raise SystemExit(f"{method}: unsupported type list {types}; update the drift check")
    kind = "string-or-object" if "string" in types else "object"
    required = list(ap.get("required", []))
    properties: dict[str, Any] = ap.get("properties", {})

    def type_name(key: str) -> str:
        json_type = properties.get(key, {}).get("type")
        if json_type not in JSON_TYPE_NAMES:
            raise SystemExit(f"{method}.{key}: property type {json_type!r} is not handled; update the drift check")
        return JSON_TYPE_NAMES[json_type]

    return {
        "kind": kind,
        "required": {k: type_name(k) for k in required},
        "optional": {k: type_name(k) for k in properties if k not in required},
    }


def classify(method: str, local: dict[str, Any] | None, upstream: dict[str, Any] | None) -> list[str]:
    """Describe differences and the release bump each implies."""
    notes: list[str] = []
    if local is None:
        notes.append(f"{method}: new upstream method -> MINOR (add to SCHEMA)")
        return notes
    if upstream is None:
        notes.append(f"{method}: removed upstream -> MAJOR (drop from SCHEMA)")
        return notes
    if local["kind"] != upstream["kind"]:
        widening = (local["kind"], upstream["kind"]) == ("object", "string-or-object")
        bump = "MINOR" if widening else "MAJOR"
        notes.append(f"{method}: kind {local['kind']} -> {upstream['kind']} -> {bump}")
    for group in ("required", "optional"):
        l = local.get(group, {})
        u = upstream.get(group, {})
        for key in sorted(set(l) | set(u)):
            if key in l and key not in u:
                other = "optional" if group == "required" else "required"
                if key in upstream.get(other, {}):
                    continue  # reported from the other group
                notes.append(f"{method}.{key}: {group} key removed upstream -> MAJOR")
            elif key in u and key not in l:
                other = "optional" if group == "required" else "required"
                if key in local.get(other, {}):
                    bump = "MAJOR" if group == "required" else "MINOR"
                    notes.append(f"{method}.{key}: {other} -> {group} upstream -> {bump}")
                else:
                    bump = "MAJOR" if group == "required" else "MINOR"
                    notes.append(f"{method}.{key}: new {group} key upstream -> {bump}")
            elif l[key] != u[key]:
                notes.append(f"{method}.{key}: type {l[key]} -> {u[key]} -> MAJOR")
    return notes


def compare(ref: str, schema_file: str) -> int:
    document = fetch_json(RAW_URL.format(ref=ref, path=schema_file))
    config = document["properties"]["config"]["properties"]
    local = normalise_local()

    problems: list[str] = []
    for method in sorted(set(local) | set(m for m in config if m in local)):
        if method not in config:
            problems.extend(classify(method, local[method], None))
            continue
        upstream = normalise_composer(method, config[method])
        if upstream != local[method]:
            problems.extend(classify(method, local[method], upstream) or [f"{method}: differs"])

    for key, schema in config.items():
        if key in local or key in NON_AUTH_CONFIG_KEYS:
            continue
        if isinstance(schema.get("additionalProperties"), dict):
            problems.append(
                f"{key}: new config key shaped like an auth method; add it to SCHEMA (MINOR) "
                "or to NON_AUTH_CONFIG_KEYS in scripts/check_schema_drift.py"
            )

    if problems:
        print(f"SCHEMA differs from Composer {ref} ({schema_file}):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"SCHEMA matches Composer {ref} ({schema_file}): {len(local)} methods verified.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pinned", action="store_true", help="compare against the release in SCHEMA_VERSION")
    mode.add_argument("--latest", action="store_true", help="compare against Composer's latest release")
    args = parser.parse_args(argv)

    version = read_schema_version(os.path.join(ROOT, "SCHEMA_VERSION"))
    if args.pinned:
        ref = version["composer"]
    else:
        ref = latest_composer_tag()
        if ref == version["composer"]:
            print(f"Composer's latest release is still {ref}, the pinned version.")
        else:
            print(f"Composer's latest release is {ref}; SCHEMA_VERSION pins {version['composer']}.")
    rc = compare(ref, version["schema-file"])
    if rc == 0 and args.latest and ref != version["composer"]:
        print("No auth schema changes between the two; bump SCHEMA_VERSION to "
              f"{ref} in the next release to keep the reference current.")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
