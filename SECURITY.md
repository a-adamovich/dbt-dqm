# Security Policy

## Supported Versions

dbt-dqm is pre-1.0 (currently `0.x`). Security fixes are made against the latest release on the
`main` branch; there is no backport policy for older `0.x` versions yet.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for a suspected security vulnerability.

Instead, use GitHub's private vulnerability reporting: open the repository's **Security** tab and
select **Report a vulnerability**. This opens a private advisory visible only to the maintainer
until a fix is ready.

Please include:

- A description of the vulnerability and its potential impact.
- Steps to reproduce, or a proof-of-concept if available.
- The affected version(s).

You should expect an initial response within a few business days. Coordinated disclosure is
appreciated — please allow time for a fix to be released before any public disclosure.

## Scope Notes

- The local review app (`dbt-dqm app`) never writes warehouse credentials to its local SQLite
  workspace; see `docs/functional-requirements.md` for the specifics of what it stores locally.
- The dbt package (`models/`, `macros/`) runs entirely inside your own warehouse under your own
  credentials — dbt-dqm has no external network calls of its own.
