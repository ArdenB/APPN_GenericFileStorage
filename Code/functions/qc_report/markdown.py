"""Markdown section fragments + QC_report.md assembly for the QC scripts.

Implements the per-run ``QC_report.md`` design (QC-report plan,
development-master repo; parent = retired pipeline plan §7
revision item 3): each per-run QC script renders **only its own section**
(from the report dict it just passed to ``write_report``) to a fragment
file it owns (``QC_data/<script>/<script>_section.md``), then reassembles
``QC_data/QC_report.md`` by concatenation — header + overview table + the
four fragments in QC00→QC03 order (stub where absent) + footer.

Hard constraints (plan §1):

- JSON-only source — nothing here recomputes, re-measures, or opens
  rasters/gpro/CSVs. Assembly reads only the contract summaries/details
  and the fragment files.
- Never load-bearing — the summary YAML stays the completion marker;
  a render failure warns and returns None, it never gates the caller.
"""

import json
import os
import pathlib
import warnings as warn
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yaml

import Code.functions.core_functions as cf
from Code.functions.qc_report.reader import read_report
from Code.functions.qc_report.report import report_paths


# ==================================================================================
def update_qc_report(
        qc_data_dir: pathlib.Path,
        report: Dict[str, Any],
    ) -> Optional[pathlib.Path]:
    """Render the calling script's section fragment, then reassemble
    ``QC_data/QC_report.md``.

    Step 1 — dispatch on ``report["script"]["name"]`` to that script's
    section renderer (current schema only: the dict the caller just passed
    to ``write_report``) and atomically write
    ``QC_data/<script>/<script>_section.md``.
    Step 2 — assemble ``QC_report.md``: header + overview table + the four
    fragments in QC00→QC03 order (stubs where absent) + footer.

    Never raises: the contract report is already on disk and the markdown
    is never load-bearing, so any failure is reported as a warning and
    ``None`` is returned (plan §3 call-site rules).

    Parameters
    ----------
    qc_data_dir : pathlib.Path
        The run's ``T1_proc/QC_data/`` folder.
    report : dict
        The contract detail-report dict the caller just wrote via
        ``write_report`` (post-derive, so ``status`` is final).

    Returns
    -------
    pathlib.Path or None
        The assembled ``QC_report.md`` path, or None if rendering failed.
    """
    qc_data_dir = pathlib.Path(qc_data_dir)
    try:
        script_name = report["script"]["name"]
        renderers = _section_renderers()
        if script_name not in renderers:
            raise KeyError(
                f"No section renderer for {script_name!r} — "
                f"known: {list(renderers)}.")
        fragment = "\n".join(
            renderers[script_name](report, qc_data_dir)) + "\n"
        frag_path = _fragment_path(qc_data_dir, script_name)
        frag_path.parent.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(frag_path, fragment)
        report_path = qc_data_dir / "QC_report.md"
        _write_text_atomic(report_path, _assemble(qc_data_dir, report))
        return report_path
    except Exception as err:  # never gate the calling QC script
        warn.warn(f"QC_report.md render failed ({err!r}); the contract "
                  "report is unaffected.")
        return None


# ==================================================================================
# ========== Shared rendering primitives (reused by plan §7 item 4) ==========
# ==================================================================================
def status_glyph(status: Optional[str]) -> str:
    """Return the display glyph for a check- or script-level status.

    Parameters
    ----------
    status : str or None
        Any status from the shared vocabulary (both levels accepted).

    Returns
    -------
    str
        One of ✅ ⚠️ ❌ ➖ (unknown/None map to ➖).
    """
    mapping = {
        "pass": "✅", "good": "✅", "acceptable": "✅",
        "warn": "⚠️", "warning": "⚠️",
        "fail": "❌",
        "not_evaluated": "➖", "not_checked": "➖",
    }
    return mapping.get(str(status), "➖")


# ==================================================================================
def checks_table(checks: Dict[str, Dict[str, Any]]) -> List[str]:
    """Render a report's ``checks`` mapping as a markdown table.

    One row per check: name, glyph + status (suffixed ``(advisory)`` /
    ``(waived)`` when flagged), headline value and note.

    Parameters
    ----------
    checks : dict
        Check name → check object (contract shape).

    Returns
    -------
    list of str
        Markdown lines (empty list when there are no checks).
    """
    if not checks:
        return []
    rows = []
    for name, chk in checks.items():
        status = chk.get("status", "not_checked")
        label = f"{status_glyph(status)} {status}"
        if chk.get("advisory"):
            label += " (advisory)"
        if chk.get("waived"):
            label += " (waived)"
        rows.append({"Check": name, "Status": label,
                     "Value": _cell(chk.get("value")),
                     "Note": _cell(chk.get("note"))})
    return [cf.markdown_table(pd.DataFrame(rows)), ""]


