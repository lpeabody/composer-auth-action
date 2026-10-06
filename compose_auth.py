#!/usr/bin/env python3
"""Compose a Composer ``auth.json`` document from a YAML fragment.

This is the implementation behind the ``composer-auth`` GitHub Action. All
configuration arrives through environment variables that ``action.yml`` sets
from the action inputs:

``INPUT_AUTH``          YAML mirroring the ``auth.json`` schema.
``INPUT_GITHUB_TOKEN``  Token for the default ``github-oauth`` entry ("" omits it).
``INPUT_EXPORT_ENV``    "true"/"false": write ``COMPOSER_AUTH`` to ``$GITHUB_ENV``.
``INPUT_WRITE_FILE``    "true"/"false": write an ``auth.json`` file.
``INPUT_FILE_PATH``     Where to write it; empty means Composer's global location.

Nothing in this module ever prints a credential value. Error messages name
the offending method, host and key only.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from typing import Any, Mapping
from urllib.parse import urlparse

try:
    import yaml
except ImportError:  # pragma: no cover - action.yml installs PyYAML first
    sys.stdout.write("::error::PyYAML is required: python3 -m pip install pyyaml\n")
    sys.exit(1)

ENV_VAR = "COMPOSER_AUTH"
DEFAULT_GITHUB_HOST = "github.com"

# Value shapes per authentication method, mirroring the ``config`` section of
# ``res/composer-schema.json`` in composer/composer.
#
# kind "string":           a non-empty string.
# kind "object":           a mapping; ``required`` keys must be present,
#                          ``optional`` keys may be, anything else is an error.
# kind "string-or-object": either of the above.
# kind "string-list":      a non-empty list of non-empty strings.
SCHEMA: dict[str, dict[str, Any]] = {
    "http-basic": {
        "kind": "object",
        "required": {"username": str, "password": str},
        "optional": {},
    },
    "bearer": {"kind": "string"},
    "github-oauth": {"kind": "string"},
    "gitlab-oauth": {
        "kind": "string-or-object",
        "required": {"token": str},
        "optional": {"refresh-token": str, "expires-at": int},
    },
    "gitlab-token": {
        "kind": "string-or-object",
        "required": {"username": str, "token": str},
        "optional": {},
    },
    "bitbucket-oauth": {
        "kind": "object",
        "required": {"consumer-key": str, "consumer-secret": str},
        "optional": {},
    },
    "custom-headers": {"kind": "string-list"},
    "client-certificate": {
        "kind": "object",
        "required": {"local_cert": str},
        "optional": {"local_pk": str, "passphrase": str},
    },
    "forgejo-token": {
        "kind": "object",
        "required": {"username": str, "token": str},
        "optional": {},
    },
}


class AuthError(Exception):
    """A validation or configuration problem. The message is safe to log."""


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "mapping"
    return type(value).__name__


def _check_scalar(value: Any, where: str, typ: type) -> Any:
    if value is None:
        raise AuthError(f"{where}: value is empty (is the secret it comes from set?)")
    if typ is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise AuthError(f"{where}: expected an integer, got {_type_name(value)}")
        return value
    if isinstance(value, bool) or not isinstance(value, str):
        hint = "; quote the value in the YAML" if isinstance(value, (bool, int, float)) else ""
        raise AuthError(f"{where}: expected a string, got {_type_name(value)}{hint}")
    if not value.strip():
        raise AuthError(f"{where}: value is empty (is the secret it comes from set?)")
    return value


def _check_object(value: Any, where: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    required: dict[str, type] = spec["required"]
    optional: dict[str, type] = spec["optional"]
    allowed = list(required) + list(optional)
    if not isinstance(value, dict):
        raise AuthError(
            f"{where}: expected a mapping with keys {', '.join(required)}, got {_type_name(value)}"
        )
    unknown = [k for k in value if k not in allowed]
    if unknown:
        raise AuthError(
            f"{where}: unknown key(s) {', '.join(map(str, unknown))}; allowed: {', '.join(allowed)}"
        )
    missing = [k for k in required if k not in value]
    if missing:
        raise AuthError(f"{where}: missing required key(s) {', '.join(missing)}")
    out: dict[str, Any] = {}
    for key in allowed:
        if key in value:
            out[key] = _check_scalar(value[key], f"{where}.{key}", {**required, **optional}[key])
    return out


def _check_string_list(value: Any, where: str) -> list[str]:
    if value is None or value == []:
        raise AuthError(f"{where}: value is empty")
    if not isinstance(value, list):
        raise AuthError(f"{where}: expected a list of header strings, got {_type_name(value)}")
    return [_check_scalar(item, f"{where}[{i}]", str) for i, item in enumerate(value)]


def _check_value(value: Any, where: str, spec: Mapping[str, Any]) -> Any:
    kind = spec["kind"]
    if kind == "string":
        return _check_scalar(value, where, str)
    if kind == "object":
        return _check_object(value, where, spec)
    if kind == "string-or-object":
        if isinstance(value, dict):
            return _check_object(value, where, spec)
        return _check_scalar(value, where, str)
    if kind == "string-list":
        return _check_string_list(value, where)
    raise AssertionError(f"unknown schema kind {kind!r}")  # pragma: no cover


def validate(auth: Any) -> dict[str, dict[str, Any]]:
    """Validate parsed YAML against the Composer auth schema.

    Returns a normalised ``{method: {host: value}}`` mapping. Raises
    :class:`AuthError` naming the first offending method/host/key.
    """
    if auth is None:
        return {}
    if not isinstance(auth, dict):
        raise AuthError(
            f"auth: expected a mapping of authentication methods, got {_type_name(auth)}"
        )
    result: dict[str, dict[str, Any]] = {}
    for method, hosts in auth.items():
        if method not in SCHEMA:
            raise AuthError(
                f"{method}: unknown authentication method; supported: {', '.join(SCHEMA)}"
            )
        if hosts is None or hosts == {}:
            raise AuthError(f"{method}: no hosts defined")
        if not isinstance(hosts, dict):
            raise AuthError(f"{method}: expected a mapping of host => credential, got {_type_name(hosts)}")
        result[method] = {}
        for host, value in hosts.items():
            if not isinstance(host, str) or not host.strip():
                raise AuthError(f"{method}: host names must be non-empty strings, got {_type_name(host)}")
            result[method][host] = _check_value(value, f"{method}.{host}", SCHEMA[method])
    return result


def add_default_github_token(
    auth: dict[str, dict[str, Any]], token: str, server_url: str | None
) -> dict[str, dict[str, Any]]:
    """Add ``github-oauth.<server host>`` unless ``auth`` already defines it."""
    if not token or not token.strip():
        return auth
    host = urlparse(server_url or "").hostname or DEFAULT_GITHUB_HOST
    existing = auth.get("github-oauth", {})
    if host in existing:
        return auth
    auth["github-oauth"] = {host: token, **existing}
    return auth


# --------------------------------------------------------------------------- #
# File handling
# --------------------------------------------------------------------------- #

def composer_home(env: Mapping[str, str], windows: bool | None = None) -> str:
    """Resolve Composer's home directory the way ``Composer\\Factory`` does."""
    home = env.get("COMPOSER_HOME")
    if home:
        return home
    if windows is None:
        windows = os.name == "nt"
    if windows:
        appdata = env.get("APPDATA")
        if not appdata:
            raise AuthError("file-path: APPDATA or COMPOSER_HOME must be set to locate Composer's home")
        return appdata.replace("\\", "/").rstrip("/") + "/Composer"
    user_dir = env.get("HOME")
    if not user_dir:
        raise AuthError("file-path: HOME or COMPOSER_HOME must be set to locate Composer's home")
    user_dir = user_dir.replace("\\", "/").rstrip("/")
    candidates = []
    use_xdg = any(key.startswith("XDG_") for key in env) or os.path.isdir("/etc/xdg")
    if use_xdg:
        xdg_config = env.get("XDG_CONFIG_HOME") or f"{user_dir}/.config"
        candidates.append(f"{xdg_config}/composer")
    candidates.append(f"{user_dir}/.composer")
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return candidates[0]


