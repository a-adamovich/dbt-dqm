import json

from dbt_dqm_app.config import load_config
from dbt_dqm_app.warehouse import _relation_parts


def _project(tmp_path):
    project = tmp_path / "project"
    profiles = tmp_path / "profiles"
    project.mkdir()
    profiles.mkdir()
    (project / "dbt_project.yml").write_text("name: consumer\nprofile: consumer_profile\n")
    return project, profiles


def test_postgres_profile_renders_dbt_env_var(monkeypatch, tmp_path):
    project, profiles = _project(tmp_path)
    monkeypatch.setenv("DQM_TEST_PASSWORD", "secret-from-environment")
    (profiles / "profiles.yml").write_text(
        """
consumer_profile:
  outputs:
    dev:
      type: postgres
      host: localhost
      port: 5432
      user: reviewer
      pass: "{{ env_var('DQM_TEST_PASSWORD') }}"
      dbname: analytics
      schema: quality
"""
    )

    config = load_config(project, profiles, "dev")

    assert config.adapter_type == "postgres"
    assert config.password == "secret-from-environment"
    assert config.dataset == "quality"


def test_manifest_relation_wins_over_manually_derived_schema(tmp_path):
    project, profiles = _project(tmp_path)
    (profiles / "profiles.yml").write_text(
        """
consumer_profile:
  outputs:
    dev:
      type: postgres
      host: localhost
      user: reviewer
      pass: password
      dbname: analytics
      schema: quality
"""
    )
    target = project / "target"
    target.mkdir()
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "nodes": {
                    "model.dbt_dqm.dqm_all_issues": {
                        "database": "analytics",
                        "schema": "custom_governance",
                        "alias": "renamed_issue_history",
                    }
                }
            }
        )
    )

    config = load_config(project, profiles, "dev")

    assert _relation_parts(config, "dqm_all_issues") == (
        "analytics",
        "custom_governance",
        "renamed_issue_history",
    )