# ==================================================================================
def figure_embeds(
        artifacts: List[str],
        qc_data_dir: pathlib.Path,
    ) -> List[str]:
    """Render image embeds for a report's ``.png`` artifacts.

    Paths are embedded relative to ``QC_data/`` (the assembled report's
    home) with ``/`` separators. A listed figure missing on disk renders
    as plain text with "(missing)" instead of a broken embed.

    Parameters
    ----------
    artifacts : list of str
        ``QC_data``-relative artifact paths (contract convention).
    qc_data_dir : pathlib.Path
        The run's ``QC_data/`` folder (existence checks only).

    Returns
    -------
    list of str
        Markdown lines (empty when there are no figures).
    """
    lines: List[str] = []
    for art in artifacts:
        if not str(art).lower().endswith(".png"):
            continue
        rel = str(art).replace(os.sep, "/")
        title = pathlib.Path(rel).stem
        if (qc_data_dir / rel).is_file():
            lines += [f"![{title}]({rel})", ""]
        else:
            lines += [f"_{rel} (missing)_", ""]
    return lines


# ==================================================================================
def artifact_links(
        artifacts: List[str],
        qc_data_dir: pathlib.Path,
    ) -> List[str]:
    """Render a report's non-figure artifacts as a markdown link list.

    Parameters
    ----------
    artifacts : list of str
        ``QC_data``-relative artifact paths.
    qc_data_dir : pathlib.Path
        The run's ``QC_data/`` folder (existence checks only).

    Returns
    -------
    list of str
        Markdown lines (empty when there are no non-figure artifacts).
    """
    files = [str(a).replace(os.sep, "/") for a in artifacts
             if not str(a).lower().endswith(".png")]
    if not files:
        return []
    lines = ["**Artifacts:**", ""]
    for rel in files:
        name = pathlib.Path(rel).name
        if (qc_data_dir / rel).is_file():
            lines.append(f"- [{name}]({rel})")
        else:
            lines.append(f"- {rel} (missing)")
    return lines + [""]


# ==================================================================================
# ========== Assembly ==========
# ==================================================================================
def _contract_sections() -> Tuple[Tuple[str, str], ...]:
    """Return the fixed section order: (script name, section subject).

    Returns
    -------
    tuple of (str, str)
        QC00→QC03 contract script names with their section subjects.
    """
    return (
        ("QC00_GCPCheck", "GCP geometric accuracy"),
        ("QC01_FlightCheck", "Flight / acquisition"),
        ("QC02_SpectralCheck", "Panel spectra"),
        ("QC03_RasterCheck", "Raster data validity"),
    )


# ==================================================================================
def _assemble(qc_data_dir: pathlib.Path, report: Dict[str, Any]) -> str:
    """Assemble the full ``QC_report.md`` text.

    Header + overview table from the four summary YAMLs, then the four
    section fragments (stubs where absent — plan §6), then the footer.
    Fragments embed as raw bytes; nothing is parsed or re-rendered.

    Parameters
    ----------
    qc_data_dir : pathlib.Path
        The run's ``QC_data/`` folder.
    report : dict
        The triggering script's report dict (identity source).

    Returns
    -------
    str
        The complete markdown document.
    """
    run = report.get("run", {})
    identity = "/".join(
        str(run[key]) for key in
        ("node", "project", "site", "sensor", "date", "run", "run_number")
        if run.get(key) is not None and key in run)
    identity = identity or "unknown run"
    # +++++ trim ISO datetimes down to the date for the title +++++
    identity = identity.replace("T00:00:00", "").replace(" 00:00:00", "")

    meta_bits = [f"{key}: {run[key]}" for key in ("gpro", "graw")
                 if run.get(key)]
    meta_bits.append(
        f"rendered {datetime.now(timezone.utc).isoformat(timespec='seconds')}")

    lines: List[str] = [f"# QC report — {identity}", "",
                        "_" + " · ".join(meta_bits) + "_", ""]
    lines += _overview_table(qc_data_dir)
    for script_name, subject in _contract_sections():
        frag_path = _fragment_path(qc_data_dir, script_name)
        lines.append("")
        if frag_path.is_file():
            try:
                lines.append(frag_path.read_text(encoding="utf-8").rstrip())
            except OSError as err:  # one bad fragment never kills the report
                lines += [f"## {script_name} — {subject}", "",
                          f"_Section unavailable: {err}_"]
                warn.warn(f"Could not read fragment {frag_path}: {err}")
        else:
            lines += _stub_section(qc_data_dir, script_name, subject)
    trigger = report.get("script", {})
    lines += ["", "---", "",
              f"_Assembled by qc_report (schema "
              f"{report.get('schema_version', '?')}) on "
              f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}; "
              f"triggered by {trigger.get('name', '?')} "
              f"{trigger.get('version', '?')}._", ""]
    return "\n".join(lines)


