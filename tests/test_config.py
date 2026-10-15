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
    monkeypatch.setenv("DBT_TARGET_PATH", "environment artifacts")
    config = load_config(project, profiles, "dev", target_path="cli artifacts")
    assert config.target_path == project / "cli artifacts"
    assert config.dbt_cli_args[-2:] == ("--target-path", str(project / "cli artifacts"))


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


def test_postgres_profile_accepts_password_key_alias(tmp_path):
    project, profiles = _project(tmp_path)
    (profiles / "profiles.yml").write_text(
        """
consumer_profile:
  outputs:
    dev:
      type: postgres
      host: localhost
      port: 5432
      user: reviewer
      password: spelled-out
      dbname: analytics
      schema: quality
"""
    )

    assert load_config(project, profiles, "dev").password == "spelled-out"


def test_naming_vars_retain_yaml_dates_and_lists(tmp_path):
    from datetime import date

    import yaml
    project,profiles=_project(tmp_path)
    variables={'table_alias_prefix':'alice','as_of':date(2026,10,14),'labels':['a','b']}
    (profiles/'profiles.yml').write_text(yaml.safe_dump({'consumer_profile': {'target':'dev','outputs': {'dev': {'type':'postgres','host':'localhost','user':'reviewer','password':'test','dbname':'analytics','schema':'quality'}}}}))
    config=load_config(project,profiles,'dev',cli_vars=variables)
    assert yaml.safe_load(config.vars_yaml)==variables
