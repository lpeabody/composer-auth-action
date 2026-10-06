# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A composite GitHub Action (`lpeabody/composer-auth-action`) that turns a YAML
input mirroring Composer's `auth.json` schema into `COMPOSER_AUTH` and/or an
`auth.json` file, validated before Composer runs. Three files matter:

- `action.yml` maps inputs to `INPUT_*` env vars and runs the Python script.
  It installs PyYAML into a private dir under `RUNNER_TEMP` with
  `pip --target` only when `import yaml` fails (keeps the runner's Python
  untouched and sidesteps PEP 668 on macOS runners).
- `compose_auth.py` is the whole implementation. It is stdlib-only apart from
  PyYAML. Everything is driven by env vars, so the test suite can run it
  as a subprocess with `GITHUB_ENV`/`GITHUB_OUTPUT` pointed at temp files.
- `tests/test_compose_auth.py` holds all tests (plain `unittest`, no pytest).

## Commands

```sh
# All tests
python3 -m unittest discover -s tests -v

# One test class / one test
python3 -m unittest tests.test_compose_auth.ValidateTests -v
python3 -m unittest tests.test_compose_auth.EndToEndTests.test_write_file_default_location_and_merge -v

# If PyYAML is not importable, mirror what action.yml does
python3 -m pip install --target /tmp/pyyaml pyyaml
PYTHONPATH=/tmp/pyyaml python3 -m unittest discover -s tests -v
```

`EndToEndTests.run_script` forwards `PYTHONPATH` to the subprocess, so a
private PyYAML install reaches the script under test.

There is no linter or formatter configured. CI (`.github/workflows/test.yml`)
runs the unit tests on Ubuntu and macOS and then exercises the action itself
(`uses: ./`) with dummy credentials: all nine methods, env + file output,
default-token behaviour, merge into a seeded `COMPOSER_HOME/auth.json`, a
real `composer diagnose` check on Linux, and five invalid inputs that must
fail the step without writing a file. Changes to behaviour usually need both
a unit test and a matching assertion in that workflow.

## Architecture of compose_auth.py

Pipeline in `run()`: parse booleans, `parse_yaml` → `validate` →
`add_default_github_token` → emit `::add-mask::` for the compact JSON → log
methods/hosts only → `append_github_file` to `GITHUB_ENV` → optionally
`resolve_file_path` / `load_existing` / `merge` / `write_file` → write the
`file-path` output. `main()` converts `AuthError` into a `::error::` command
and exit code 1; anything else is a genuine bug and should traceback.

- **`SCHEMA`** is the single source of truth for accepted shapes. Each method
  has a `kind` (`string`, `object`, `string-or-object`, `string-list`) plus
  `required`/`optional` key→type maps. Adding a method means adding a
  `SCHEMA` entry, a row in the README table, a line in `action.yml`'s `auth`
  description, and a host in `ALL_METHODS_YAML` + the CI workflow's
  all-methods step. Shapes come from Composer's `res/composer-schema.json`.
- **Validation errors name method/host/key, never values.** `AuthError`
  messages are the only thing that reaches the log on failure. `parse_yaml`
  deliberately avoids `str(exc)` because PyYAML's message echoes the
  offending line. `test_error_messages_never_contain_values` and
  `assert_no_leak` in the end-to-end tests enforce this; keep any new error
  path consistent with it.
- **Numbers and booleans are rejected, not coerced.** An unquoted token
  like `12345` or `yes` parses as int/bool; `_check_scalar` fails with a
  "quote the value" hint. The only integer field is `gitlab-oauth.expires-at`.
- **Empty values are treated as unset secrets.** `None` or whitespace-only
  strings fail with "is the secret it comes from set?".
- **Default `github-oauth` entry** uses the host of `GITHUB_SERVER_URL`
  (GHES-aware), is skipped when the token is empty, and loses to an explicit
  entry for the same host. This mirrors `shivammathur/setup-php`.
- **`composer_home()`** reimplements `Composer\Factory::getHomeDir`
  (`COMPOSER_HOME` → `%APPDATA%/Composer` → first existing of
  `$XDG_CONFIG_HOME/composer` and `~/.composer`, falling back to XDG).
  It takes `env` and `windows` parameters so tests can drive it without
  touching the real environment.
- **File write is merge-then-atomic.** `merge` is per method per host with
  this run's entries winning; `write_file` uses `mkstemp` + `chmod 0600` +
  `os.replace` in the target directory. A path inside `GITHUB_WORKSPACE`
  emits a `::warning::` (deploy pipelines often upload the workspace).
- **Workflow-command escaping**: `_escape_command_value` percent-encodes
  `%`, `\r`, `\n` for `::add-mask::`/`::error::`/`::warning::`;
  `append_github_file` uses a UUID heredoc delimiter for multi-line safety.

## Release process

Tag `vX.Y.Z` and publish a GitHub release. `.github/workflows/release.yml`
force-moves the floating major tag (`v1`) to that release, so consumers
pin `@v1`. Breaking changes to inputs/outputs mean a new major.
