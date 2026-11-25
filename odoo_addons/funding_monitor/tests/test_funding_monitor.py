"""Run with the Odoo 19 addon test runner in a staging database."""

from odoo.exceptions import AccessError  # deptry: ignore[DEP001]
from odoo.tests import TransactionCase, new_test_user  # deptry: ignore[DEP001]


class FundingMonitorTest(TransactionCase):
    def test_immutable_official_revisions_and_manual_ownership(self):
        collector = new_test_user(self.env, login='funding_collector_test', groups='funding_monitor.group_collector')
        editor = new_test_user(self.env, login='funding_editor_test', groups='funding_monitor.group_editor')
        model = self.env['x_oportunidade_fomento']
        collector_model = model.with_user(collector)
        editor_model = model.with_user(editor)
        self.assertEqual(collector_model.funding_contract(for_write=True)['write_checked'], True)
        state = {'identity': 'example:call:1', 'title': 'Call', 'checked_at': '2026-11-25T00:00:00+00:00',
                 'last_attempt_at': '2026-11-25T00:00:00+00:00', 'verification': 'verified',
                 'facts': {'deadline': {'value': '2027-01-01'}}}
        self.assertEqual(collector_model.funding_append(state['identity'], 0, state)['status'], 'stored')
        row = collector_model.funding_head(state['identity'])
        self.assertEqual(row['state'], state)
        editor_record = editor_model.browse(row['id'])
        editor_record.write({'x_company_notes': 'Manual note'})
        with self.assertRaises(AccessError):
            editor_record.write({'x_identity': 'override'})
        with self.assertRaises(AccessError):
            editor_model.funding_append(state['identity'], 1, state)

        next_state = {**state, 'last_attempt_at': '2026-11-25T06:00:00+00:00', 'verification': 'not_reverified'}
        self.assertEqual(collector_model.funding_append(state['identity'], 1, next_state)['status'], 'stored')
        self.assertEqual(collector_model.funding_append(state['identity'], 1, state)['status'], 'stale')
        self.assertEqual(collector_model.funding_append(state['identity'], 2, next_state)['status'], 'confirmed')
        revisions = self.env['x_revisao_fomento'].search([('x_opportunity_id', '=', row['id'])])
        self.assertEqual(len(revisions), 1)

        extension = {**state, 'last_attempt_at': '2026-11-25T12:00:00+00:00',
                     'checked_at': '2026-11-25T12:00:00+00:00', 'facts': {'deadline': {'value': '2027-02-01'}}}
        self.assertEqual(collector_model.funding_append(state['identity'], 2, extension)['status'], 'stored')
        self.assertEqual(collector_model.funding_head(state['identity'])['state'], extension)
        self.assertEqual(editor_record.x_company_notes, 'Manual note')
        self.assertEqual(len(self.env['x_revisao_fomento'].search([('x_opportunity_id', '=', row['id'])])), 2)
