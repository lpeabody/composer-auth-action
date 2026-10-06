"""Unit tests for compose_auth.py. Run with: python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import compose_auth as ca  # noqa: E402

ALL_METHODS_YAML = """
http-basic:
  connect.advancedcustomfields.com:
    username: ACF-KEY-123
    password: https://example.com
bearer:
  bearer.example.com: BEARER-SECRET
github-oauth:
  ghe.example.com: GHE-SECRET
gitlab-oauth:
  gitlab.com: GL-OAUTH-SECRET
  gitlab.example.com:
    token: GL-OAUTH-OBJ-SECRET
    refresh-token: GL-REFRESH
    expires-at: 1700000000
gitlab-token:
  gitlab.com: GL-TOKEN-SECRET
  gitlab.example.com:
    username: gl-user
    token: GL-TOKEN-OBJ-SECRET
bitbucket-oauth:
  bitbucket.org:
    consumer-key: BB-KEY
    consumer-secret: BB-SECRET
custom-headers:
  headers.example.com:
    - "X-Api-Key: HEADER-SECRET"
    - "X-Other: value"
client-certificate:
  cert.example.com:
    local_cert: /certs/client.pem
    local_pk: /certs/client.key
    passphrase: CERT-PASS
forgejo-token:
  codeberg.org:
    username: fj-user
    token: FJ-SECRET
