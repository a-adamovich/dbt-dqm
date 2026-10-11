"""The review app keeps object-backed strings (Arrow strings cost ~70 MiB at the cache boundary)."""

import pandas as pd

from dbt_dqm_app.frames import configure_pandas


def test_app_frames_use_object_strings():
    configure_pandas()
    frame = pd.DataFrame({"notes": ["checked", None]})
    assert frame["notes"].dtype == object
    assert frame["notes"].iloc[1] is None


def test_streamlit_app_configures_pandas_on_import():
    import dbt_dqm_app.streamlit_app  # noqa: F401  (import applies the setting)

    assert pd.get_option("future.infer_string") is False
