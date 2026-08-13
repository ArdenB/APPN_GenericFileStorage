"""Check whether derived output files are newer than their inputs."""

import pathlib
from typing import List


def outputs_up_to_date(
        outputs: List[pathlib.Path],
        inputs: List[pathlib.Path],
    ) -> bool:
    """Return True when every output exists and is newer than every input.

    Used for mtime-based caching: a processing step can be skipped when
    all of its outputs already exist on disk and none of its inputs have
    been modified since the oldest output was written.

    Parameters
    ----------
    outputs : list of pathlib.Path
        Files the processing step would produce.
    inputs : list of pathlib.Path
        Files the processing step reads.

    Returns
    -------
    bool
        False if any output is missing, any input is missing, or any
        input is newer than the oldest output; True otherwise.
    """
    out_mtimes = []
    for p in outputs:
        if not p.is_file():
            return False
        out_mtimes.append(p.stat().st_mtime)
    oldest_out = min(out_mtimes)
    for p in inputs:
        if not p.is_file():
            return False
        if p.stat().st_mtime > oldest_out:
            return False
    return True
