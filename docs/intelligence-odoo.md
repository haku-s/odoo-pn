# Strategic intelligence on Odoo Online 20

Use Odoo Studio and standard JSON-2 operations. Test the schema and integration
accounts on a neutralized duplicate before applying the approved configuration
to production. Use developer mode to inspect actual model and field keys.
The names below specify the contract for additions. Verify related fields,
required fields, access domains and selection keys against the target database
before a writer can run.

## News

Reuse Publications, Sources and Subjects. In Data Sync, use `inspect` to
validate metadata and `diagnose-news` to compare deployed source registrations
with the repository registry. Advertise a source as active only after its
configured endpoint and a live read have been checked. Use the registry code
and URL in `DIAGNOSTIC_CHANNELS`; select `API REST` for entries in `NEWS_APIS`
and `RSS` for the other channels. Preserve existing codes and avoid a second
registration for the same endpoint. Keep the current editorial terms until
their changes are approved.

The news writer requires read access to Subjects, read/write access to Sources
and read/create/write access scoped to non-scientific Publications, with no delete.
Keep the current Avatar field and existing views. Add a category exclusion to
the news menu if it also displays scientific records, without changing its
existing date policy.

## Funding models

Create or reuse `x_oportunidade_fomento` for the current official opportunity
and `x_revisao_fomento` for its official evidence. Studio creates the
description field `x_name`. Use the following technical keys.

| Model | Type | Technical fields |
| --- | --- | --- |
| `x_oportunidade_fomento` | Char | `x_name`, `x_identity`, `x_institution`, `x_modality`, `x_official_url`, `x_official_status`, `x_operational_status`, `x_submission_status`, `x_deadline_time`, `x_currency`, `x_compatibility`, `x_verification`, `x_current_digest` |
| | Integer | `x_revision_number` |
| | Date | `x_deadline` |
| | Float | `x_global_amount`, `x_min_amount`, `x_max_amount` |
| | Boolean | `x_has_global_amount`, `x_has_min_amount`, `x_has_max_amount`, `x_conflicts` |
| | Datetime | `x_checked_at`, `x_last_attempt_at` |
| | One2many | `x_revisions` to `x_revisao_fomento`, inverse `x_opportunity_id` |
| `x_revisao_fomento` | Char | `x_name`, `x_digest` |
| | Integer | `x_number` |
| | Text | `x_evidence` |
| | Many2one | `x_opportunity_id` to `x_oportunidade_fomento`, required, restrict deletion |

In the funding app create a **Funding opportunities** menu with a list/form
window action on `x_oportunidade_fomento`. Put institution, modality, official
and submission statuses, deadline, currency, amounts, verification and
conflicts in the list and search views. For amount filters include its
corresponding presence flag. Put the source URL, last successful verification,
last attempt and revision list on the form. Expose a read-only **Official
revisions** list/form through `x_revisions`, ordered by `x_number`. Evidence
is a dedicated versioned document with official extracts, source URLs,
document hashes and conflicting alternatives. The corresponding quantitative
and status fields remain searchable without parsing the evidence. Filename or
URL reuse with new official evidence creates a new version. The initial,
pre-proposal and restricted deadlines retain their distinct meanings; the
searchable deadline represents entry for a new applicant.

In Odoo 20 use the Access records (`ir.access`) and verify the grants on the
target version. Grant the funding integration account read/create/write on opportunities,
read/create on revisions, and no delete on either. Grant staff read-only
access to both official models. Keep internal notes and eligibility decisions
in a separately permissioned existing evaluation model when available. If
needed, use Studio to create `x_fomento_avaliacao` with `x_opportunity_id`
(required Many2one to `x_oportunidade_fomento`, restrict deletion), `x_name` (Char),
`x_notes` (Text), `x_assessment` (Selection), and `x_company_id` (Many2one
to `res.company`). Add an **Internal assessments** menu for staff and grant
the integration account no rights on this model. A read-only view alone does
not enforce write permissions.

The head and new revision are written in one standard ORM call through the
One2many. Each opportunity is confirmed by a subsequent read. Independent
JSON-2 calls and different opportunities have independent transactions.
Duplicates and stale reads cause a reconciliation error. Standard Studio
fields provide no unique identity constraint or compare-and-set API. The
deployment therefore requires one authorized writer, including during manual
maintenance and recovery. Do not import or edit official records concurrently
with the workflow. A contradictory or duplicate row requires manual review.