# ==================================================================================
def _overview_table(qc_data_dir: pathlib.Path) -> List[str]:
    """Render the header overview table from the four summary YAMLs.

    One row per contract script in run order: glyph + script status,
    script version, ``generated_utc`` and a freshness flag (plan §4:
    JSON-vs-JSON gpro-mtime comparison only, never a filesystem stat).

    Parameters
    ----------
    qc_data_dir : pathlib.Path
        The run's ``QC_data/`` folder.

    Returns
    -------
    list of str
        Markdown lines for the overview table.
    """
    summaries = {name: _load_summary(qc_data_dir, name)
                 for name, _ in _contract_sections()}
    gpro_mtimes = {name: _recorded_gpro_mtime(qc_data_dir, name)
                   for name, _ in _contract_sections()}
    known = [m for m in gpro_mtimes.values() if m]
    newest = max(known) if known else None

    rows = []
    for name, _ in _contract_sections():
        summary = summaries[name]
        if summary is None:
            rows.append({"Script": name, "Status": "— not yet run",
                         "Version": "—", "Generated (UTC)": "—",
                         "Freshness": "—"})
            continue
        status = summary.get("status", "not_evaluated")
        mtime = gpro_mtimes[name]
        if mtime is None or newest is None:
            fresh = "—"
        else:
            fresh = "ok" if mtime == newest else "stale?"
        rows.append({
            "Script": name,
            "Status": f"{status_glyph(status)} {status}",
            "Version": summary.get("script", {}).get("version", "—"),
            "Generated (UTC)": _cell(summary.get("generated_utc"))[:19],
            "Freshness": fresh,
        })
    return [cf.markdown_table(pd.DataFrame(rows)), ""]


# ==================================================================================
def _stub_section(
        qc_data_dir: pathlib.Path,
        script_name: str,
        subject: str,
    ) -> List[str]:
    """Render the stub for a script with no fragment on disk (plan §6).

    Distinguishes "not yet run", "contract report predates section
    fragments" and "legacy report only".

    Parameters
    ----------
    qc_data_dir : pathlib.Path
        The run's ``QC_data/`` folder.
    script_name : str
        Contract script name.
    subject : str
        Section subject for the heading.

    Returns
    -------
    list of str
        Markdown lines for the stub section.
    """
    heading = [f"## {script_name.split('_')[0]} — {subject}", ""]
    found = read_report(qc_data_dir, script_name)
    if found is None:
        return heading + ["_Not yet run._"]
    if found["legacy"]:
        return heading + [
            f"_Legacy report present ({found['path'].name}, status "
            f"{found['status']}); re-run {script_name} for a full section._"]
    return heading + [
        f"_Report exists (status {found['status']}) but predates section "
        f"fragments — re-run {script_name} to render._"]


# ==================================================================================
def _fragment_path(
        qc_data_dir: pathlib.Path,
        script_name: str,
    ) -> pathlib.Path:
    """Return a script's section-fragment path.

    Parameters
    ----------
    qc_data_dir : pathlib.Path
        The run's ``QC_data/`` folder.
    script_name : str
        Contract script name.

    Returns
    -------
    pathlib.Path
        ``QC_data/<script>/<script>_section.md``.
    """
    return qc_data_dir / script_name / f"{script_name}_section.md"