"""

SECRETS = [
    "ACF-KEY-123", "BEARER-SECRET", "GHE-SECRET", "GL-OAUTH-SECRET",
    "GL-OAUTH-OBJ-SECRET", "GL-REFRESH", "GL-TOKEN-SECRET", "GL-TOKEN-OBJ-SECRET",
    "BB-KEY", "BB-SECRET", "HEADER-SECRET", "CERT-PASS", "FJ-SECRET", "RUN-TOKEN",
]


def load(text: str):
    return ca.validate(ca.parse_yaml(text))


class ValidateTests(unittest.TestCase):
    def test_all_nine_methods_round_trip(self):
        result = load(ALL_METHODS_YAML)
        self.assertEqual(set(result), set(ca.SCHEMA))
        self.assertEqual(result["http-basic"]["connect.advancedcustomfields.com"],
                         {"username": "ACF-KEY-123", "password": "https://example.com"})
        self.assertEqual(result["bearer"]["bearer.example.com"], "BEARER-SECRET")
        self.assertEqual(result["gitlab-oauth"]["gitlab.example.com"],
                         {"token": "GL-OAUTH-OBJ-SECRET", "refresh-token": "GL-REFRESH", "expires-at": 1700000000})
        self.assertEqual(result["gitlab-token"]["gitlab.example.com"], {"username": "gl-user", "token": "GL-TOKEN-OBJ-SECRET"})
        self.assertEqual(result["custom-headers"]["headers.example.com"], ["X-Api-Key: HEADER-SECRET", "X-Other: value"])
        self.assertEqual(result["client-certificate"]["cert.example.com"],
                         {"local_cert": "/certs/client.pem", "local_pk": "/certs/client.key", "passphrase": "CERT-PASS"})

    def test_empty_input_is_empty_mapping(self):
        self.assertEqual(load(""), {})
        self.assertEqual(load("# just a comment\n"), {})

    def assert_error(self, text: str, *fragments: str):
        with self.assertRaises(ca.AuthError) as ctx:
            load(text)
        message = str(ctx.exception)
        for fragment in fragments:
            self.assertIn(fragment, message)
        return message

    def test_unknown_method(self):
        self.assert_error("http-digest:\n  example.com: x\n", "http-digest", "unknown authentication method")

    def test_top_level_not_mapping(self):
        self.assert_error("- http-basic\n", "auth:", "expected a mapping")

    def test_method_without_hosts(self):
        self.assert_error("http-basic:\n", "http-basic", "no hosts")
        self.assert_error("http-basic: {}\n", "http-basic", "no hosts")

    def test_wrong_shape_string_for_object(self):
        self.assert_error("http-basic:\n  example.com: just-a-string\n", "http-basic.example.com", "expected a mapping")

    def test_wrong_shape_object_for_string(self):
        self.assert_error("bearer:\n  example.com:\n    token: x\n", "bearer.example.com", "expected a string")

    def test_missing_required_key(self):
        self.assert_error("http-basic:\n  example.com:\n    username: u\n", "http-basic.example.com", "missing required", "password")

    def test_unknown_object_key(self):
        self.assert_error("http-basic:\n  example.com:\n    username: u\n    password: p\n    extra: x\n",
                          "http-basic.example.com", "unknown key", "extra")

    def test_empty_credential_from_unset_secret(self):
        # `password: ${{ secrets.MISSING }}` renders as `password: ` which YAML reads as null.
        self.assert_error("http-basic:\n  example.com:\n    username: u\n    password: \n",
                          "http-basic.example.com.password", "empty", "secret")
        self.assert_error("bearer:\n  example.com: ''\n", "bearer.example.com", "empty")
        self.assert_error("bearer:\n  example.com: '   '\n", "bearer.example.com", "empty")

    def test_numeric_or_boolean_scalar_needs_quoting(self):
        self.assert_error("bearer:\n  example.com: 12345\n", "bearer.example.com", "integer", "quote")
        self.assert_error("bearer:\n  example.com: yes\n", "bearer.example.com", "boolean", "quote")

    def test_custom_headers_shape(self):
        self.assert_error("custom-headers:\n  example.com: 'X: y'\n", "custom-headers.example.com", "list")
        self.assert_error("custom-headers:\n  example.com: []\n", "custom-headers.example.com", "empty")
        self.assert_error("custom-headers:\n  example.com:\n    - 'X: y'\n    - \n", "custom-headers.example.com[1]", "empty")

    def test_bitbucket_oauth_accepts_composer_written_keys(self):
        result = load("bitbucket-oauth:\n  bitbucket.org:\n    consumer-key: k\n    consumer-secret: s\n"
                      "    access-token: t\n    access-token-expiration: 1700000000\n")
        self.assertEqual(result["bitbucket-oauth"]["bitbucket.org"],
                         {"consumer-key": "k", "consumer-secret": "s", "access-token": "t", "access-token-expiration": 1700000000})
        self.assert_error("bitbucket-oauth:\n  bitbucket.org:\n    consumer-key: k\n    consumer-secret: s\n    access-token-expiration: soon\n",
                          "bitbucket-oauth.bitbucket.org.access-token-expiration", "integer")

    def test_gitlab_oauth_expires_at_must_be_int(self):
        self.assert_error("gitlab-oauth:\n  gitlab.com:\n    token: t\n    expires-at: soon\n",
                          "gitlab-oauth.gitlab.com.expires-at", "integer")

    def test_invalid_yaml_does_not_echo_content(self):
        message = self.assert_error("http-basic:\n  example.com: {username: SUPER-SECRET, password: [\n", "invalid YAML", "line")
        self.assertNotIn("SUPER-SECRET", message)

    def test_error_messages_never_contain_values(self):
        for text in [
            "bearer:\n  example.com: SHOULD-NOT-LEAK-1\n  other.example.com: ''\n",
            "http-basic:\n  example.com:\n    username: SHOULD-NOT-LEAK-2\n    password: 12345\n",
            "http-basic:\n  example.com:\n    username: SHOULD-NOT-LEAK-3\n",
        ]:
            with self.assertRaises(ca.AuthError) as ctx:
                load(text)
            self.assertNotIn("SHOULD-NOT-LEAK", str(ctx.exception))


class DefaultGithubTokenTests(unittest.TestCase):
    def test_added_when_absent(self):
        result = ca.add_default_github_token({}, "RUN-TOKEN", "https://github.com")
        self.assertEqual(result, {"github-oauth": {"github.com": "RUN-TOKEN"}})

    def test_uses_server_host(self):
        result = ca.add_default_github_token({}, "RUN-TOKEN", "https://ghe.corp.example")
        self.assertEqual(result, {"github-oauth": {"ghe.corp.example": "RUN-TOKEN"}})

    def test_falls_back_to_github_com_without_server_url(self):
        result = ca.add_default_github_token({}, "RUN-TOKEN", None)
        self.assertEqual(result, {"github-oauth": {"github.com": "RUN-TOKEN"}})

    def test_empty_token_omits_entry(self):
        self.assertEqual(ca.add_default_github_token({}, "", "https://github.com"), {})
        self.assertEqual(ca.add_default_github_token({}, "  ", "https://github.com"), {})

    def test_explicit_entry_wins(self):
        auth = {"github-oauth": {"github.com": "EXPLICIT"}}
        result = ca.add_default_github_token(auth, "RUN-TOKEN", "https://github.com")
        self.assertEqual(result["github-oauth"], {"github.com": "EXPLICIT"})

    def test_other_github_hosts_preserved(self):
        auth = {"github-oauth": {"ghe.example.com": "GHE"}}
        result = ca.add_default_github_token(auth, "RUN-TOKEN", "https://github.com")
        self.assertEqual(result["github-oauth"], {"github.com": "RUN-TOKEN", "ghe.example.com": "GHE"})


class MergeTests(unittest.TestCase):
    def test_run_entries_win_and_others_are_preserved(self):
        existing = {
            "http-basic": {
                "keep.example.com": {"username": "old", "password": "old"},
                "shared.example.com": {"username": "old", "password": "old"},
            },
            "bearer": {"keep.example.com": "old-bearer"},
        }
        composed = {
            "http-basic": {"shared.example.com": {"username": "new", "password": "new"}},
            "github-oauth": {"github.com": "tok"},
        }
        merged = ca.merge(existing, composed)
        self.assertEqual(merged["http-basic"]["keep.example.com"], {"username": "old", "password": "old"})
        self.assertEqual(merged["http-basic"]["shared.example.com"], {"username": "new", "password": "new"})
        self.assertEqual(merged["bearer"], {"keep.example.com": "old-bearer"})
        self.assertEqual(merged["github-oauth"], {"github.com": "tok"})
        # merge must not mutate its inputs
        self.assertEqual(existing["http-basic"]["shared.example.com"]["username"], "old")

    def test_non_mapping_existing_method_is_replaced(self):
        merged = ca.merge({"bearer": "garbage"}, {"bearer": {"a.example.com": "x"}})
        self.assertEqual(merged["bearer"], {"a.example.com": "x"})


class ComposerHomeTests(unittest.TestCase):
    def test_composer_home_env_wins(self):
        self.assertEqual(ca.composer_home({"COMPOSER_HOME": "/opt/composer", "HOME": "/home/u"}), "/opt/composer")

    def test_windows_uses_appdata(self):
        self.assertEqual(ca.composer_home({"APPDATA": "C:\\Users\\u\\AppData\\Roaming\\"}, windows=True),
                         "C:/Users/u/AppData/Roaming/Composer")
        with self.assertRaises(ca.AuthError):
            ca.composer_home({}, windows=True)

    def test_requires_home(self):
        with self.assertRaises(ca.AuthError):
            ca.composer_home({}, windows=False)

    def test_prefers_existing_dir(self):
        with tempfile.TemporaryDirectory() as home:
            os.mkdir(os.path.join(home, ".composer"))
            env = {"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, "cfg")}
            self.assertEqual(ca.composer_home(env, windows=False), os.path.join(home, ".composer"))
            os.mkdir(os.path.join(home, "cfg"))
            os.mkdir(os.path.join(home, "cfg", "composer"))
            self.assertEqual(ca.composer_home(env, windows=False), os.path.join(home, "cfg", "composer"))

    def test_defaults_to_xdg_when_xdg_in_use_and_nothing_exists(self):
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "XDG_RUNTIME_DIR": "/run/user/1000"}
            self.assertEqual(ca.composer_home(env, windows=False), f"{home}/.config/composer")

    def test_defaults_to_dot_composer_without_xdg(self):
        with tempfile.TemporaryDirectory() as home, mock.patch("os.path.isdir", return_value=False):
            self.assertEqual(ca.composer_home({"HOME": home}, windows=False), f"{home}/.composer")


class EndToEndTests(unittest.TestCase):
    """Run the script as the action would, with GITHUB_ENV/GITHUB_OUTPUT redirected to temp files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.github_env = os.path.join(self.dir, "github_env")
        self.github_output = os.path.join(self.dir, "github_output")
        self.workspace = os.path.join(self.dir, "workspace")
        os.mkdir(self.workspace)

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, **inputs):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": self.dir,
            "GITHUB_ENV": self.github_env,
            "GITHUB_OUTPUT": self.github_output,
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_WORKSPACE": self.workspace,
            "COMPOSER_HOME": os.path.join(self.dir, "composer-home"),
            "INPUT_GITHUB_TOKEN": "RUN-TOKEN",
        }
        # Keep a private PyYAML install reachable (see the Test workflow).
        if os.environ.get("PYTHONPATH"):
            env["PYTHONPATH"] = os.environ["PYTHONPATH"]
        for key, value in inputs.items():
            env["INPUT_" + key.upper().replace("-", "_")] = value
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "compose_auth.py")],
            env=env, capture_output=True, text=True, check=False,
        )

    def read_env_var(self, name="COMPOSER_AUTH"):
        with open(self.github_env, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        header = next(i for i, line in enumerate(lines) if line.startswith(f"{name}<<"))
        delimiter = lines[header].split("<<", 1)[1]
        body = []
        for line in lines[header + 1:]:
            if line == delimiter:
                break
            body.append(line)
        return "\n".join(body)

    def read_output(self, name):
        with open(self.github_output, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        header = next(i for i, line in enumerate(lines) if line.startswith(f"{name}<<"))
        return lines[header + 1]

    def assert_no_leak(self, proc):
        visible = "\n".join(
            line for line in (proc.stdout + proc.stderr).splitlines()
            if not line.startswith("::add-mask::")
        )
        for secret in SECRETS:
            self.assertNotIn(secret, visible, f"{secret!r} leaked into step output")

    def test_export_env_default(self):
        proc = self.run_script(auth=ALL_METHODS_YAML)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assert_no_leak(proc)
        composed = json.loads(self.read_env_var())
        self.assertEqual(composed["github-oauth"]["github.com"], "RUN-TOKEN")
        self.assertEqual(composed["github-oauth"]["ghe.example.com"], "GHE-SECRET")
        self.assertEqual(composed["http-basic"]["connect.advancedcustomfields.com"]["username"], "ACF-KEY-123")
        self.assertIn("::add-mask::" + json.dumps(composed, separators=(",", ":")), proc.stdout)
        self.assertEqual(self.read_output("file-path"), "")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "composer-home", "auth.json")))

    def test_github_token_empty_omits_entry(self):
        proc = self.run_script(auth="bearer:\n  b.example.com: BEARER-SECRET\n", github_token="")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(json.loads(self.read_env_var()), {"bearer": {"b.example.com": "BEARER-SECRET"}})

    def test_explicit_github_oauth_overrides_default(self):
        proc = self.run_script(auth="github-oauth:\n  github.com: EXPLICIT-GH\n")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(json.loads(self.read_env_var()), {"github-oauth": {"github.com": "EXPLICIT-GH"}})

    def test_write_file_default_location_and_merge(self):
        home = os.path.join(self.dir, "composer-home")
        os.mkdir(home)
        with open(os.path.join(home, "auth.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "http-basic": {
                    "keep.example.com": {"username": "k", "password": "k"},
                    "connect.advancedcustomfields.com": {"username": "old", "password": "old"},
                },
            }, fh)
        proc = self.run_script(auth=ALL_METHODS_YAML, export_env="false", write_file="true")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assert_no_leak(proc)
        path = os.path.join(home, "auth.json")
        self.assertEqual(self.read_output("file-path"), path)
        with open(path, encoding="utf-8") as fh:
            written = json.load(fh)
        self.assertEqual(written["http-basic"]["keep.example.com"], {"username": "k", "password": "k"})
        self.assertEqual(written["http-basic"]["connect.advancedcustomfields.com"]["username"], "ACF-KEY-123")
        self.assertEqual(written["github-oauth"]["github.com"], "RUN-TOKEN")
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        self.assertFalse(os.path.exists(self.github_env), "export-env=false must not touch GITHUB_ENV")
        self.assertIn("preserved 1 existing entry", proc.stdout)

    def test_write_file_custom_path_and_both_outputs(self):
        target = os.path.join(self.dir, "elsewhere", "nested", "auth.json")
        proc = self.run_script(auth="bearer:\n  b.example.com: BEARER-SECRET\n", write_file="true", file_path=target)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.read_output("file-path"), target)
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), json.loads(self.read_env_var()))
        self.assertNotIn("::warning::", proc.stdout)

    def test_write_file_inside_workspace_warns(self):
        target = os.path.join(self.workspace, "auth.json")
        proc = self.run_script(auth="bearer:\n  b.example.com: BEARER-SECRET\n", write_file="true", file_path=target)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("::warning::", proc.stdout)
        self.assertIn("inside the workspace", proc.stdout)

    def test_existing_file_invalid_json_fails(self):
        target = os.path.join(self.dir, "auth.json")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        proc = self.run_script(auth="bearer:\n  b.example.com: x\n", export_env="false", write_file="true", file_path=target)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("::error::file-path: existing", proc.stdout)
        self.assertIn("not valid JSON", proc.stdout)

    def test_neither_output_is_error(self):
        proc = self.run_script(auth="bearer:\n  b.example.com: x\n", export_env="false", write_file="false")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("::error::export-env and write-file are both false", proc.stdout)

    def test_bad_boolean_is_error(self):
        proc = self.run_script(auth="bearer:\n  b.example.com: x\n", export_env="yes")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("::error::export-env: expected 'true' or 'false'", proc.stdout)

    def test_nothing_to_compose_is_error(self):
        proc = self.run_script(auth="", github_token="")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("::error::nothing to compose", proc.stdout)

    def test_validation_failure_writes_nothing(self):
        target = os.path.join(self.dir, "auth.json")
        proc = self.run_script(auth="http-basic:\n  example.com: SHOULD-NOT-LEAK\n", write_file="true", file_path=target)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("::error::http-basic.example.com: expected a mapping", proc.stdout)
        self.assertNotIn("SHOULD-NOT-LEAK", proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(self.github_env))

    def test_invalid_yaml_writes_nothing(self):
        target = os.path.join(self.dir, "auth.json")
        proc = self.run_script(auth="http-basic:\n  example.com: {username: SHOULD-NOT-LEAK, password: [\n",
                               write_file="true", file_path=target)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("::error::auth: invalid YAML", proc.stdout)
        self.assertNotIn("SHOULD-NOT-LEAK", proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(self.github_env))

    def test_percent_in_credential_is_escaped_in_mask_command(self):
        proc = self.run_script(auth="bearer:\n  b.example.com: 'abc%def'\n", github_token="")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("::add-mask::{\"bearer\":{\"b.example.com\":\"abc%25def\"}}", proc.stdout)
        self.assertEqual(json.loads(self.read_env_var())["bearer"]["b.example.com"], "abc%def")


if __name__ == "__main__":
    unittest.main()