## Scientific publications

Reuse `x_publicacoes`. Add the selection value `scientific_publication`
(label **Scientific publication**) to `x_studio_tipo_de_publicacao`. Register
one active source `science_metadata` in `x_fontes`, method `API REST`,
URL `https://pubmed.ncbi.nlm.nih.gov/`. Keep its news collection checkbox off.
The source is the publication owner, while provider metadata identifies the
actual bibliographic providers. The science writer validates this distinction.

| Type | Technical fields added to `x_publicacoes` |
| --- | --- |
| Char | `x_science_doi`, `x_science_pmid`, `x_science_scielo`, `x_science_openalex`, `x_science_inpi`, `x_science_published`, `x_science_kind`, `x_science_providers` |
| Date | `x_science_indexed` |
| Text | `x_science_links`, `x_science_observations` |

Leave Subject, the news publication datetime and the news summary optional at
the model level. Scientific dates retain their supplied precision; a year
or month is never converted into a fabricated day. Keep mandatory news form
behavior in its existing view. Scientific observations hold licensed or
bibliographic provider data, exact dates and typed links to corrections,
retractions and versions. The stable publication ID is created once and
remains unchanged when a provider later supplies additional identifiers. Use
the existing `x_active` flag; new scientific publications are created active
and existing archived publications stay archived.

Add a **Scientific publications** list/form window action on `x_publicacoes`
with domain `[('x_studio_tipo_de_publicacao','=','scientific_publication')]`.
Expose title, DOI, PMID, provider, type and publication/index dates in the
list and search views. Show the original link and typed relationships in the
form. Give the scientific account read access to Sources, read/create/write
access to scientific Publications through an access domain on the selection,
and no delete. Ensure additive permissions from other groups do not widen
either collector's write scope. Readers can have access to both categories.

## Activation and reversal

1. On a neutralized Odoo Online duplicate, provision the fields, access domains
   and views. Test both a positive and a negative action with each account,
   including a write to the other publication category. Confirm visible
   dates, filters, manual fields, revisions and related identifiers.
2. Run the read-only previews and preflights. Example from the repository:
   `funding-check --summary-only` checks the writable contract and reads the
   baseline without sending writes. Run `science-preview --science-odoo
   --summary-only` to inspect the current publication contract. Run each
   staging writer twice, including a replay and a simulated unavailable
   provider, and compare identities, revisions and manually owned records.
3. After the owner authorizes the production deployment, confirm a restorable
   backup, create the approved Studio configuration and dedicated integration
   accounts, and repeat preflight. Keep GitHub enable variables unset during
   setup. Configure the existing read preview credentials and
   `NEWS_ODOO_API_KEY` as a secret. Configure `ODOO_URL`,
   `FUNDING_ODOO_API_KEY` and optional `ODOO_DATABASE` in the
   `funding-production` environment, with analogous settings using
   `SCIENCE_ODOO_API_KEY` in `science-production`. Keep credentials private.
4. Enable the approved workflows individually using `NEWS_SYNC_ENABLED`,
   `FUNDING_SYNC_ENABLED` and `SCIENCE_SYNC_ENABLED`, setting each chosen
   variable to `true`. Check confirmation counts plus the corresponding
   menus and search views after the first runs. Read public Actions logs for
   incomplete coverage and retry the affected window where appropriate.
5. To halt a writer, unset its variable, cancel any active or queued run and,
   if necessary, revoke its dedicated API key. Keep existing records and
   revisions. Stopping the writer does not revert committed data. Restoring
   data or removing fields requires separate authorization.

References
[Odoo 20 JSON-2 API](https://www.odoo.com/documentation/20.0/developer/reference/external_api.html),
[Studio models](https://www.odoo.com/documentation/20.0/applications/studio/models_modules_apps.html),
[Studio views](https://www.odoo.com/documentation/20.0/applications/studio/views.html),
[neutralized databases](https://www.odoo.com/documentation/20.0/administration/neutralized_database.html).
