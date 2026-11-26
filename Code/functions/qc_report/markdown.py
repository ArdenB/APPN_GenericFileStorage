"""Markdown section fragments + QC_report.md assembly for the QC scripts.

Implements the QC report plan (development-master repo, parent plan §7
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
import re
import warnings as warn
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yaml

import Code.functions.core_functions as cf
from Code.functions.qc_report.quicklook import is_quicklook
from Code.functions.qc_report.reader import read_report
from Code.functions.qc_report.report import report_paths


# ==================================================================================
def update_qc_report(
        qc_data_dir: pathlib.Path,
        report: Dict[str, Any],
        force_pdf: bool = False,
    ) -> Optional[pathlib.Path]:
    """Render the calling script's section fragment, then reassemble
    ``QC_data/QC_report.md``.

    Step 1 — dispatch on ``report["script"]["name"]`` to that script's
    section renderer (current schema only: the dict the caller just passed
    to ``write_report``) and atomically write
    ``QC_data/<script>/<script>_section.md``.
    Step 2 — assemble ``QC_report.md``: header + overview table + the four
    fragments in QC00→QC03 order (stubs where absent) + footer.
    Step 3 — PDF (plan v1.4 §3.1): ``QC_report.pdf`` is written when all
    four scripts have run and none is stale, or when ``force_pdf``; an
    older PDF is deleted when the run is no longer complete and current.

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
    force_pdf : bool
        Write the PDF even when the run is incomplete or stale (the QC
        scripts' ``--pdf`` flag).

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
        _write_text_atomic(
            _fragment_path(qc_data_dir, script_name, "provenance"),
            "\n".join(_provenance(report, qc_data_dir)) + "\n")
        report_path = qc_data_dir / "QC_report.md"
        _write_text_atomic(report_path, _assemble(qc_data_dir, report))
    except Exception as err:  # never gate the calling QC script
        warn.warn(f"QC_report.md render failed ({err!r}); the contract "
                  "report is unaffected.")
        return None
    if force_pdf or _run_complete(qc_data_dir):
        export_qc_pdf(qc_data_dir)
    else:
        (qc_data_dir / "QC_report.pdf").unlink(missing_ok=True)
    return report_path


# ==================================================================================
def export_qc_pdf(qc_data_dir: pathlib.Path) -> Optional[pathlib.Path]:
    """Convert the run's ``QC_report.md`` to ``QC_report.pdf``.

    Markdown → HTML (python-markdown) → PDF (weasyprint) with a print
    stylesheet. Status emoji become coloured symbols (no emoji font
    needed) and local file links become plain text (they cannot resolve
    on a client's machine). Used by :func:`update_qc_report` and by the
    QC scripts' ``--pdf`` flag on cached runs. Never raises.

    Parameters
    ----------
    qc_data_dir : pathlib.Path
        The run's ``T1_proc/QC_data/`` folder.

    Returns
    -------
    pathlib.Path or None
        The PDF path, or None when there is no report or conversion failed.
    """
    qc_data_dir = pathlib.Path(qc_data_dir)
    md_path = qc_data_dir / "QC_report.md"
    pdf_path = qc_data_dir / "QC_report.pdf"
    try:
        import markdown
        import weasyprint

        body = markdown.markdown(md_path.read_text(encoding="utf-8"),
                                 extensions=["tables"])
        html = f"<html><head><meta charset='utf-8'><style>{_pdf_css()}" \
               f"</style></head><body>{_pdf_body(body)}</body></html>"
        tmp = pdf_path.with_suffix(".pdf.tmp")
        weasyprint.HTML(string=html, base_url=str(qc_data_dir)).write_pdf(
            str(tmp))
        os.replace(tmp, pdf_path)
        return pdf_path
    except Exception as err:  # never gate the calling QC script
        warn.warn(f"QC_report.pdf export failed ({err!r}); QC_report.md is "
                  "unaffected.")
        return None


# ==================================================================================
def _run_complete(qc_data_dir: pathlib.Path) -> bool:
    """Return True when all four scripts have run and none is stale.

    ponytail: QC00/QC02 record no gpro identity, so their Freshness is
    "—" and counts as current here; only a detected ``stale?`` blocks.
    Upgrade path: record ``staleness.gpro_*`` in QC00/QC02 too.

    Parameters
    ----------
    qc_data_dir : pathlib.Path
        The run's ``QC_data/`` folder.

    Returns
    -------
    bool
        Whether the automatic PDF should be written.
    """
    summaries = {name: _load_summary(qc_data_dir, name)
                 for name, _ in _contract_sections()}
    if any(summary is None for summary in summaries.values()):
        return False
    return "stale?" not in _freshness(qc_data_dir, summaries).values()


# ==================================================================================
def _pdf_body(body: str) -> str:
    """Adapt the report HTML for print (status symbols, local links).

    Parameters
    ----------
    body : str
        HTML converted from ``QC_report.md``.

    Returns
    -------
    str
        Print-ready HTML body.
    """
    symbols = {
        "✅": '<span class="st pass">●</span>',
        "⚠️": '<span class="st warn">▲</span>',
        "⚠": '<span class="st warn">▲</span>',
        "❌": '<span class="st fail">✖</span>',
        "➖": '<span class="st na">–</span>',
    }
    for emoji, span in symbols.items():
        body = body.replace(emoji, span)
    return re.sub(r'<a href="(?!https?:)[^"]*">(.*?)</a>', r"\1", body)


# ==================================================================================
def _pdf_css() -> str:
    """Return the print stylesheet for ``QC_report.pdf``.

    Returns
    -------
    str
        CSS text (A4, compact tables, two-up At-a-glance grid).
    """
    return """
    @page { size: A4; margin: 14mm 12mm;
            @bottom-right { content: counter(page) " / " counter(pages);
                            font-size: 8pt; color: #666; } }
    body { font-family: 'DejaVu Sans', sans-serif; font-size: 9.5pt;
           line-height: 1.35; }
    h1 { font-size: 15pt; margin: 0 0 4pt; }
    h2 { font-size: 12.5pt; border-bottom: 1px solid #bbb;
         margin-top: 16pt; break-after: avoid; }
    h3 { font-size: 10.5pt; break-after: avoid; }
    table { border-collapse: collapse; width: 100%; font-size: 8pt;
            margin: 4pt 0 8pt; break-inside: auto; }
    th, td { border: 1px solid #ccc; padding: 2pt 4pt; text-align: left;
             vertical-align: top; word-break: break-word; }
    th { background: #f0f0f0; }
    tr { break-inside: avoid; }
    img { max-width: 100%; max-height: 120mm; break-inside: avoid; }
    p.glance img { width: 49%; height: 62mm; object-fit: contain;
                   margin: 0 0.5% 2mm 0; vertical-align: top; }
    .st { font-weight: bold; }
    .pass { color: #2e7d32; } .warn { color: #e69500; }
    .fail { color: #c62828; } .na { color: #777; }
    em { color: #444; }
    """


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
def status_label(status: Optional[str]) -> str:
    """Return the display label (glyph + one word) for any status.

    Both vocabulary levels collapse to one client word, so a status
    never renders twice ("✅ pass") or in second-level jargon
    ("✅ acceptable") — QC report plan v1.4 §5.0 item 1.

    Parameters
    ----------
    status : str or None
        Any status from the shared vocabulary (both levels accepted).

    Returns
    -------
    str
        e.g. ``"✅ Pass"``, ``"⚠️ Warn"``, ``"➖ Not checked"``.
    """
    words = {
        "pass": "Pass", "good": "Pass", "acceptable": "Pass",
        "warn": "Warn", "warning": "Warn",
        "fail": "Fail",
        "not_evaluated": "Not evaluated", "not_checked": "Not checked",
    }
    return f"{status_glyph(status)} {words.get(str(status), 'Not evaluated')}"


# ==================================================================================
def _wording() -> Dict[str, str]:
    """Return every client-facing context string in the report.

    Single source for the operator-authored wording (finalised
    2026-09-04; drafted in a temporary QC_REPORT_WORDING.md working doc
    that was folded back here and deleted uncommitted — this function
    is the canonical record).

    Returns
    -------
    dict
        Key → plain-text string (renderers add italics/bolding).
    """
    return {
        "report_intro": (
            "This report summarises four checks for one sensor run: "
            "georeferencing (QC00), flight execution (QC01), reflectance "
            "calibration (QC02) and raster validity (QC03). The table "
            "below shows the overall result."),
        "legend": (
            "✅ Pass. ⚠️ Warning: usable with caution. ❌ Fail: review "
            "before use. ➖ Not evaluated. Advisory checks do not affect "
            "the overall result."),
        "freshness_note": (
            "Freshness: \"stale?\" means the check used an older sensor "
            "bundle. Re-run it for current results."),
        "glance_heading": "At a glance",
        "caption_quicklook_cir": (
            "false-colour image (healthy vegetation shows red)"),
        "caption_quicklook_dsm": (
            "surface height from LiDAR (blue low, yellow high; white is "
            "no data)"),
        "caption_quicklook_trajectory": (
            "flight lines coloured by height"),
        "appendix_intro": (
            "Operator detail: script versions, the data each check read, "
            "threshold files and output files."),
        "blurb_QC00_GCPCheck": (
            "Compares surveyed ground control points (GCPs) with their "
            "image positions. Distances are in metres. If this check "
            "fails, reprocess the georeferencing and re-run all checks."),
        "blurb_QC01_FlightCheck": (
            "Checks bundle completeness, altitude, image overlap "
            "(sidelap), frame rate, pixel size (GSD) and sun position "
            "against the APPN acquisition specification."),
        "blurb_QC02_SpectralCheck": (
            "Compares image spectra with lab measurements for each "
            "reference panel and camera range (VNIR and SWIR). "
            "Differences are reflectance percentage points (pp)."),
        "blurb_QC03_RasterCheck": (
            "Checks each raster band for file errors, zero or no-data "
            "pixels, saturation, and dead or constant values."),
        "role_val": (
            "Validation (VAL) panels were not used for calibration, so "
            "they provide the best independent test of reflectance."),
        "role_elm": (
            "ELM panels were used for calibration. Large differences "
            "indicate a problem."),
        "caption_displacements": (
            "Each arrow shows the surveyed and image positions of one "
            "GCP."),
        "caption_sun_geometry": (
            "Sun position relative to the flight lines."),
        "caption_agl_profiles": (
            "Altitude above ground level (AGL) along each flight line."),
        "caption_dhr_overlay": (
            "Image spectra compared with each panel's lab reference."),
        "caption_dhr_delta": (
            "Measured minus expected reflectance by band. Values near "
            "zero are ideal; shading marks excluded atmospheric water "
            "bands."),
        "caption_band_percentiles": (
            "Reflectance range by band. Sudden drops or spikes flag "
            "suspect bands."),
        "caption_band_zero_fraction": (
            "Percentage of zero or no-data pixels by band."),
        "caption_band_over_range": (
            "Percentage of saturated pixels by band."),
        # +++++ check-family labels for the headline (DRAFT 2026-11-26,
        # +++++ awaiting operator wording pass) +++++
        "check_gcp_2d": "GCP position error",
        "check_height_bias": "GCP height bias",
        "check_planar_bias": "GCP planar bias",
        "check_graw_present": "Raw data bundle",
        "check_dark_reference": "Dark reference",
        "check_panels_present": "Reference panels",
        "check_reflectance_product": "Reflectance product",
        "check_flightcal_spec": "Flight spec file",
        "check_gsd": "Pixel size (GSD)",
        "check_frame_rate": "Frame rate",
        "check_sidelap": "Image overlap",
        "check_oversampling": "Oversampling",
        "check_time_to_solar_noon": "Time from solar noon",
        "check_appn_compliant": "APPN spec compliance",
        "check_reference_equivalence": "Panel reference match",
        "check_nodata_zero": "Panel no-data",
        "check_homogeneity": "Panel uniformity",
        "check_panel_set_pinned": "Panel set",
        "check_dhr_bias": "Panel reflectance bias",
        "check_header_bin_integrity": "File integrity",
        "check_zeros_in_footprint": "Zero pixels in footprint",
        "check_over_range": "Saturated pixels",
        "check_negative": "Negative values",
        "check_nan_inf": "NaN/Inf values",
        "check_dead_bands": "Dead bands",
        "check_capture_extent": "Capture extent",
        "check_zero_edge_band": "Zero edge band",
        "check_dropout_in_roi": "Data dropout in capture area",
        "check_data_outside_bbox": "Data outside capture area",
    }


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
        label = status_label(status)
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
        captions: Optional[Dict[str, str]] = None,
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
    captions : dict, optional
        Figure-stem *suffix* → caption text; the first matching caption
        renders as an italic line under the image.

    Returns
    -------
    list of str
        Markdown lines (empty when there are no figures).
    """
    lines: List[str] = []
    for art in artifacts:
        if not str(art).lower().endswith(".png") or is_quicklook(art):
            continue
        rel = str(art).replace(os.sep, "/")
        title = pathlib.Path(rel).stem
        if (qc_data_dir / rel).is_file():
            lines += [f"![{title}]({rel})", ""]
            caption = next((text for suffix, text in (captions or {}).items()
                            if title.endswith(suffix)), None)
            if caption:
                lines += [f"_{caption}_", ""]
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
    lines = ["**Output files:**", ""]
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

    Header, descriptive headline + overview table from the four summary
    YAMLs, the four section fragments (stubs where absent — plan §6),
    the provenance appendix from the ``*_provenance.md`` fragments, then
    the footer. Fragments embed as raw bytes; nothing is parsed or
    re-rendered.

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

    wording = _wording()
    summaries = {name: _load_summary(qc_data_dir, name)
                 for name, _ in _contract_sections()}
    freshness = _freshness(qc_data_dir, summaries)
    lines: List[str] = [f"# QC report — {identity}", "",
                        "_" + " · ".join(meta_bits) + "_", "",
                        wording["report_intro"], ""]
    lines += _headline(summaries, freshness)
    lines += _overview_table(summaries, freshness)
    lines += [f"_{wording['legend']}_", "",
              f"_{wording['freshness_note']}_", ""]
    lines += _at_a_glance(qc_data_dir, summaries)
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
    appendix = [frag for name, _ in _contract_sections()
                if (frag := _fragment_path(
                    qc_data_dir, name, "provenance")).is_file()]
    if appendix:
        lines += ["", "---", "", "## Appendix — provenance", "",
                  f"_{wording['appendix_intro']}_"]
        for frag in appendix:
            try:
                lines += ["", frag.read_text(encoding="utf-8").rstrip()]
            except OSError as err:
                warn.warn(f"Could not read fragment {frag}: {err}")
    trigger = report.get("script", {})
    lines += ["", "---", "",
              f"_Assembled by qc_report (schema "
              f"{report.get('schema_version', '?')}) on "
              f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}; "
              f"triggered by {trigger.get('name', '?')} "
              f"{trigger.get('version', '?')}._", ""]
    return "\n".join(lines)


# ==================================================================================
def _at_a_glance(
        qc_data_dir: pathlib.Path,
        summaries: Dict[str, Optional[Dict[str, Any]]],
    ) -> List[str]:
    """Render the At-a-glance quicklook grid (plan v1.4 §4.2).

    Places the ``quicklook_*.png`` artifacts listed in the summaries (CIR,
    then DSM, then trajectory) side by side as raw HTML so both the
    markdown preview and the PDF show them two per row. Missing files are
    left out; no quicklooks means no section.

    Parameters
    ----------
    qc_data_dir : pathlib.Path
        The run's ``QC_data/`` folder (existence checks only).
    summaries : dict
        Script name → summary dict (None when not yet run).

    Returns
    -------
    list of str
        Markdown lines (empty when there is nothing to show).
    """
    order = ("quicklook_cir", "quicklook_dsm", "quicklook_trajectory")
    found = [str(a).replace(os.sep, "/")
             for summary in summaries.values() if summary
             for a in summary.get("artifacts") or []
             if is_quicklook(a) and (qc_data_dir / str(a)).is_file()]
    found.sort(key=lambda a: next(
        (i for i, k in enumerate(order)
         if pathlib.PurePosixPath(a).stem.startswith(k)), len(order)))
    if not found:
        return []
    wording = _wording()
    captions = []
    for art in found:
        stem = pathlib.PurePosixPath(art).stem
        key = next((k for k in order if stem.startswith(k)), None)
        text = wording.get(f"caption_{key}", stem) if key else stem
        label = stem[len("quicklook_cir_"):] if key == "quicklook_cir" else ""
        captions.append(f"{text} ({label})" if label else text)
    imgs = "".join(f'<img src="{art}" alt="{pathlib.PurePosixPath(art).stem}"'
                   f' width="49%">' for art in found)
    return [f"## {wording['glance_heading']}", "",
            f'<p class="glance">{imgs}</p>', "",
            "_In order: " + "; ".join(captions) + "._", ""]


# ==================================================================================
def _overview_table(
        summaries: Dict[str, Optional[Dict[str, Any]]],
        freshness: Dict[str, str],
    ) -> List[str]:
    """Render the header overview table from the four summary YAMLs.

    One row per contract script in run order: status label, script
    version, ``generated_utc`` and the Freshness flag.

    Parameters
    ----------
    summaries : dict
        Script name → summary dict (None when not yet run), run order.
    freshness : dict
        Script name → Freshness flag from :func:`_freshness`.

    Returns
    -------
    list of str
        Markdown lines for the overview table.
    """
    rows = []
    for name, summary in summaries.items():
        if summary is None:
            rows.append({"Script": name, "Status": "— not yet run",
                         "Version": "—", "Generated": "—",
                         "Freshness": "—"})
            continue
        rows.append({
            "Script": name,
            "Status": status_label(summary.get("status", "not_evaluated")),
            "Version": summary.get("script", {}).get("version", "—"),
            "Generated": _fmt_utc(summary.get("generated_utc")),
            "Freshness": freshness[name],
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
        kind: str = "section",
    ) -> pathlib.Path:
    """Return a script's fragment path.

    Parameters
    ----------
    qc_data_dir : pathlib.Path
        The run's ``QC_data/`` folder.
    script_name : str
        Contract script name.
    kind : str
        ``"section"`` (report body) or ``"provenance"`` (appendix block).

    Returns
    -------
    pathlib.Path
        ``QC_data/<script>/<script>_<kind>.md``.
    """
    return qc_data_dir / script_name / f"{script_name}_{kind}.md"


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
    lines += [f"_{_wording()['blurb_QC00_GCPCheck']}_", ""]
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

    lines += figure_embeds(
        report.get("artifacts", []), qc_data_dir,
        captions={"_displacements": _wording()["caption_displacements"]})
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
    lines += [f"_{_wording()['blurb_QC01_FlightCheck']}_", ""]
    checks = report.get("checks", {})
    integrity_names = ("graw_present", "dark_reference", "panels_present",
                       "reflectance_product_vnir",
                       "reflectance_product_swir", "flightcal_spec")
    integrity = {k: v for k, v in checks.items() if k in integrity_names}
    spec = _merge_sidelap_rows(
        {k: v for k, v in checks.items() if k not in integrity_names})
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
            + _fmt_time_range(acquisition.get("first_line_start_utc"),
                              acquisition.get("last_line_end_utc")))
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

    wording = _wording()
    lines += figure_embeds(
        report.get("artifacts", []), qc_data_dir,
        captions={"_sun_geometry": wording["caption_sun_geometry"],
                  "_agl_profiles": wording["caption_agl_profiles"]})
    return lines


# ==================================================================================
def _merge_sidelap_rows(
        checks: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
    """Collapse agreeing calculator/fieldbook sidelap rows into one.

    QC01 grades one sidelap range against two thresholds; when both give
    the same status and value the pair renders as a single ``sidelap_{tag}``
    row (plan v1.4 §5.2 item 5). Disagreeing pairs stay as two rows.

    Parameters
    ----------
    checks : dict
        QC01 spec checks, in report order.

    Returns
    -------
    dict
        Checks with agreeing pairs merged at the calculator row's position.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for name, chk in checks.items():
        if name.startswith("sidelap_") and name.endswith("_calculator"):
            stem = name[:-len("_calculator")]
            twin = checks.get(f"{stem}_fieldbook")
            if twin is not None and all(
                    twin.get(k) == chk.get(k) for k in ("status", "value")):
                note = "; ".join(filter(None, (
                    "calculator and fieldbook agree", chk.get("note"))))
                merged[stem] = {**chk, "note": note}
                continue
        if name.startswith("sidelap_") and name.endswith("_fieldbook") \
                and name[:-len("_fieldbook")] in merged:
            continue
        merged[name] = chk
    return merged


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
    lines += [f"_{_wording()['blurb_QC02_SpectralCheck']}_", ""]
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

    lines += _qc02_figure_blocks(report, qc_data_dir)
    return lines


# ==================================================================================
def _qc02_figure_blocks(
        report: Dict[str, Any],
        qc_data_dir: pathlib.Path,
    ) -> List[str]:
    """Render QC02 figures grouped per target, in reporting priority.

    Operator rules: one ``###`` heading per target with both EM regions
    under it (2026-09-04) and a one-line role note (VAL vs ELM); the
    2-panel VAL sets are the headline QC02 result — their block renders
    first, then the ELM targets, then the remaining (4-panel) VAL sets
    (2026-09-03). Figures whose target cannot be matched go last,
    ungrouped. Artifact order is preserved within each target, so the
    overlay/delta pairing and region order survive.

    Parameters
    ----------
    report : dict
        The QC02 contract detail-report dict (targets + panel counts
        come from ``spectral_report``).
    qc_data_dir : pathlib.Path
        The run's ``QC_data/`` folder (figure existence checks).

    Returns
    -------
    list of str
        Markdown lines (empty when there are no figures).
    """
    targets = (report.get("spectral_report") or {}).get("targets", {})
    n_panels = {
        target: max((len(block.get("panels", {}))
                     for block in regions.values()), default=0)
        for target, regions in targets.items()}

    def target_priority(target: str) -> int:
        if "VAL" in target.upper() and n_panels[target] == 2:
            return 0
        if "ELM" in target.upper():
            return 1
        return 2

    pngs = [str(a) for a in report.get("artifacts", [])
            if str(a).lower().endswith(".png")]
    by_target: Dict[Optional[str], List[str]] = {}
    for art in pngs:
        stem = pathlib.Path(art).stem
        match = max((t for t in n_panels if stem.startswith(t)),
                    key=len, default=None)
        by_target.setdefault(match, []).append(art)

    wording = _wording()
    captions = {"_dhr_overlay": wording["caption_dhr_overlay"],
                "_dhr_delta": wording["caption_dhr_delta"]}
    lines: List[str] = []
    for target in sorted(n_panels, key=target_priority):
        figs = by_target.get(target)
        if not figs:
            continue
        lines += [f"### {target}", ""]
        if "ELM" in target.upper():
            lines += [f"_{wording['role_elm']}_", ""]
        elif "VAL" in target.upper():
            lines += [f"_{wording['role_val']}_", ""]
        lines += figure_embeds(figs, qc_data_dir, captions=captions)
    lines += figure_embeds(by_target.get(None, []), qc_data_dir,
                           captions=captions)
    return lines


# ==================================================================================
def _qc03_figure_order(artifacts: List[str]) -> List[str]:
    """Order QC03 figure artifacts for embedding.

    The percentile envelope (the data-health overview) leads, then the
    zero-fraction and over-range fraction figures (operator order,
    2026-09-04). The sort is stable, so products keep their recorded
    order within each figure type; non-figure artifacts pass through.

    Parameters
    ----------
    artifacts : list of str
        The report's artifact paths.

    Returns
    -------
    list of str
        Artifacts with the ``.png`` entries reordered.
    """
    def priority(art: str) -> int:
        stem = pathlib.Path(str(art)).stem
        for rank, suffix in enumerate(("_band_percentiles",
                                       "_band_zero_fraction",
                                       "_band_over_range")):
            if stem.endswith(suffix):
                return rank
        return 3

    pngs = sorted((str(a) for a in artifacts
                   if str(a).lower().endswith(".png")), key=priority)
    others = [str(a) for a in artifacts
              if not str(a).lower().endswith(".png")]
    return pngs + others


# ==================================================================================
def _section_qc03(
        report: Dict[str, Any],
        qc_data_dir: pathlib.Path,
    ) -> List[str]:
    """Render the QC03 (raster data validity) section fragment.

    Pivots the per-product check explosion into a check-family × product
    matrix, then one detail block per product (dims, integrity, zone-split
    evidence), figure embeds (per-band figures since QC03 v1.8; the
    percentile envelope leads — operator order, 2026-09-04) and the
    staleness + config lines.

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
    lines += [f"_{_wording()['blurb_QC03_RasterCheck']}_", ""]
    checks = report.get("checks", {})
    labels = list(report.get("products", {}))
    families = ("header_bin_integrity", "zeros_in_footprint", "over_range",
                "negative", "nan_inf", "dead_bands", "capture_extent",
                "zero_edge_band", "dropout_in_roi", "data_outside_bbox")
    if labels:
        rows = []
        for family in families:
            row: Dict[str, Any] = {"Check": family}
            for label in labels:
                chk = checks.get(f"{family}_{label}")
                if chk is None:
                    row[label] = "—"
                elif _cell(chk.get("value")):
                    row[label] = (f"{status_glyph(chk.get('status'))} "
                                  f"{chk['value']}")
                elif chk.get("status") == "not_checked" and chk.get("note"):
                    row[label] = f"➖ n/a — {chk['note']}"
                else:
                    row[label] = status_label(chk.get("status"))
            rows.append(row)
        lines += [cf.markdown_table(pd.DataFrame(rows)), ""]
    else:
        lines += checks_table(checks)

    wording = _wording()
    captions = {
        "_band_percentiles": wording["caption_band_percentiles"],
        "_band_zero_fraction": wording["caption_band_zero_fraction"],
        "_band_over_range": wording["caption_band_over_range"]}
    ordered = _qc03_figure_order(report.get("artifacts", []))
    figures_by_label: Dict[Optional[str], List[str]] = {}
    for art in ordered:
        if not str(art).lower().endswith(".png"):
            continue
        stem = pathlib.Path(str(art)).stem
        match = next((lbl for lbl in labels
                      if stem.startswith(f"{lbl}_")), None)
        figures_by_label.setdefault(match, []).append(str(art))

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
            inset = zones.get("inset") or {}
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
        lines += figure_embeds(figures_by_label.get(label, []), qc_data_dir,
                               captions=captions)

    lines += figure_embeds(figures_by_label.get(None, []), qc_data_dir,
                           captions=captions)
    return lines


# ==================================================================================
# ========== Small shared internals ==========
# ==================================================================================
def _section_header(report: Dict[str, Any], subject: str) -> List[str]:
    """Render a section's heading + status line (plan §5 skeleton).

    Script version / generated time live in the provenance appendix
    (plan v1.4 §4.4).

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
    prefix = str(report.get("script", {}).get("name", "QC?")).split("_")[0]
    status = report.get("status", "not_evaluated")
    return [f"## {prefix} — {subject}", "",
            f"**Status: {status_label(status)}**", ""]


# ==================================================================================
def _provenance(
        report: Dict[str, Any],
        qc_data_dir: pathlib.Path,
    ) -> List[str]:
    """Render one script's provenance-appendix block (plan v1.4 §4.4).

    Script version + generated time, the gpro / product identities from
    ``staleness``, threshold config(s) with hash, and the output files —
    operator detail kept out of the client-facing section body.

    Parameters
    ----------
    report : dict
        The contract detail-report dict.
    qc_data_dir : pathlib.Path
        The run's ``QC_data/`` folder (artifact existence checks).

    Returns
    -------
    list of str
        Markdown lines for the ``<script>_provenance.md`` fragment.
    """
    script = report.get("script", {})
    name = str(script.get("name", "QC?"))
    lines = [f"### {name}", "",
             f"- Script: {name} {script.get('version', '?')}, generated "
             f"{_fmt_utc(report.get('generated_utc'))}"]
    stale = report.get("staleness") or {}
    if stale.get("gpro_path"):
        lines.append(
            f"- Computed against: `{pathlib.Path(stale['gpro_path']).name}` "
            f"(modified {_fmt_utc(stale.get('gpro_mtime_utc'))})")
    for label, entry in stale.items():
        if isinstance(entry, dict) and entry.get("path"):
            lines.append(
                f"- Product {label}: `{pathlib.Path(entry['path']).name}` "
                f"(modified {_fmt_utc(entry.get('mtime_utc'))})")
    config = report.get("config") or {}
    for cfg in (config, config.get("spectral_limits")):
        if isinstance(cfg, dict) and cfg.get("path"):
            sha = str(cfg.get("sha256") or "")[:12]
            lines.append(f"- Thresholds: {cfg['path']}"
                         + (f" (sha256 {sha})" if sha else ""))
    if config.get("table_schema_version") is not None:
        lines.append(
            f"- Spectra table schema: {config['table_schema_version']}")
    lines.append("")
    return lines + artifact_links(report.get("artifacts", []), qc_data_dir)


# ==================================================================================
def _freshness(
        qc_data_dir: pathlib.Path,
        summaries: Dict[str, Optional[Dict[str, Any]]],
    ) -> Dict[str, str]:
    """Return each contract script's Freshness flag (plan §4).

    JSON-vs-JSON only: a script's recorded gpro mtime is compared with
    the newest gpro mtime recorded by any sibling; the bundle itself is
    never stat-ed.

    Parameters
    ----------
    qc_data_dir : pathlib.Path
        The run's ``QC_data/`` folder.
    summaries : dict
        Script name → summary dict (None when not yet run).

    Returns
    -------
    dict
        Script name → ``"ok"``, ``"stale?"`` or ``"—"`` (not run / no
        recorded gpro identity).
    """
    mtimes = {name: _recorded_gpro_mtime(qc_data_dir, name)
              if summaries.get(name) is not None else None
              for name in summaries}
    known = [m for m in mtimes.values() if m]
    newest = max(known) if known else None
    return {name: "—" if mtime is None or newest is None
            else ("ok" if mtime == newest else "stale?")
            for name, mtime in mtimes.items()}


# ==================================================================================
def _headline(
        summaries: Dict[str, Optional[Dict[str, Any]]],
        freshness: Dict[str, str],
    ) -> List[str]:
    """Render the page-1 descriptive summary (plan v1.4 §4.1).

    One bullet per script that ran, naming its warning/failing checks
    (worst first, at most three plus a count) — advisory checks are left
    out, matching how they are left out of the run status. Not-run
    scripts are listed together. Deliberately **no** overall PASS/FAIL
    verdict until thresholds are calibrated.

    Parameters
    ----------
    summaries : dict
        Script name → summary dict (None when not yet run), run order.
    freshness : dict
        Script name → Freshness flag from :func:`_freshness`.

    Returns
    -------
    list of str
        Markdown lines.
    """
    subjects = dict(_contract_sections())
    ran = [name for name, summary in summaries.items() if summary]
    lines = [f"**Summary:** {len(ran)} of {len(summaries)} checks run.", ""]
    severity = {"fail": 0, "warn": 1, "warning": 1}
    for name in ran:
        summary = summaries[name] or {}
        prefix = name.split("_")[0]
        checks = _merge_sidelap_rows(summary.get("checks") or {})
        flagged = sorted(
            ((chk.get("status"), check) for check, chk in checks.items()
             if not chk.get("advisory") and chk.get("status") in severity),
            key=lambda pair: severity[pair[0]])
        issues = [f"{status_glyph(status)} {_check_label(check)} "
                  f"{_cell(checks[check].get('value'))}".rstrip()
                  for status, check in flagged]
        if issues:
            text = "; ".join(issues[:3])
            if len(issues) > 3:
                text += f"; and {len(issues) - 3} more"
        elif summary.get("status") == "pass":
            text = "all checks passed"
        else:
            text = "nothing graded"
        if freshness.get(name) == "stale?":
            text += " — **stale?** re-run for current results"
        lines.append(f"- {status_label(summary.get('status'))} — "
                     f"{prefix} {subjects[name]}: {text}")
    missing = [f"{name.split('_')[0]} {subjects[name]}"
               for name, summary in summaries.items() if not summary]
    if missing:
        lines.append(f"- ➖ Not yet run: {', '.join(missing)}")
    return lines + [""]


# ==================================================================================
def _check_label(check: str) -> str:
    """Return a client-facing label for a contract check name.

    The longest matching ``check_<family>`` key in :func:`_wording` gives
    the label; any remaining name parts (sensor/product/source) follow in
    brackets, e.g. ``sidelap_vnir_calculator`` →
    ``"Image overlap (vnir, calculator)"``.

    Parameters
    ----------
    check : str
        Contract check name.

    Returns
    -------
    str
        Display label (the raw name with spaces when no family matches).
    """
    families = sorted((key[len("check_"):] for key in _wording()
                       if key.startswith("check_")), key=len, reverse=True)
    family = next((fam for fam in families
                   if check == fam or check.startswith(f"{fam}_")), None)
    if family is None:
        return check.replace("_", " ")
    label = _wording()[f"check_{family}"]
    rest = [part for part in check[len(family):].split("_") if part]
    return f"{label} ({', '.join(rest)})" if rest else label


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
        The recorded ``staleness.gpro_mtime_utc`` (QC01; QC03 v2.5+),
        else None.
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
def _fmt_utc(value: Any) -> str:
    """Format a recorded timestamp as ``YYYY-MM-DD HH:MM UTC``.

    Plan v1.4 §5.0 item 3 (seconds/microseconds/offset dropped).
    ponytail: UTC only — site-local time waits on open question 5
    (timezone source); swap the ``tz_convert`` target when it lands.

    Parameters
    ----------
    value : Any
        ISO string / timestamp (naive values are taken as UTC) or None.

    Returns
    -------
    str
        Display string, ``"—"`` when None, the raw text if unparseable.
    """
    if value is None or value == "":
        return "—"
    stamp = _as_utc(value)
    return str(value) if stamp is None else stamp.strftime("%Y-%m-%d %H:%M UTC")


# ==================================================================================
def _as_utc(value: Any) -> Optional[pd.Timestamp]:
    """Parse a recorded timestamp to a UTC ``pd.Timestamp``.

    Parameters
    ----------
    value : Any
        ISO string / timestamp; naive values are taken as UTC.

    Returns
    -------
    pd.Timestamp or None
        None when missing or unparseable.
    """
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp):
        return None
    return (stamp.tz_localize("UTC") if stamp.tzinfo is None
            else stamp.tz_convert("UTC"))


# ==================================================================================
def _fmt_time_range(start: Any, end: Any) -> str:
    """Format a start/end pair as ``YYYY-MM-DD HH:MM–HH:MM UTC (N min)``.

    Parameters
    ----------
    start, end : Any
        Recorded timestamps (see :func:`_fmt_utc`).

    Returns
    -------
    str
        Compact range with duration; falls back to ``"{start} → {end}"``
        when either end is missing/unparseable or the range spans days.
    """
    t0, t1 = _as_utc(start), _as_utc(end)
    if t0 is None or t1 is None:
        return f"{_fmt_utc(start)} → {_fmt_utc(end)}"
    minutes = round((t1 - t0).total_seconds() / 60)
    first, last = _fmt_utc(t0), _fmt_utc(t1)
    if first[:10] != last[:10]:
        return f"{first} → {last} ({minutes} min)"
    return f"{first[:16]}–{last[11:16]} UTC ({minutes} min)"


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
