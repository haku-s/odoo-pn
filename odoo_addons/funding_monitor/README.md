# Funding Monitor deployment

This addon is the Odoo 19 contract for the scheduled funding collector. Odoo
stores the current observation and immutable official revisions. Each call to
`funding_append` handles the identity, new revision, and current pointer in one
transaction. A row lock and expected generation prevent concurrent replacement.
The unique identity also covers concurrent creation before a row exists. A lost
response is confirmed with a separate read. A stale writer leaves the current
record intact and the next run reconciles from the Odoo head.

## Models and ownership

| Model | Field | Type | Owner and use |
|---|---|---|---|
| `x_oportunidade_fomento` | `x_identity` | Char, required, SQL unique | Official stable identity, derived from institution/program and call identifier |
| | `x_name` | Char, required | Latest official call title |
| | `x_current_id` | Many2one to `x_revisao_fomento`, restrict delete | Current official revision |
| | `x_generation` | Integer | Compare-and-append token, including attempts without a new revision |
| | `x_checked_at` | Datetime | Most recent fully successful verification, UTC |
| | `x_last_attempt_at` | Datetime | Most recent collection attempt, UTC |
| | `x_verification` | Char | `verified`, `partial`, or `not_reverified` |
| | `x_company_id` | Many2one to `res.company` | Human-selected applicant |
| | `x_company_notes` | Text | Human-managed notes |
| | `x_company_assessment` | Selection | Human assessment of applicability |
| `x_revisao_fomento` | `x_opportunity_id` | Many2one to `x_oportunidade_fomento`, restrict delete | Official revision owner |
| | `x_revision` | Integer, unique with `x_opportunity_id` | Immutable revision number |
| | `x_state` | JSON | Versioned official data, evidence, document digests, unresolved alternatives, and calendar status |

The JSON field uses the documented `Opportunity` contract. `facts` contains
individual sourced values, publication time when available, URL, section, page,
and authority. `documents` maps document URLs to verified SHA-256 digests, when
available. `conflicts` preserves competing evidence. When only the attempt
timestamps or verification outcome change, the collector updates the observation
fields and reuses the existing official revision. It never includes human-owned
fields in API payloads. The collector and editor groups both have read access.
Editors can write only the three human-owned fields. Collector writes go
exclusively through the restricted transactional model method. Neither group can
delete opportunities or versions or create and modify version rows directly.

A compatible existing implementation can be reused if it exposes the same
model, field, and method contracts, enforces the constraints and ownership above,
and passes preflight. Studio fields alone cannot provide this transactional
contract. Odoo Online does not accept arbitrary custom modules. Confirm the
deployment tier and that installing this addon is supported before authorizing
any production setup.

## Activation and verification

1. On an Odoo 19 staging instance that already supports add-ons, add
   `odoo_addons` to the server's add-ons path, update the Apps list, and install
   **Funding Monitor**. On a shell-managed staging instance, the equivalent is
   `odoo-bin -d <staging-db> --addons-path=<existing-paths>,<repo>/odoo_addons -i funding_monitor --test-enable --stop-after-init`.
   This installs the models, SQL constraints, and both security groups.
2. Assign **Funding Monitor Collector** to a dedicated Odoo integration account
   and **Funding Monitor Editor** to the staff who assess opportunities. Keep
   the integration account out of the editor group. Create a JSON-2 API key for
   the integration account.
3. Run `PYTHONPATH=src uv run --locked python -m intelligence funding-check`
   with the staging `ODOO_URL`, `ODOO_API_KEY` and optional `ODOO_DATABASE`.
   This verifies schema and collector permissions and reads the baseline. Use
   `funding-preview --funding-odoo` to see reconciled data without writes.
4. After permission to write in staging, run `funding-sync --summary-only`.
   Verify the first read-back, run again, verify the same external identities
   and compare revisions. Edit a human-owned field between runs and confirm it
   persists. Test the loss of a connection or a stale writer in staging.
5. With separate approval for production, take a restorable Odoo backup,
   install the addon, create the dedicated account and configure the GitHub
   environment `funding-production`. Set environment secrets `ODOO_URL`,
   `FUNDING_ODOO_API_KEY`, and `ODOO_DATABASE` if the server requires it.
   Restrict deployment to the default branch. Environment approval gates each
   execution when configured.
6. From the default branch, run **Funding Monitor** in `funding-check` and
   `funding-preview` before any production write. To authorize manual and
   scheduled synchronization, set the repository variable
   `FUNDING_SYNC_ENABLED` to `true`. The proposed schedule is every six hours,
   at 02:20, 08:20, 14:20, and 20:20 UTC, when the gate is enabled.
7. To halt writes, unset `FUNDING_SYNC_ENABLED` and cancel any running funding
   writer. Revoke the dedicated API key if immediate access revocation is
   required. Keep the stored rows and revisions for audit. Uninstalling the
   addon is a separate destructive schema operation and requires an approved
   backup and restore plan.

The public **Data Sync** `funding-preview` requires no Odoo credentials and
can use an artifact from a preceding preview through `funding_baseline_run`.
Artifacts are retained for 14 days for diagnosis. The scheduled writer builds
on the state read from Odoo.
