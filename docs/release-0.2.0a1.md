# 0.2.0a1 release preparation: blocked

Recorded 2026-10-11. This is not release approval or proof that publication completed.

- Baseline: `82a2917f2fd91bf64e00eadee4ec869b4a393ea9`.
- Production candidate tested: `d0385e685bc8581bda3bbe19170066a0576e3950`.
- Preparation branch: `codex/release-0.2.0a1`.
- [Draft preparation PR #60](https://github.com/a-adamovich/dbt-dqm/pull/60).
- [Candidate CI run 38181136207](https://github.com/a-adamovich/dbt-dqm/actions/runs/38181136207).

## Blocking decision

dbt-core 1.12.5's actual project schema rejects `version: 0.2.0a1` in `dbt_project.yml`:
`at path ['version']: '0.2.0a1' is not valid under any of the given schemas`.
Its lower-level `VersionSpecifier` accepts this spelling, so a parser-only check is insufficient.
The release regression test now validates the actual project schema as well.

The project schema accepts `0.2.0-a1`. The proposed resolution is that spelling for the dbt
project, retaining Python version `0.2.0a1` and tag `v0.2.0a1`, with normalized release version
comparisons. This requires confirmation because the approved plan specified literal equality.
No runtime patch or schema-validation bypass has been introduced.

**Smallest missing action:** confirm the compatible dbt spelling and normalized comparison.
Then update the declarations/checks/docs, freeze a new candidate, and run every remaining gate
against it. This candidate must not be merged or tagged.

## Results for the candidate

| Gate or check | Result | Evidence / limitation |
| --- | --- | --- |
| Locked dependency installation | Passed | `uv sync --locked --all-groups`; no lockfile dependency changes |
| Lint and workflow syntax | Passed | Ruff and actionlint on the release workflow |
| Unit suite | **Failed** | 194 collected: 181 passed, 10 failed, 3 skipped; 59.83 s. Nine protocol-render failures and the project-schema regression failure have the same version cause. |
| Optional unit skips | Expected, not acceptance evidence | Postgres/BQ browsing equivalence need opt-in live credentials; wheel-security metadata check is supplied by CI after building. |
| Offline WIF smoke | Passed, 7/7 | Real app and dbt ADC loading, synthetic external-account source, simulated STS and service-account impersonation, refreshed tokens, STS/IAM failures without fallback, and actual subprocess environment propagation. Unexpected HTTP transport blocked. Live WIF/IAM not verified. |
| Candidate CI | **Failed**, 1/5 jobs passed | Package smoke passed; lint/unit, Postgres 14, Postgres 16 and BQ compile fail on dbt's project version schema. |
| Built distributions | Passed | One wheel and one source archive; both pass `twine check`. Wheel has 16 files, source archive 189. |
| Distribution inventory / credentials | Passed | No private-key blocks, `.env`, local `profiles.yml`, ADC file, SQLite cache, bytecode, checkout metadata, environment, targets, logs or installed dbt packages. |
| Clean Python 3.11 wheel installation | Passed | Outside checkout, without development lockfile: 118 compatible packages, CLI help and version/import checks passed. Candidate wheel is byte-identical to the wheel installed in that environment. |
| Independent dbt consumer | **Blocked at parse** | `dbt deps` installed exact candidate commit from public Git; dbt 1.12.5/Postgres adapter 1.11.0 then rejected the package project version. No lifecycle run attempted. |
| Postgres 14/16 full acceptance | **Blocked** | Candidate CI fails before lifecycle/acceptance. Isolated 14.20/16.11 QA runtimes prepared; no passing acceptance result claimed. |
| Full credentialed BigQuery acceptance | **Not run** | Version validation prevents dbt operations; must run the complete suite with six workers after resolution. |
| Restricted runner/reviewer permissions | **Not run** | Serial shared-QA suite remains required after full BigQuery acceptance. Shared QA was not reset or altered. |
| BigQuery identity preflight | Passed, not permissions-gate evidence | Read-only `SESSION_USER()` matched `dbt-dqm-dev-sa`, `dbt-dqm-runner` and `dbt-dqm-reviewer` service accounts in project `dbt-dqm`. Used existing local files only. |
| Browser QA and 50,000-issue memory | **Not run** | Both remain required against the valid final candidate. |
| Security review | Passed for preparation | No open Dependabot alerts at preflight; pip-audit of the clean environment reports no known vulnerabilities. Local unpublished project is not itself registry-auditable. |
| Trusted publisher / approval protection | Confirmed | Maintainer confirmed trusted publishing; existing `pypi` required reviewer and `v*` tag policy inspected. No protections changed. |
| Tag / approval pause / publication | **Not attempted** | No annotated alpha tag or release workflow triggered; approval pause not yet observed. |

The provenance inventory fix adds `uv build --no-create-gitignore`: otherwise uv creates
`dist/.gitignore`, which the intentionally strict provenance verifier rejects. The verifier's
artifact count, exact source/workflow identity, retries and publication ordering remain intact.

## Candidate artifact hashes

These identify local artifacts for the candidate above, not future published artifacts:

| Artifact | SHA-256 |
| --- | --- |
| `dbt_dqm-0.2.0a1-py3-none-any.whl` | `e103b32c6cb035cd6aa69e8893cf0a34ce54b33d3343e702e572dc0203cade91` |
| `dbt_dqm-0.2.0a1.tar.gz` | `31ccb50c55e6920e6a74bb5dd5636b9ce6d089a11502245530e7ba83bb43c215` |

Local diagnostic logs, JUnit, artifact inspection and security report are retained in
`/private/tmp/dbt-dqm-alpha-qa/`. Credentials were not copied there. CI provides public logs
through the candidate run linked above. A later documentation-only evidence commit records
these results; it does not change the production candidate against which they were measured.

## Publication and Hub status

Python `0.2.0a1` and Git `v0.2.0a1` were absent at preflight. Neither package has been published
by this preparation. Hub registration has not been submitted, because it follows publication.

The maintainer selected the required Python-style tag without a second Hub alias.
[Hub's current parser](https://github.com/dbt-labs/hubcap/blob/main/hubcap/version.py)
rejects `v0.2.0a1`; future repository registration does not imply this alpha is indexed.
Keep the Git installation example and report registration status independently of alpha
availability. WIF support remains offline compatibility coverage without provisioning or
maintaining cloud infrastructure; live warehouse gates use existing service-account files.
