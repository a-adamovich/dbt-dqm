# 0.2.0a3 release candidate evidence

Prepared 2026-10-13. Publication is pending. Final candidate hashes, gate results and artifact
verification will be recorded after the checks complete.

This candidate makes warehouse adapters optional, follows dbt configuration defaults, fixes
Windows executable lookup, preserves worker error diagnostics, checks required app relations,
orders setup models and supports custom model aliases. Internal tracking names and warehouse
data are preserved; no schema migration is introduced.

Native Windows/macOS CI covers configuration, process spawning, authentication libraries and
clean wheel variants on Python 3.11–3.14. This does not certify an individual corporate Windows
network, identity setup or project. Live BigQuery gates use existing service-account credentials;
WIF loading and refresh retain their offline library-level coverage.
