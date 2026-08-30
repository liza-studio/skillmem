# Publishing skillmem: PyPI, MCP Registry, Claude Code plugin

Distribution status as of 2026-08-30: **nothing is published yet**. This document is the
runbook. Steps marked **[owner]** need accounts/tokens that only the project owner holds —
nothing in CI or in this repo performs them automatically.

The formats below follow the official docs as of 2026-08-30:

- Claude Code plugins: <https://code.claude.com/docs/en/plugins-reference> and
  <https://code.claude.com/docs/en/plugin-marketplaces>
- MCP Registry: <https://github.com/modelcontextprotocol/registry> →
  `docs/modelcontextprotocol-io/quickstart.mdx`, `package-types.mdx`,
  `docs/reference/server-json/generic-server-json.md`
  (schema `https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json`;
  the registry is still labelled *preview* — re-check the docs before publishing)

Publish in this order — the MCP Registry only stores metadata and verifies ownership
against the live PyPI package, so PyPI must go first.

---

## 1. PyPI

CI already builds the sdist + wheel on every push (`build` job in
`.github/workflows/ci.yml`, `python -m build`). Publishing is manual:

1. **[owner]** Create a PyPI account and verify that the name `skillmem` is free:
   <https://pypi.org/project/skillmem/> should 404.
2. **[owner]** Create an API token (scope: entire account for the first upload; after the
   project exists, replace it with a project-scoped token). Store it locally only — do
   not commit it, do not put it in repo secrets until step 4.
3. Make sure `README.md` contains the MCP ownership marker (it does, near the top):

   ```
   <!-- mcp-name: io.github.liza-studio/skillmem -->
   ```

   The registry verifies PyPI ownership by finding this exact string in the package
   description (= README). Removing it breaks step 2 of the registry publish.
4. Upload:

   ```bash
   python -m pip install build twine
   python -m build
   python -m twine upload dist/*        # prompts for the token
   ```

   Or, for repeatable releases, add [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
   (GitHub OIDC, no long-lived token) and a `release.yml` workflow triggered on tags —
   **[owner]** configures the trusted publisher on pypi.org first.
5. Smoke-test from a clean environment:

   ```bash
   uvx --from skillmem skillmem doctor
   ```

Version bumps: `version` lives in `pyproject.toml`, and must be mirrored in
`server.json` (two places: top-level `version` and `packages[0].version`) and in
`.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json`.

---

## 2. MCP Registry (registry.modelcontextprotocol.io)

Manifest: [`server.json`](../server.json) in the repo root. Server name:
`io.github.liza-studio/skillmem` (GitHub-authenticated namespace — no DNS setup needed).

**Prerequisite — `skillmem mcp` subcommand.** `server.json` describes the launch command
as `uvx skillmem mcp` (registry clients run `uvx <identifier> <packageArguments>`, and the
identifier must equal the PyPI package name for ownership verification, so the
`skillmem-mcp` console script cannot be referenced directly). The CLI does not have an
`mcp` subcommand yet — it needs a ~5-line click command that calls
`skillmem.mcp_server.run()`. That is a package-code change and deliberately **not** part
of the `packaging` branch; land it through normal review before publishing to the
registry. (Everything published to Claude Code via the plugin uses the existing
`skillmem-mcp` entry point and does not wait on this.)

Steps:

1. Publish to PyPI first (section 1) — the registry validates that the package exists
   and that its README contains `mcp-name: io.github.liza-studio/skillmem`.
2. Install the publisher CLI:

   ```bash
   brew install mcp-publisher
   # or:
   curl -L "https://github.com/modelcontextprotocol/registry/releases/latest/download/mcp-publisher_$(uname -s | tr '[:upper:]' '[:lower:]')_$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/').tar.gz" | tar xz mcp-publisher && sudo mv mcp-publisher /usr/local/bin/
   ```
3. Validate the manifest offline:

   ```bash
   mcp-publisher validate    # run in the repo root, reads ./server.json
   ```
4. **[owner]** Authenticate — GitHub device flow, must be done as an account that can act
   for the `liza-studio` org (the `io.github.liza-studio/*` namespace is granted by
   GitHub auth for that org; a personal account only gets `io.github.<username>/*`):

   ```bash
   mcp-publisher login github
   ```
5. Publish:

   ```bash
   mcp-publisher publish
   ```
6. Verify:

   ```bash
   curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.liza-studio/skillmem"
   ```

For later releases the login + publish can be automated with GitHub Actions OIDC
(`docs/modelcontextprotocol-io/github-actions.mdx` in the registry repo) — no stored
token needed.

---

## 3. Claude Code plugin

The repo is **both the plugin and its own single-plugin marketplace**:

| File | Role |
| --- | --- |
| `.claude-plugin/plugin.json` | Plugin manifest; declares the MCP server inline (`skillmem-mcp`) and points at the hooks file |
| `hooks/hooks.json` | The 6 hooks + Stop→migrate, same set as `skillmem init --claude-code --hooks full` |
| `.claude-plugin/marketplace.json` | Marketplace `liza-studio` with one plugin, `source: "./"` |

Nothing to upload — the plugin ships the moment the GitHub repo is public. **[owner]**
flips `liza-studio/skillmem` to public. After that users run:

```
/plugin marketplace add liza-studio/skillmem
/plugin install skillmem@liza-studio
```

Caveats (also in the README):

- The plugin does **not** bundle the Python package. `skillmem` and `skillmem-mcp` must
  be on PATH first: `pip install skillmem` (or `uv tool install skillmem`). Until PyPI
  publication, that means `install.sh` / `install.ps1` — but note those installers put
  the binaries in a venv and symlink them, so check `skillmem doctor` runs from a fresh
  shell before installing the plugin.
- **Choose one wiring, not both.** The plugin replaces `skillmem init --claude-code`'s
  Claude Code wiring (MCP entry in `~/.claude.json` + hooks in `~/.claude/settings.json`).
  Running both duplicates every hook injection and registers the MCP server twice.
  Migrating to the plugin: `skillmem uninstall` (keeps the DB), then install the plugin.
  `skillmem init` without `--claude-code` is still fine for DB creation/migration.
- Plugin version is pinned in `plugin.json`; bump it together with `pyproject.toml`.

Optional later step: submissions to third-party plugin catalogs/directories — only after
PyPI is live, so the install instructions actually work.

---

## Release checklist (per version)

1. Bump `version` in `pyproject.toml`, `server.json` (×2), `.claude-plugin/plugin.json`,
   `.claude-plugin/marketplace.json`; update `CHANGELOG.md`.
2. Gate: `python -m pytest -q` green on CI (3 OS × 2 Python + semantic job).
3. Tag, build, `twine upload` (or tag-triggered trusted-publishing workflow). **[owner]**
4. `mcp-publisher publish` with the new version. **[owner]**
5. Users get the plugin update via `/plugin marketplace update`.
