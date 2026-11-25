# odoo-pn

## Strategic intelligence

Data Sync runs news collection at minute 50 of each hour when
`NEWS_SYNC_ENABLED=true`. The Funding Monitor runs every six hours when
`FUNDING_SYNC_ENABLED=true`. Scientific Intelligence runs daily at 09:25 UTC
when `SCIENCE_SYNC_ENABLED=true`. Writers run only from the default branch
and share one concurrency group per subsystem. The procurement workflow has
its own existing controls.

Manual Data Sync modes `inspect`, `preview`, `diagnose-news`,
`funding-preview`, `science-preview` and `images-preview` are read-only.
Funding and science also support local read-only previews without Odoo
credentials. Public Actions logs contain summary metrics. The optional local
baseline and output files are for diagnostics and historical comparison.
Scheduled funding and science use Odoo as their persistent record.

Scientific collection uses a 14-day UTC overlap. Replay uses `--from-date`
and `--through-date` together, up to 31 days. PubMed uses creation,
modification and MeSH dates. Crossref uses index dates. Capped searches are
subdivided within bounded request budgets. An unresolved cap remains partial.
OpenAlex uses publication dates. SciELO uses Crossref for discovery and
ArticleMeta for verification of deposited legacy identifiers. INPI includes
historical radar editions.

See the [Odoo Online setup and verification procedure](docs/intelligence-odoo.md)
before enabling any writer.
