# Codeboarding Outputs: task-management-public-release

- Target repo: `<repo-root>`
- Output directory: `docs/codeboarding`
- Generated at: `2026-07-30T05:12:49Z`
- Evidence source: deterministic static scan by `repo_inventory.py`

## Start Here

1. Read `architecture.md` for the high-level inventory and overview diagram.
2. Read `module-map.md` for package/module evidence and cheap import edges.
3. Read `runtime-flow.md` for manifest/config entrypoints.
4. Read `data-flow.md` for inventory-generation flow and safety exclusions.
5. Use `inventory.json` as the machine-readable source for follow-up Codex synthesis.

## Files

```text
docs/codeboarding/
  README.md
  architecture.md
  module-map.md
  runtime-flow.md
  data-flow.md
  inventory.json
  diagrams/
    overview.mmd
    module-dependencies.mmd
    runtime-flow.mmd
    data-flow.mmd
```

## Viewing

- Markdown files contain inline Mermaid fenced diagrams.
- GitHub/GitLab render Mermaid diagrams when these Markdown files are viewed there.
- Local Markdown viewers may need Mermaid support enabled or an extension.
- Standalone diagrams live under `diagrams/*.mmd`.

Optional Mermaid CLI render:

```bash
cd <repo-root>
npx -y @mermaid-js/mermaid-cli \
  -i docs/codeboarding/diagrams/overview.mmd \
  -o docs/codeboarding/diagrams/overview.svg
open docs/codeboarding/diagrams/overview.svg
```

`npx` is optional and may fetch packages on first use. The inventory script itself
uses only the Python standard library.

## Evidence vs. Inference

- `inventory.json`, file counts, important configs, entrypoints, and cheap import
  edges are static evidence.
- Architecture or runtime claims beyond those facts require follow-up Codex
  synthesis and should be labeled as inference.
- Skipped secret/runtime-like paths were intentionally not read.