# ==================================================================================
# ========== Per-script section renderers (current schema only) ==========
# ==================================================================================
def _section_renderers() -> Dict[str, Any]:
    """Return the script-name → section-renderer dispatch mapping.

    Returns
    -------
    dict
        Contract script name → renderer callable.
    """
    return {
        "QC00_GCPCheck": _section_qc00,
        "QC01_FlightCheck": _section_qc01,
        "QC02_SpectralCheck": _section_qc02,
        "QC03_RasterCheck": _section_qc03,
    }


# ==================================================================================
def _section_qc00(
        report: Dict[str, Any],
        qc_data_dir: pathlib.Path,
    ) -> List[str]:
    """Render the QC00 (GCP geometric accuracy) section fragment.

    Checks table, per-pair results table, gate callout on any ``gcp_2d*``
    fail, displacement-figure embeds, artifact links and the config line.

    Parameters
    ----------
    report : dict
        The QC00 contract detail-report dict.
    qc_data_dir : pathlib.Path
        The run's ``QC_data/`` folder (figure existence checks).

    Returns
    -------
    list of str
        Markdown lines for the fragment.
    """
    lines = _section_header(report, "GCP geometric accuracy")
    lines += checks_table(report.get("checks", {}))

    if any(chk.get("status") == "fail"
           for name, chk in report.get("checks", {}).items()
           if name.startswith("gcp_2d")):
        lines += ["> **QC00 fail — downstream QC void.** GNSS reprocessing "
                  "required; QC01/QC02 results in this report may be stale — "
                  "check the freshness column above.", ""]

    rows = []
    for stem, pair in report.get("pairs", {}).items():
        d2d = pair.get("statistics_metres", {}).get("distance_2d", {})
        planar = pair.get("bias", {}).get("planar_2d", {})
        rows.append({
            "Pair": stem,
            "Matched": pair.get("counts", {}).get("matched"),
            "Mean 2D (m)": d2d.get("mean"),
            "Max 2D (m)": d2d.get("max"),
            "RMSE 2D (m)": d2d.get("rmse"),
            "Planar bias (m)": planar.get("bias_magnitude_m"),
            "Result": _cell(pair.get("status", {}).get("result")),
        })
    if rows:
        lines += ["**Per-pair results:**", "",
                  cf.markdown_table(pd.DataFrame(rows)), ""]

    lines += figure_embeds(report.get("artifacts", []), qc_data_dir)
    lines += artifact_links(report.get("artifacts", []), qc_data_dir)
    lines += _config_line(report.get("config"))
    return lines


# ==================================================================================
def _section_qc01(
        report: Dict[str, Any],
        qc_data_dir: pathlib.Path,
    ) -> List[str]:
    """Render the QC01 (flight/acquisition) section fragment.

    Bundle-integrity checks first, then the spec checks, an acquisition
    summary distilled from ``acquisition_report``, artifact links and the
    staleness + config lines.

    Parameters
    ----------
    report : dict
        The QC01 contract detail-report dict.
    qc_data_dir : pathlib.Path
        The run's ``QC_data/`` folder.

    Returns
    -------
    list of str
        Markdown lines for the fragment.
    """
    lines = _section_header(report, "Flight / acquisition")
    checks = report.get("checks", {})
    integrity_names = ("graw_present", "dark_reference", "panels_present",
                       "reflectance_product_vnir",
                       "reflectance_product_swir", "flightcal_spec")
    integrity = {k: v for k, v in checks.items() if k in integrity_names}
    spec = {k: v for k, v in checks.items() if k not in integrity_names}
    if integrity:
        lines += ["**Bundle integrity:**", ""] + checks_table(integrity)
    if spec:
        lines += ["**Spec checks:**", ""] + checks_table(spec)

    acq = report.get("acquisition_report", {})
    bullets = []
    acquisition = acq.get("acquisition", {})
    if acquisition:
        bullets.append(
            f"- Flight lines: {acquisition.get('n_flight_lines', '?')} "
            f"({acquisition.get('n_rogue_lines', 0)} rogue), "
            f"{acquisition.get('first_line_start_utc', '?')} → "
            f"{acquisition.get('last_line_end_utc', '?')}")
    geometry = acq.get("geometry", {})
    if geometry.get("mean_agl_m") is not None:
        bullets.append(f"- Mean AGL: {geometry['mean_agl_m']:.1f} m")
    solar = acq.get("solar", {})
    elev = solar.get("solar_elevation_deg_range")
    if elev:
        bullets.append(
            f"- Solar elevation: {elev[0]:.1f}–{elev[-1]:.1f}°")
    mission = acq.get("mission", {})
    if mission.get("conditions"):
        bullets.append(f"- Conditions: {mission['conditions']} "
                       f"(pilot: {mission.get('pilot', '?')})")
    if bullets:
        lines += ["**Acquisition summary:**", ""] + bullets + [""]

    lines += figure_embeds(report.get("artifacts", []), qc_data_dir)
    lines += artifact_links(report.get("artifacts", []), qc_data_dir)
    stale = report.get("staleness", {})
    if stale.get("gpro_path"):
        lines += [f"_Computed against {pathlib.Path(stale['gpro_path']).name} "
                  f"(mtime {_cell(stale.get('gpro_mtime_utc'))[:19]})_", ""]
    lines += _config_line(report.get("config"))
    return lines


