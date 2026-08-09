-- Package-level invariant: at most one Active occurrence per (test_unique_id, unique_id).
-- A violation means two dqm_reconcile runs raced (e.g. overlapping CI jobs) and both created a
-- simultaneously Active occurrence for the same failure identity. Any returned row is a failure.
select
  test_unique_id,
  unique_id,
  count(*) as active_occurrence_count
from {{ ref('dqm_current_issues') }}
group by test_unique_id, unique_id
having count(*) > 1