def resolve_file_path(raw: str, env: Mapping[str, str]) -> str:
    path = raw.strip() if raw else ""
    if not path:
        path = os.path.join(composer_home(env), "auth.json")
    return os.path.abspath(os.path.expanduser(path))


def is_inside(path: str, directory: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(directory)]) == os.path.realpath(directory)
    except ValueError:  # different drives on Windows
        return False


def load_existing(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuthError(f"file-path: existing {path} is not valid JSON (line {exc.lineno}, column {exc.colno})") from None
    if not isinstance(data, dict):
        raise AuthError(f"file-path: existing {path} is not a JSON object")
    return data


def merge(existing: Mapping[str, Any], composed: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Merge per method per host; entries from ``composed`` win."""
    merged: dict[str, Any] = {
        k: dict(v) if isinstance(v, dict) else v for k, v in existing.items()
    }
    for method, hosts in composed.items():
        base = merged.get(method)
        if isinstance(base, dict):
            base.update(hosts)
        else:
            merged[method] = dict(hosts)
    return merged


def write_file(path: str, data: Mapping[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".auth.json.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=4, ensure_ascii=False))
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# GitHub Actions plumbing
# --------------------------------------------------------------------------- #

def _escape_command_value(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _command(out, name: str, value: str) -> None:
    out.write(f"::{name}::{_escape_command_value(value)}\n")


def append_github_file(path: str, name: str, value: str) -> None:
    """Append ``name=value`` to a GITHUB_ENV / GITHUB_OUTPUT style file."""
    delimiter = f"ghadelimiter_{uuid.uuid4()}"
    if delimiter in value:  # pragma: no cover - astronomically unlikely
        raise AuthError(f"{name}: value contains the heredoc delimiter")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def parse_bool(name: str, raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    lowered = raw.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise AuthError(f"{name}: expected 'true' or 'false'")


def parse_yaml(raw: str) -> Any:
    try:
        return yaml.safe_load(raw)
    except yaml.MarkedYAMLError as exc:
        # Deliberately avoid exc.__str__(): it includes a snippet of the
        # offending line, which may contain a credential.
        mark = exc.problem_mark
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = exc.problem or exc.context or "parse error"
        raise AuthError(f"auth: invalid YAML{location}: {problem}") from None
    except yaml.YAMLError:
        raise AuthError("auth: invalid YAML") from None


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def run(env: Mapping[str, str], out) -> None:
    export_env = parse_bool("export-env", env.get("INPUT_EXPORT_ENV"), True)
    write_to_file = parse_bool("write-file", env.get("INPUT_WRITE_FILE"), False)
    if not export_env and not write_to_file:
        raise AuthError("export-env and write-file are both false; enable at least one output")

    auth = validate(parse_yaml(env.get("INPUT_AUTH") or ""))
    auth = add_default_github_token(auth, env.get("INPUT_GITHUB_TOKEN", ""), env.get("GITHUB_SERVER_URL"))
    if not auth:
        raise AuthError("nothing to compose: auth is empty and github-token is empty")

    compact = json.dumps(auth, separators=(",", ":"), ensure_ascii=True)
    # Mask the assembled document so no later step can echo it into the log.
    _command(out, "add-mask", compact)

    entry_count = sum(len(hosts) for hosts in auth.values())
    out.write(f"Composed Composer auth with {entry_count} host entr{'y' if entry_count == 1 else 'ies'}:\n")
    for method, hosts in auth.items():
        out.write(f"  {method}: {', '.join(hosts)}\n")

    if export_env:
        github_env = env.get("GITHUB_ENV")
        if not github_env:
            raise AuthError("export-env: GITHUB_ENV is not set; is this running inside GitHub Actions?")
        append_github_file(github_env, ENV_VAR, compact)
        out.write(f"Exported {ENV_VAR} for subsequent steps in this job.\n")

    written_path = ""
    if write_to_file:
        written_path = resolve_file_path(env.get("INPUT_FILE_PATH", ""), env)
        workspace = env.get("GITHUB_WORKSPACE")
        if workspace and is_inside(written_path, workspace):
            _command(
                out,
                "warning",
                f"auth.json is being written inside the workspace ({written_path}). "
                "Make sure no later step commits or uploads it.",
            )
        existing = load_existing(written_path)
        preserved = sum(
            1
            for method, hosts in existing.items()
            if isinstance(hosts, dict)
            for host in hosts
            if host not in auth.get(method, {})
        )
        write_file(written_path, merge(existing, auth))
        suffix = f" (preserved {preserved} existing entr{'y' if preserved == 1 else 'ies'})" if existing else ""
        out.write(f"Wrote {written_path}{suffix}.\n")

    github_output = env.get("GITHUB_OUTPUT")
    if github_output:
        append_github_file(github_output, "file-path", written_path)


def main(env: Mapping[str, str] | None = None, out=None) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    try:
        run(env, out)
    except AuthError as exc:
        _command(out, "error", str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