# ==================================================================================
def _section_qc02(
        report: Dict[str, Any],
        qc_data_dir: pathlib.Path,
    ) -> List[str]:
    """Render the QC02 (panel spectra) section fragment.

    Checks table, panel-set identity, the per-target × region table that
    restores the granularity the v3.3 summary collapse removed, the DHR
    delta-stats table (full-region rows), paired overlay/delta figure
    embeds and the config line.

    Parameters
    ----------
    report : dict
        The QC02 contract detail-report dict.
    qc_data_dir : pathlib.Path
        The run's ``QC_data/`` folder.

    Returns
    -------
    list of str
        Markdown lines for the fragment.
    """
    lines = _section_header(report, "Panel spectra")
    lines += checks_table(report.get("checks", {}))

    dhr = report.get("dhr_comparison") or {}
    panel_set = dhr.get("panel_set") or {}
    if panel_set:
        bits = [f"gpro pin: {panel_set.get('gpro_pin', '—')}"]
        if panel_set.get("n_elm_targets"):
            bits.append(f"{panel_set['n_elm_targets']} ELM target(s)")
        lines += [f"**Panel set:** {' · '.join(bits)}", ""]

    rows = []
    targets = (report.get("spectral_report") or {}).get("targets", {})
    for target, regions in targets.items():
        for region, block in regions.items():
            panels = block.get("panels", {})
            nodata = {p: s.get("nodata_zero_fraction")
                      for p, s in panels.items()
                      if s.get("nodata_zero_fraction") is not None}
            all_nodata = [p for p, s in panels.items() if s.get("all_nodata")]
            medians = [abs(s["median_residual_pct"]) for s in panels.values()
                       if s.get("median_residual_pct") is not None]
            rows.append({
                "Target": target,
                "Region": region,
                "Panels": len(panels),
                "Max nodata %": (max(nodata.values()) * 100
                                 if nodata else None),
                "All-nodata panels": ", ".join(all_nodata),
                "Worst abs median residual (pp)": (max(medians)
                                                   if medians else None),
            })
    if rows:
        lines += ["**Per-target extraction (per EM region):**", "",
                  cf.markdown_table(pd.DataFrame(rows), "{:.2f}"), ""]

    full_rows = [r for r in dhr.get("delta_stats", [])
                 if r.get("region") == "full"]
    if full_rows:
        df = pd.DataFrame(full_rows)[
            ["panel_name", "EM_Region", "Panel_ref", "serial",
             "bias_pct", "rmse_pct", "mae_pct", "max_abs_pct"]]
        # differences of reflectance-% report as percentage points (pp);
        # bias_pct = mean per-band delta, so label it as what it is
        df.columns = ["Target", "EM", "Panel", "Serial",
                      "Mean Δ (pp)", "RMSE (pp)", "MAE (pp)",
                      "Max abs Δ (pp)"]
        lines += ["**Observed vs expected DHR (bad bands masked, "
                  "full region; differences in reflectance percentage "
                  "points):**", "",
                  cf.markdown_table(df, "{:.2f}"), ""]

    lines += figure_embeds(_qc02_figure_order(report), qc_data_dir)
    tables = [a for a in report.get("artifacts", [])
              if not str(a).lower().endswith(".png")]
    if tables:
        lines += [f"_Spectra tables: {len(tables)} file(s) under "
                  "`QC_Spectral_Tables/` (see the summary YAML artifact "
                  "list)._", ""]
    config = report.get("config", {})
    lines += _config_line(config.get("spectral_limits"))
    return lines


