"""Versioned funding observations and company-managed opportunity fields."""

import json
import re
from datetime import datetime, timezone

from odoo import api, fields, models  # deptry: ignore[DEP001]
from odoo.exceptions import AccessError, ValidationError  # deptry: ignore[DEP001]
from odoo.tools import SQL  # deptry: ignore[DEP001]

CONTRACT = 'funding-monitor-v1'
COLLECTOR = 'funding_monitor.group_collector'
MANUAL_FIELDS = {'x_company_id', 'x_company_notes', 'x_company_assessment'}
ROW_LIMIT = 256 * 1024
OBSERVATION_FIELDS = {'checked_at', 'last_attempt_at', 'verification'}
VERIFICATION = {'verified', 'partial', 'not_reverified'}


class FundingOpportunity(models.Model):
    _name = 'x_oportunidade_fomento'
    _description = 'Funding opportunity'
    _rec_name = 'x_name'

    x_name = fields.Char(required=True, readonly=True)
    x_identity = fields.Char(required=True, index=True, readonly=True)
    x_current_id = fields.Many2one('x_revisao_fomento', readonly=True, ondelete='restrict')
    x_generation = fields.Integer(default=0, readonly=True)
    x_checked_at = fields.Datetime(readonly=True)
    x_last_attempt_at = fields.Datetime(readonly=True)
    x_verification = fields.Char(readonly=True)
    x_company_id = fields.Many2one('res.company')
    x_company_notes = fields.Text(groups='funding_monitor.group_editor')
    x_company_assessment = fields.Selection([
        ('potential_direct', 'Potential direct applicant'),
        ('conditional_partner', 'Conditional on partnership'),
        ('incompatible', 'Confirmed incompatible'),
        ('insufficient_information', 'Insufficient information'),
    ], groups='funding_monitor.group_editor')

    _identity_unique = models.Constraint('UNIQUE(x_identity)', 'Funding identity already exists.')

    def write(self, vals):
        if not self.env.su and vals.keys() - MANUAL_FIELDS:
            raise AccessError('Official funding fields are managed by the collector.')
        return super().write(vals)

    @api.model
    def funding_contract(self, for_write=False):
        self.check_access('read')
        self.env['x_revisao_fomento'].check_access('read')
        if for_write and not self.env.user.has_group(COLLECTOR):
            raise AccessError('Funding collector access denied.')
        if for_write:
            self.env.cr.execute(SQL("""
                SELECT count(*) FROM pg_constraint
                WHERE contype = 'u' AND (
                    (conrelid = to_regclass(%s) AND conname = %s)
                    OR (conrelid = to_regclass(%s) AND conname = %s)
                )
                """, self._table, self._table + '_identity_unique',
                self.env['x_revisao_fomento']._table, 'x_revisao_fomento_revision_unique'))
            if self.env.cr.fetchone() != (2,):
                raise ValidationError('Funding uniqueness constraints are missing.')
        return {'contract': CONTRACT, 'write_checked': bool(for_write)}

    @api.model
    def funding_page(self, after_id=0, limit=16):
        if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 16:
            raise ValidationError('Invalid funding pagination.')
        self.check_access('read')
        revisions = self.env['x_revisao_fomento']
        revisions.check_access('read')
        records = self.search([('id', '>', after_id)], order='id', limit=limit)
        heads = records.mapped('x_current_id')
        heads.check_access('read')
        return [record._funding_row() for record in records]

    def _funding_row(self):
        self.ensure_one()
        head = self.x_current_id
        state = {
            **head.x_state,
            'checked_at': self._format_timestamp(self.x_checked_at),
            'last_attempt_at': self._format_timestamp(self.x_last_attempt_at),
            'verification': self.x_verification,
        } if head else None
        return {'id': self.id, 'identity': self.x_identity, 'revision': self.x_generation, 'state': state}

    @staticmethod
    def _format_timestamp(value):
        return value.replace(tzinfo=timezone.utc).isoformat(timespec='seconds') if value else ''

    @staticmethod
    def _parse_timestamp(value):
        if not value:
            return False
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValidationError('Funding timestamps require a time zone.')
        return result.astimezone(timezone.utc).replace(tzinfo=None)

    @api.model
    def funding_append(self, identity, expected_revision, state):
        if not self.env.user.has_group(COLLECTOR):
            raise AccessError('Funding collector access denied.')
        if (not isinstance(identity, str) or not re.fullmatch(r'[a-z][a-z0-9:/._-]{0,159}', identity)
                or type(expected_revision) is not int or expected_revision < 0 or not isinstance(state, dict)
                or state.get('identity') != identity or not isinstance(state.get('title'), str)
                or not state['title'].strip() or len(state['title']) > 500
                or {key for key in state if key.startswith('x_')}
                or not isinstance(state.get('last_attempt_at'), str) or not state['last_attempt_at']
                or not isinstance(state.get('checked_at'), str) or state.get('verification') not in VERIFICATION
                or len(json.dumps(state).encode()) > ROW_LIMIT):
            raise ValidationError('Invalid funding revision.')
        attempted = self._parse_timestamp(state['last_attempt_at'])
        verified = self._parse_timestamp(state['checked_at'])
        if verified and verified > attempted:
            raise ValidationError('Last successful verification is after the attempt.')
        records = self.sudo()
        record = records.search([('x_identity', '=', identity)], limit=1)
        if record:
            self.env.cr.execute(SQL('SELECT id FROM %s WHERE id = %s FOR UPDATE', SQL.identifier(self._table),
                                    record.id))
        if (record.x_generation or 0) != expected_revision:
            return {'status': 'stale'}
        if record and record._funding_row()['state'] == state:
            return {'status': 'confirmed', 'revision': expected_revision}
        if record and record.x_last_attempt_at and attempted < record.x_last_attempt_at:
            return {'status': 'stale'}
        if not record:
            record = records.create({'x_name': state['title'], 'x_identity': identity})
        number = expected_revision + 1
        head = record.x_current_id
        official = {key: value for key, value in state.items() if key not in OBSERVATION_FIELDS}
        if not head or head.x_state != official:
            head = self.env['x_revisao_fomento'].sudo().create({
                'x_opportunity_id': record.id, 'x_revision': number, 'x_state': official,
            })
        record.write({'x_name': state['title'], 'x_current_id': head.id, 'x_generation': number,
                      'x_checked_at': verified, 'x_last_attempt_at': attempted, 'x_verification': state['verification']})
        return {'status': 'stored', 'revision': number}

    @api.model
    def funding_head(self, identity):
        self.check_access('read')
        record = self.search([('x_identity', '=', identity)], limit=1)
        if not record:
            return None
        record.x_current_id.check_access('read')
        return record._funding_row()


class FundingRevision(models.Model):
    _name = 'x_revisao_fomento'
    _description = 'Official funding revision'
    _order = 'x_opportunity_id, x_revision desc'

    x_opportunity_id = fields.Many2one('x_oportunidade_fomento', required=True, index=True,
                                      readonly=True, ondelete='restrict')
    x_revision = fields.Integer(required=True, readonly=True)
    x_state = fields.Json(required=True, readonly=True)

    _revision_unique = models.Constraint('UNIQUE(x_opportunity_id, x_revision)', 'Funding revision already exists.')
