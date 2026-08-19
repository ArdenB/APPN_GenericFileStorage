# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Coding rules and repo map

All coding rules (script template, no-hidden-inputs, git-root bootstrap,
docstrings) and the repo map (what this repo is, commands, architecture,
pipeline stages) live in the imported file below — follow them.

@AGENTS.md

## Machine- and workflow-specific notes

- Conda env: `conda activate datastorage` (full geospatial stack). Sub-folder
  READMEs name different env names for the same stack — `datastorage` is the
  working one on this machine. There is no `environment.yml` here — the root
  README lists the `conda create` line (the node-side repo does have one).
- Linting is pylint with `--disable=C,R` plus the template-driven warning
  exemptions in `.vscode/settings.json` (untracked).
- **Code flows node-side → here.** Features are developed against real data in
  APPN-42-datastorage and then *ported* into this repo generically (see the
  `DS0x port: ...` commits). When something looks half-finished here, the
  node-side repo is usually where the rest of it lives — check there before
  assuming a gap.
