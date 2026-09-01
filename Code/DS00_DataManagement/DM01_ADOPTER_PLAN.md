# DM01_StructureAdopter — plan

Version: v1.2 (01.09.2026)
Status: **implemented + validated on a real store** (01.09.2026) —
script + 25 tests landed; full suite 284 passing; end-to-end run on
`/mnt/d/Tier3_ColdStorage` (18 projects): audit caught 6 genuine tree
faults + two false-positive classes (fixed: NAS `@eaDir` noise ignored,
doc/code folders at date level warn not fail), `--apply` merged
append-only (WheatHeat metadata reconstructed from nothing, existing
rows untouched), ProjectBuilder pass validated every checksum, adopted
metadata committed to the Tier3-APPN-42-DataStorage repo. One post-plan
refinement: non-project-shaped unknown folders at root grade **warn**
(`unrecognised_root_folder`), not fail — only project-shaped strays
block `--apply`. README "Adopting an Existing Data Store" documents the
user workflow; `.gitignore` gained the master-repo depth-rule allowlist
so wrapped data stores can never stage collected data.
Home: `Code/DS00_DataManagement/DM01_StructureAdopter.py` (this repo — the
generic repo is the product; users fork/template it, so the adopter is
developed here, not synced down from the master).

## 1. Purpose

`ProjectBuilder.py` builds an APPN folder structure *from* metadata
(NodeSummary.yaml → ProjectsSummary.csv → ProjectSummary.yaml →
FieldLog.csv). DM01 is the inverse for trees that already exist in the
APPN on-disk format but were assembled by hand (or by another tool): it

1. **audits** the tree against the convention and reports non-compliance,
   and
2. **reconstructs the metadata files ProjectBuilder consumes**, so that a
   subsequent ProjectBuilder run adopts the tree as its own and creates
   everything derived.

## 2. Intended workflow (README-owned, not script-owned)

The user-facing steps live in the README; DM01 assumes steps 1–3 are done
and never creates or configures a git repo itself:

1. Fork / "use this template" on the generic repo → your own copy.
2. In the root directory of the existing data tree: set the git remote to
   your copy and pull (the repo lands *around* the data tree; `.gitignore`
   already excludes data files).
3. Edit `NodeSummary.yaml` — the node `name` must match the existing node
   folder name, and `SensorPlatforms` must list every sensor in use.
4. `python Code/DS00_DataManagement/DM01_StructureAdopter.py`
   → audit only. Fix every **fail** finding (rename folders etc.), re-run
   until clean.
5. `python Code/DS00_DataManagement/DM01_StructureAdopter.py --apply`
   → writes the reconstructed metadata (dry-run listing + y/N confirm
   first, per the OT00/DS00 convention).
6. `python ProjectBuilder.py --historical --enable-sensors` (add
   `--no-git` to skip commits) → creates all derived artefacts and
   checksums.
7. Fill in the human-only placeholders (see §5 TODO list), commit.

## 3. Core design decision — reconstruct inputs, not outputs

ProjectBuilder is already idempotent over an existing tree (`pymkdir`
no-ops, `RunOverview.csv` is created only when missing, READMEs are
seeded once, checksums are computed when `CheckSum` is NaN). DM01
therefore **never re-implements a ProjectBuilder writer**. It writes only
the three driving files (§5) and defers every derived artefact —
`RunOverview.csv`, `FieldNotes.txt`, `run_XX_Issues.yaml`,
`Documentation/{Plot_Layout,Trial_Info}/README.md`, tier folders,
`QC_data`/`Vault`, checksums, git staging — to the ProjectBuilder pass in
step 6. Reused code is imported (`import ProjectBuilder as pb` — the repo
root is already on `sys.path`): `pb._defaultProjectYAML`,
`pb._sitenamemaker` (round-trip verification of inverted site names),
`pb.pymkdir`.

Guarantee this buys: the adopted tree's metadata is byte-compatible with
what ProjectBuilder maintains, because ProjectBuilder wrote it.

## 4. Crawl and inference

- Crawl with `rglob` and route every path through
  `cf.parse_APPN_dataset_path` (R8) — its `valid`/`errors` output *is*
  the compliance engine.
- Group parsed runs into node → project → site → sensor → date → runs.
- Inversions:
  - **Site**: folder `{YYYY}{SiteName}[_F|_C]` → `name` (strip leading
    year), `year`, `ControlledEnvironment` (`_C` → True, `_F` → False,
    no suffix → None). Verified by round-tripping through
    `pb._sitenamemaker`.
  - **Field-log row**: one per (site, sensor, `YYYYMMDD`) folder.
    `Runs` = number of `run_XX` folders (gaps reported, §6).
- Merge with any pre-existing metadata: **never overwrite** a
  hand-made `FieldLog.csv` / `ProjectSummary.yaml` /
  `{Node}_ProjectsSummary.csv` — only append missing rows / sites /
  sensor flags; disagreements between file and tree are report findings,
  not silent edits.

## 5. Reconstructed files (the ProjectBuilder inputs)

