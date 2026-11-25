{  # noqa: B018 - Odoo reads the manifest as a literal.
    'name': 'Funding Monitor',
    'version': '19.0.1.0.0',
    'depends': ['base'],
    'license': 'LGPL-3',
    'data': ['security/groups.xml', 'security/ir.model.access.csv'],
    'installable': True,
}
