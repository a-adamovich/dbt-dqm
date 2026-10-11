"""pandas settings shared by the review app's DataFrames."""

from __future__ import annotations

import pandas as pd


def configure_pandas() -> None:
    """Keep object-backed strings for the app's frames.

    pandas 3 infers Arrow-backed strings by default. At the 50,000-issue cache boundary that
    raised the sync path's peak memory by about 70 MiB (median 636 vs 569 MiB) for no benefit
    here, so the app keeps pandas 2's object strings (docs/scale.md). Missing values are still
    handled either way (display.is_missing).
    """
    try:
        pd.set_option("future.infer_string", False)
    except (KeyError, pd.errors.OptionError):  # an older or newer pandas without the option
        pass