| File | Content inferred from tree | Placeholders left for humans |
|---|---|---|
| `{Node}_ProjectsSummary.csv` | one row per project folder; sensor bools TRUE for sensors actually present | — |
| `ProjectSummary.yaml` (per project) | `pb._defaultProjectYAML()` skeleton + `sites` list (name/year/ControlledEnvironment per §4); `sensors` left empty (ProjectBuilder rebuilds it from the field log) | FullName, description, dates, funding, researchers, site lat/long/season/description |
| `FieldLog.csv` (per project) | Year/Month/Day, Sensor, Site, `Runs` = run-folder count | `Technician = "Unknown"` (must be non-empty str for `Rowchecker`); **`CheckSum` left blank** — `Rowchecker` fills it on the ProjectBuilder pass |

Locked decisions:

- `MakeNotesFile` / `MakeTableFile` = **True always** (operator,
  2026-09-01) — the ProjectBuilder pass brings every adopted date folder
  fully up to standard.
- **No SyncSummary, no RDS, no storage-tier logic of any kind** — those
  are master-repo concerns; DM01 uses generic information only.
- The audit report ends with the **TODO list of placeholders** (which
  YAML fields / Technician cells need real values) so step 7 is a
  checklist, not archaeology.

## 6. Compliance report

Written to `./{Node}/DM01_AdoptionReport.md` (overwritten each run — git
history keeps priors) + a REPORTED/SKIPPED status DataFrame from
`main()` per repo idiom. Findings classed by severity:

| Class | Examples | Blocks `--apply`? |
|---|---|---|
| **fail — unparseable** | names `parse_APPN_dataset_path` rejects: bad `YYYYMMDD`, `run1` vs `run_01`, site missing year prefix, project not `YYYY_Desc…`, projects sitting at root with no node folder | yes — no FieldLog row can be inferred |
| **fail — unknown sensor** | sensor folder not in `NodeSummary.yaml` `SensorPlatforms` | yes, for that branch |
| **fail — node mismatch** | node folder name doesn't match any `nodes[].name` in NodeSummary.yaml | yes |
| **warn — structure gaps** | missing `T0_raw`/`T1_proc`/`T2_traits`, missing `QC_data`/`Vault` for GOBI/CALVIS, non-contiguous run numbers (`run_00`, `run_03` — ProjectBuilder assumes 0..N−1) | no — tiers get created by ProjectBuilder; run gaps need manual renumbering |
| **warn — misplaced files** | loose files at levels the spec says are folder-only | no |
| **info — metadata TODOs** | placeholder Technician, empty site metadata, tree↔existing-metadata disagreements | no |

Exit code: nonzero when any **fail** finding exists (scriptable as a
hand-over gate). `--apply` refuses to run while fails are present and
skips only the failing branches is **not** offered — fix, then apply
(keeps partial-adoption states out of the metadata).

## 7. CLI

```
--path      root of the tree to adopt (default: the git repo root).
            Anything other than the repo root works but raises a
            UserWarning — off the supported workflow (operator,
            2026-09-01) — and implies no ProjectBuilder hand-off advice.
--apply     write the reconstructed metadata (default: audit only).
            Prints the dry-run file list and asks y/N before writing.
--projectsYAML  ./NodeSummary.yaml (same flag name as ProjectBuilder)
```

No `--no-git`: DM01 itself never touches git — files are picked up by
the user's own commit or by the ProjectBuilder pass.

## 8. Script skeleton (per AGENTS.md template)

```
main(args)
├── load_node_summary(path)          # NodeSummary.yaml → nodes/sensors
├── crawl_tree(root, node)           # rglob + cf.parse_APPN_dataset_path
├── build_tree_model(parsed)         # node→project→site→sensor→date→runs
├── audit_tree(model, node, existing)# findings list (§6 classes)
├── write_report(findings, node_dir) # DM01_AdoptionReport.md + TODOs
└── if args.apply and no fails:
    ├── plan_writes(model, existing) # dry-run listing
    ├── confirm()                    # y/N
    ├── write_projects_summary(...)  # append-only merge
    ├── write_project_yaml(...)      # pb._defaultProjectYAML + sites
    └── write_field_log(...)         # append-only merge, CheckSum blank
```

## 9. Tests (`Code/DS00_DataManagement/tests/`)

Synthetic `tmp_path` trees (machine-independent, same pattern as the
`parse_APPN_dataset_path` tests):

- compliant tree → clean audit, `--apply` produces metadata that a
  ProjectBuilder dry pass accepts (Rowchecker passes, checksums fill).
- each fail class fires on a crafted bad name; exit code nonzero.
- site-name inversion round-trips through `pb._sitenamemaker` for
  `_F` / `_C` / no-suffix.
- append-only merge: pre-existing FieldLog rows/YAML sites survive
  untouched; tree↔metadata disagreement produces an info finding.
- run-gap tree (`run_00`, `run_03`) → warn + `Runs` inference reported.

## 10. Out of scope

- Creating/configuring the git repo or remote (README step, operator
  decision 2026-09-01).
- Storage-tier / sync metadata (master-repo only).
- Renaming non-compliant folders automatically — DM01 reports, the human
  renames. (A future `--fix` for mechanical renames like `run1` →
  `run_01` could be a stretch goal; not v1.)
- Running ProjectBuilder itself — the hand-off is a printed instruction,
  not an invocation, so there is exactly one builder entry point.
