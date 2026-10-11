"""dbt failures map to the right reviewer-facing hint."""

import subprocess

from dbt_dqm_app.errors import classify

OK_PROFILE = "02:05:40    profiles.yml file [\x1b[32mOK found and valid\x1b[0m]\n"


def _failure(output):
    return classify(subprocess.CalledProcessError(1, ["dbt", "debug"], output=output, stderr=""))


def test_unsupported_dbt_version_is_not_reported_as_a_missing_profile():
    output = OK_PROFILE + (
        "02:05:40    dbt_project.yml file [\x1b[31mERROR invalid\x1b[0m]\n"
        "Runtime Error\n  This version of dbt is not supported with the 'demo' package.\n"
    )
    message = _failure(output).message
    assert "doesn't support the installed dbt version" in message
    assert "profile or target" not in message


def test_missing_profile_still_gets_the_profile_hint():
    output = "02:05:40    profiles.yml file [\x1b[31mERROR not found\x1b[0m]\n"
    assert "profile or target" in _failure(output).message
    output = "Runtime Error\n  Could not find profile named 'demo'\n"
    assert "profile or target" in _failure(output).message


def test_valid_profile_line_alone_gives_the_generic_message():
    assert _failure(OK_PROFILE + "Something else went wrong\n").message == "`dbt debug` failed."
