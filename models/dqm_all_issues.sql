-- Complete reviewer-facing occurrence history, including both Active and Archived records.
-- This stable interface hides the internal reconciliation model name from consumers.
{{ config(materialized='view') }}

select * from {{ ref('dqm_reconcile') }}
