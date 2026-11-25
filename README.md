# odoo-pn

## Scientific metadata preview

Use `PYTHONPATH=src uv run --locked python -m intelligence science-preview --summary-only`
for a read-only preview. The default window is 14 UTC calendar days. Use
`--from-date` and `--through-date` together to replay up to 31 days.
PubMed uses creation, modification and MeSH dates. Crossref uses its index
date, which can change after reindexing. The SciELO DOI stream uses Crossref
for discovery. SciELO ArticleMeta can confirm publisher-deposited legacy
article identifiers. The OpenAlex supplement uses publication dates.
The INPI catalogue covers historical radar editions.

Use `--science-output PATH` to create a new private JSON snapshot, and
`--science-baseline PATH` to carry observations forward from a previous
snapshot. Keep the newest verified snapshot to preserve the accumulated
record. Missing records and partial responses leave prior observations
intact. The scheduled preview reports metrics and does not maintain a
persistent baseline. Inspect the per-query metrics for pagination caps.
Choose shorter replay periods if a query reached its cap.

The Data Sync workflow accepts the manual `science-preview` mode, which
also checks Odoo metadata when credentials are available. The independent
daily workflow runs at 09:25 UTC when the repository variable
`SCIENCE_PREVIEW_ENABLED` is set to `true`. Scientific preview performs no
Odoo writes. The private snapshot can include a Crossref abstract where
the deposited version of record carries a current CC BY or CC0 license.
Summary logs omit titles, authors, and abstracts.