# ==================================================================================
def _qc02_figure_order(report: Dict[str, Any]) -> List[str]:
    """Order QC02 figure artifacts by reporting priority.

    Operator rule (2026-09-03): the 2-panel VAL sets are the headline
    QC02 result — their figures embed first, then the ELM targets, then
    the remaining (4-panel) VAL sets. Figures whose target cannot be
    matched go last. The sort is stable, so the overlay/delta pairing
    and region order within each target are preserved.

    Parameters
    ----------
    report : dict
        The QC02 contract detail-report dict (targets + panel counts
        come from ``spectral_report``).

    Returns
    -------
    list of str
        The ``.png`` artifacts in embed order.
    """
    targets = (report.get("spectral_report") or {}).get("targets", {})
    n_panels = {
        target: max((len(block.get("panels", {}))
                     for block in regions.values()), default=0)
        for target, regions in targets.items()}

    def priority(art: str) -> int:
        stem = pathlib.Path(str(art)).stem
        match = max((t for t in n_panels if stem.startswith(t)),
                    key=len, default=None)
        if match is None:
            return 3
        if "VAL" in match.upper() and n_panels[match] == 2:
            return 0
        if "ELM" in match.upper():
            return 1
        return 2

    pngs = [str(a) for a in report.get("artifacts", [])
            if str(a).lower().endswith(".png")]
    return sorted(pngs, key=priority)


# ==================================================================================
def _section_qc03(
        report: Dict[str, Any],
        qc_data_dir: pathlib.Path,
    ) -> List[str]:
    """Render the QC03 (raster data validity) section fragment.

    Pivots the per-product check explosion into a check-family × product
    matrix, then one detail block per product (dims, integrity, zone-split
    evidence), figure embeds (none today, future-proof) and the staleness
    + config lines.

    Parameters
    ----------
    report : dict
        The QC03 contract detail-report dict.
    qc_data_dir : pathlib.Path
        The run's ``QC_data/`` folder.

    Returns
    -------
    list of str
        Markdown lines for the fragment.
    """
    lines = _section_header(report, "Raster data validity")
    checks = report.get("checks", {})
    labels = list(report.get("products", {}))
    families = ("header_bin_integrity", "zeros_in_footprint", "over_range",
                "negative", "nan_inf", "capture_extent", "zero_edge_band",
                "dropout_in_roi", "data_outside_bbox")
    if labels:
        rows = []
        for family in families:
            row: Dict[str, Any] = {"Check": family}
            for label in labels:
                chk = checks.get(f"{family}_{label}")
                if chk is None:
                    row[label] = "—"
                else:
                    value = _cell(chk.get("value")) or chk.get("status")
                    row[label] = f"{status_glyph(chk.get('status'))} {value}"
            rows.append(row)
        lines += [cf.markdown_table(pd.DataFrame(rows)), ""]
    else:
        lines += checks_table(checks)

    for label, product in report.get("products", {}).items():
        shape = product.get("shape", {})
        integrity = product.get("header_bin_integrity", {})
        lines += [f"### {label}", "",
                  f"- File: `{product.get('file', '?')}` — "
                  f"{shape.get('bands', '?')} bands × "
                  f"{shape.get('height', '?')} × {shape.get('width', '?')} "
                  f"({shape.get('dtype', '?')}), header/bin "
                  f"{'ok' if integrity.get('ok') else 'MISMATCH'}"]
        zones = product.get("zero_zones", {})
        if zones:
            inset = zones.get("inset", {})
            lines.append(
                f"- Zone split ({zones.get('classifier', '?')} classifier): "
                f"edge band {_pct(zones.get('zero_edge_band_pct'))} zero, "
                f"ROI dropout {_pct(zones.get('dropout_in_roi_pct'))} "
                f"({_pct(zones.get('interior_cc_roi_share_pct'))} "
                f"interior-connected), line spacing from "
                f"{inset.get('line_spacing_source', '?')}")
        cube = product.get("cube", {})
        worst = cube.get("worst_over_range_band")
        if worst:
            lines.append(
                f"- Worst over-range band: {worst.get('band', '?')} "
                f"({worst.get('wavelength_nm', '?')} nm) at "
                f"{_pct(worst.get('over_range_pct'))}")
        if product.get("constant_bands"):
            lines.append(
                f"- Constant bands: {product['constant_bands']}")
        lines.append("")

    lines += figure_embeds(report.get("artifacts", []), qc_data_dir)
    stale = report.get("staleness", {})
    for label, entry in stale.items():
        if isinstance(entry, dict) and entry.get("path"):
            lines += [
                f"_Computed against {pathlib.Path(entry['path']).name} "
                f"(mtime {_cell(entry.get('mtime_utc'))[:19]})_", ""]
    lines += _config_line(report.get("config"))
    return lines


