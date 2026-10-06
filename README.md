# Composer Auth GitHub Action

Build Composer authentication from a single YAML input that mirrors the
[`auth.json`](https://getcomposer.org/doc/articles/authentication-for-private-packages.md)
schema. The action validates the YAML against that schema, merges in the
GitHub token for the current run, and exposes the result as the
`COMPOSER_AUTH` environment variable, as an `auth.json` file, or both.

Each credential stays its own repository or environment secret and is
interpolated into the YAML at run time. Adding, rotating, or removing one
credential never means editing a monolithic JSON secret, and a malformed entry
fails fast with a message naming the method and host instead of a confusing
download error in the middle of `composer install`.

## Usage

```yaml
- uses: lpeabody/composer-auth-action@v1
  with:
    auth: |
      http-basic:
        connect.advancedcustomfields.com:
          username: "${{ secrets.ACF_PRO_KEY }}"
          password: https://example.com
        objectcache.pro:
          username: token
          password: "${{ secrets.OBJECTCACHE_TOKEN }}"

- run: composer install --no-dev --prefer-dist
```

With the defaults above the action exports `COMPOSER_AUTH` for every later
step in the job and also includes `github-oauth.github.com` set to the run
token, so private GitHub-hosted packages work with no extra configuration.

To write a global `auth.json` instead of (or as well as) the environment
variable:

```yaml
- uses: lpeabody/composer-auth-action@v1
  with:
    export-env: false
    write-file: true          # defaults to $COMPOSER_HOME/auth.json
    auth: |
      bearer:
        packages.example.com: "${{ secrets.PACKAGES_TOKEN }}"
```

## Inputs

| Input | Default | Description |
|---|---|---|
| `auth` | `''` | Multiline YAML mirroring the `auth.json` schema. Top-level keys are authentication methods; each is keyed by host. See [Supported methods](#supported-methods). |
| `github-token` | `${{ github.token }}` | Token for the default `github-oauth` entry for this run's GitHub host (`github.com`, or your GHES host). Pass `''` to omit the entry. An explicit `github-oauth` entry in `auth` for the same host takes precedence. |
| `export-env` | `'true'` | Write the composed JSON to `COMPOSER_AUTH` (via `GITHUB_ENV`) for all later steps in the job. |
| `write-file` | `'false'` | Write the composed JSON to an `auth.json` file. |
| `file-path` | `''` | Path of the file written when `write-file` is true. Empty means Composer's global `auth.json`. See [File output](#file-output). |

At least one of `export-env` and `write-file` must be true.

## Outputs

| Output | Description |
|---|---|
| `file-path` | Absolute path of the `auth.json` written, or empty when `write-file` is false. |

The composed JSON is deliberately **not** an output.

## Supported methods

The accepted shapes are taken from Composer's own
[`composer-schema.json`](https://github.com/composer/composer/blob/main/res/composer-schema.json),
at the Composer release recorded in [`SCHEMA_VERSION`](SCHEMA_VERSION).

| Method | Value per host |
|---|---|
| `http-basic` | mapping with `username` and `password` |
| `bearer` | string |
| `github-oauth` | string |
| `gitlab-oauth` | string, or mapping with `token` and optional `refresh-token` (string) and `expires-at` (integer) |
| `gitlab-token` | string, or mapping with `username` and `token` |
| `bitbucket-oauth` | mapping with `consumer-key` and `consumer-secret` |
| `custom-headers` | list of `"Header-Name: value"` strings |
| `client-certificate` | mapping with `local_cert` and optional `local_pk`, `passphrase` |
| `forgejo-token` | mapping with `username` and `token` |

```yaml
auth: |
  http-basic:
    repo.packagist.com:
      username: token
      password: "${{ secrets.PACKAGIST_TOKEN }}"
  bearer:
    packages.example.com: "${{ secrets.PACKAGES_TOKEN }}"
  gitlab-token:
    gitlab.com: "${{ secrets.GITLAB_TOKEN }}"
  bitbucket-oauth:
    bitbucket.org:
      consumer-key: "${{ secrets.BB_KEY }}"
      consumer-secret: "${{ secrets.BB_SECRET }}"
  custom-headers:
    satis.example.com:
      - "X-Api-Key: ${{ secrets.SATIS_KEY }}"
  client-certificate:
    cert.example.com:
      local_cert: /etc/ssl/client.pem
      passphrase: "${{ secrets.CERT_PASSPHRASE }}"
  forgejo-token:
    codeberg.org:
      username: deploy
      token: "${{ secrets.CODEBERG_TOKEN }}"
```

### Validation rules

The step fails, before anything is written, when:

- the YAML does not parse (the error names the line and column only, never the content);
- a top-level key is not one of the nine methods above;
- a method has no hosts, or a host has the wrong value shape (for example a
  string where `http-basic` expects a mapping, or an unknown key inside a mapping);
- a credential value is empty. An unset secret interpolates as an empty
  string, so `password: ${{ secrets.MISSING }}` is reported as
  `http-basic.<host>.password: value is empty (is the secret it comes from set?)`;
- a value parsed as a number or boolean rather than a string (see below).

Error messages name the method, host, and key. They never include values.

### Quote interpolated secrets

`${{ secrets.X }}` is substituted as raw text before the YAML is parsed, so a
credential is subject to YAML syntax. Wrap interpolations in double quotes:

```yaml
password: "${{ secrets.OBJECTCACHE_TOKEN }}"
```

Unquoted, a credential that starts with a character such as `*`, `&`, `!`,
`%`, `@`, `{`, `[`, `#`, or `|`, or that contains `: ` or ` #`, breaks or
silently changes the parse. A purely numeric credential (`12345`) becomes an
integer and `yes`/`no`/`on`/`off` become booleans; the action rejects both and
tells you to quote the value. Credentials containing `"` or `\` need those
escaped inside a double-quoted YAML scalar; most API tokens contain neither.

## Default GitHub token

Matching the behaviour of [`shivammathur/setup-php`](https://github.com/shivammathur/setup-php),
the composed JSON includes `github-oauth.<host>` set to `github-token`, where
`<host>` is the host of `GITHUB_SERVER_URL` (`github.com` on GitHub.com). The
default `github-token` is the run's `github.token`.

- Pass `github-token: ''` to omit the entry.
- An explicit `github-oauth` entry in `auth` for the same host wins over the default.

## File output

With `write-file: true` and no `file-path`, the file goes to Composer's
**global** `auth.json`, resolved exactly as Composer does: `COMPOSER_HOME`
when set, otherwise `%APPDATA%/Composer` on Windows, otherwise the first of
`$XDG_CONFIG_HOME/composer` (when XDG is in use) and `~/.composer` that already
exists, falling back to the XDG one. That location is outside the workspace.

An existing file is merged. Entries from this run take precedence per method
and host; entries already in the file for other hosts are preserved. The file
is written atomically with mode `0600`.

### Do not write auth.json into the workspace

Many deploy pipelines commit or upload the workspace wholesale (for example a
`git add --force .` followed by a push to a hosting provider). An `auth.json`
inside the workspace would ship with it. Keep the default location, or point
`file-path` somewhere under `${{ runner.temp }}`. The action emits a workflow
warning when the resolved path is inside `GITHUB_WORKSPACE`.

## Log safety

The action never prints the composed JSON or any credential value. Values that
came from `${{ secrets.* }}` are already masked by Actions; the action also
registers the whole composed document as a mask so a later step cannot echo it
into the log. The step log lists only method names and hosts.

## Requirements

- A runner with `bash` and `python3` (Ubuntu, macOS, and Windows hosted runners all qualify).
- PyYAML. Ubuntu hosted runners ship it; elsewhere the action installs it into a private directory under `RUNNER_TEMP` with `pip --target` if the import fails, leaving the runner's Python untouched.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The unit tests cover schema validation, the default token, merge semantics,
Composer home resolution, and end-to-end runs of the script with `GITHUB_ENV`
and `GITHUB_OUTPUT` redirected to temporary files, including a check that no
dummy credential appears in the step output. The `Test` workflow additionally
runs the action itself on Ubuntu and macOS with dummy credentials, asserts the
composed document, the env export, the file write and merge, the default file
location, that Composer picks the credentials up, and that each kind of
invalid input fails the step without writing anything.

## Versioning

This action follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).
Releases are tagged `vMAJOR.MINOR.PATCH` with a `v` prefix, as is conventional
for GitHub Actions.

### What counts as the public API

The versioned surface is everything a workflow can observe:

- the input names, their defaults, and their documented semantics;
- the output names and their documented values;
- the accepted YAML schema (methods, value shapes, validation rules);
- the composed JSON for a given input;
- the default `auth.json` location and the merge behaviour;
- the log-safety guarantees (no composed JSON or credential value is printed).

The Python module layout, function names, and test suite are internal and may
change in any release.

### Relationship to Composer's schema

The accepted YAML mirrors the `config` section of Composer's
[`composer-schema.json`](https://github.com/composer/composer/blob/main/res/composer-schema.json)
as of the Composer release named in [`SCHEMA_VERSION`](SCHEMA_VERSION). The
action tracks that file, so changes upstream flow through as follows:

| Composer change | Action release |
|---|---|
| Adds a method, an optional key, or a wider value type | MINOR |
| Removes or renames a method or key, adds a required key, or narrows a value type | MAJOR, released only once the Composer version that made the change is generally available |
| Changes only descriptions or documentation | none |

The action never accepts input that the referenced Composer release would
reject, and never rejects input it would accept. Where the two disagree, the
action is wrong and the fix is a PATCH. Each release note states the Composer
version whose schema it matches.

If you are on a newer Composer and need a method the action does not yet
know, open an issue naming the Composer version; support lands as a MINOR.

### Bump rules

| Change | Bump | Examples |
|---|---|---|
| Incompatible | **MAJOR** | Removing or renaming an input or output. Changing a default. Changing the composed JSON for input that was previously valid. Rejecting YAML that an earlier release accepted. Changing where the default `auth.json` is written. |
| Backward-compatible addition | **MINOR** | Adding an optional input or output. Accepting a new authentication method or value shape that Composer adds. Deprecating an input while still honouring it. |
| Backward-compatible fix | **PATCH** | Correcting a validation message, the Composer home resolution, or the merge logic so behaviour matches the documentation. Dependency and CI changes. |

Tightening validation to reject input that was already invalid for Composer
counts as a fix. Tightening it to reject input Composer accepts is a breaking
change.

Deprecations are announced in the release notes at least one MINOR release
before the MAJOR release that removes them.

### Tags and pinning

| Reference | Moves? | Use when |
|---|---|---|
| `@v1` | Yes, to the latest `v1.x.y` release | You want fixes and additions automatically and can tolerate no breaking changes. The default for most workflows. |
| `@v1.2.3` | Never | You want a fixed version and will bump deliberately. |
| `@<commit sha>` | Never | You require an immutable reference, for example under a supply-chain policy. |

The `Release` workflow force-moves the floating major tag (`v1`, `v2`, ...)
to each published release of that major. Full-version tags are never moved or
deleted; a mistaken release is followed by a new release, not a re-tag. When a
new major ships, the previous floating tag stays at its last release and only
receives backports if a security issue warrants one.

### Release notes

GitHub Releases are the changelog. Each release lists changes grouped as
breaking, added, and fixed, and names any deprecated inputs along with their
replacement.

## License

[MIT](LICENSE)