# ==================================================================================
# ========== Small shared internals ==========
# ==================================================================================
def _section_header(report: Dict[str, Any], subject: str) -> List[str]:
    """Render a section's heading + provenance line (plan §5 skeleton).

    Parameters
    ----------
    report : dict
        The contract detail-report dict.
    subject : str
        Section subject, e.g. ``"Panel spectra"``.

    Returns
    -------
    list of str
        Markdown lines.
    """
    script = report.get("script", {})
    prefix = str(script.get("name", "QC?")).split("_")[0]
    status = report.get("status", "not_evaluated")
    return [
        f"## {prefix} — {subject}", "",
        f"**Status: {status_glyph(status)} {status}** · "
        f"{script.get('name', '?')} {script.get('version', '?')} · "
        f"generated {_cell(report.get('generated_utc'))[:19]}", "",
    ]


# ==================================================================================
def _config_line(config: Optional[Dict[str, Any]]) -> List[str]:
    """Render the thresholds-config provenance line.

    Parameters
    ----------
    config : dict or None
        A ``{path, sha256}`` config snapshot (or None / other shapes).

    Returns
    -------
    list of str
        Markdown lines (empty when no usable snapshot).
    """
    if not isinstance(config, dict) or not config.get("path"):
        return []
    sha = str(config.get("sha256") or "")[:12]
    return [f"_Thresholds: {config['path']}"
            + (f" (sha256 {sha})" if sha else "") + "_", ""]


# ==================================================================================
def _load_summary(
        qc_data_dir: pathlib.Path,
        script_name: str,
    ) -> Optional[Dict[str, Any]]:
    """Load a script's contract summary YAML if present.

    Parameters
    ----------
    qc_data_dir : pathlib.Path
        The run's ``QC_data/`` folder.
    script_name : str
        Contract script name.

    Returns
    -------
    dict or None
        The parsed summary, or None when absent/unreadable.
    """
    summary_path, _ = report_paths(qc_data_dir, script_name)
    if not summary_path.is_file():
        return None
    try:
        with open(summary_path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as err:
        warn.warn(f"Could not read {summary_path}: {err}")
        return None


# ==================================================================================
def _recorded_gpro_mtime(
        qc_data_dir: pathlib.Path,
        script_name: str,
    ) -> Optional[str]:
    """Return the gpro mtime a script's detail JSON recorded, if any.

    JSON-vs-JSON freshness only (plan §4) — the bundle itself is never
    stat-ed, so assembly behaves identically offline.

    Parameters
    ----------
    qc_data_dir : pathlib.Path
        The run's ``QC_data/`` folder.
    script_name : str
        Contract script name.

    Returns
    -------
    str or None
        The recorded ``staleness.gpro_mtime_utc`` (QC01 today), else None.
    """
    _, detail_path = report_paths(qc_data_dir, script_name)
    if not detail_path.is_file():
        return None
    try:
        with open(detail_path, "r", encoding="utf-8") as fh:
            detail = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    stale = detail.get("staleness")
    if isinstance(stale, dict):
        value = stale.get("gpro_mtime_utc")
        return str(value) if value else None
    return None


# ==================================================================================
def _write_text_atomic(path: pathlib.Path, text: str) -> None:
    """Write text to a file atomically (``.tmp`` + ``os.replace``).

    Parameters
    ----------
    path : pathlib.Path
        Destination file.
    text : str
        Full file content.

    Returns
    -------
    None
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# ==================================================================================
def _cell(value: Any) -> str:
    """Format a value for a markdown table cell (None → empty string).

    Parameters
    ----------
    value : Any
        Raw value.

    Returns
    -------
    str
        Display string.
    """
    return "" if value is None else str(value)


# ==================================================================================
def _pct(value: Any) -> str:
    """Format a percentage value with 3 significant decimals.

    Parameters
    ----------
    value : Any
        Numeric percentage (already 0–100 scaled) or None.

    Returns
    -------
    str
        e.g. ``"0.351 %"`` (or ``"?"`` when None/non-numeric).
    """
    try:
        return f"{float(value):.3f} %"
    except (TypeError, ValueError):
        return "?"
