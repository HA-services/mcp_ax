# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
"""Static definitions of the MCP tools offered by the server."""

STR = {'type': 'string'}
INT = {'type': 'integer'}
BOOL = {'type': 'boolean'}
DOMAIN = {
    'type': 'array',
    'description': 'Odoo domain, for example [["state","=","sale"],["date_order",">=","@month_start"]]. '
                   'Date macros: @today, @today-30d, @today+7d, @month_start, @year_start, @now.',
    'items': {},
}
FIELDS = {'type': 'array', 'items': STR, 'description': 'Field names to return. Omit for the default set.'}
MODEL = {'type': 'string', 'description': 'Technical model name, for example res.partner.'}
DRY = {'type': 'boolean', 'description': 'Run the change and roll it back, returning a preview.'}
CONFIRM = {'type': 'string', 'description': 'Confirmation token returned by an earlier call that asked for confirmation.'}


def schema(props, required=()):
    return {'type': 'object', 'properties': props, 'required': list(required), 'additionalProperties': False}


TOOLS = [
    {'name': 'whoami', 'title': 'Who am I', 'kind': 'read', 'op': 'none', 'seq': 1,
     'description': 'Show the Odoo user, company, access level, limits and server version this connection uses. '
                    'Call this first.',
     'schema': schema({})},
    {'name': 'list_models', 'title': 'List models', 'kind': 'read', 'op': 'none', 'seq': 2,
     'description': 'List the models the user can read, optionally filtered by a text query on name or technical name.',
     'schema': schema({'query': STR, 'limit': INT})},
    {'name': 'describe_model', 'title': 'Describe a model', 'kind': 'read', 'op': 'read', 'seq': 3,
     'description': 'Return the fields of a model with type, label, required flag, relation, selection values and '
                    'whether the field is masked, plus the operations this connection may run on it.',
     'schema': schema({'model': MODEL}, ['model'])},
    {'name': 'search_records', 'title': 'Search records', 'kind': 'read', 'op': 'read', 'seq': 4,
     'description': 'Search and read records of a model. Returns at most the configured maximum per call; '
                    'use offset to page.',
     'schema': schema({'model': MODEL, 'domain': DOMAIN, 'fields': FIELDS, 'limit': INT, 'offset': INT,
                       'order': {'type': 'string', 'description': 'For example "date_order desc".'}}, ['model'])},
    {'name': 'count_records', 'title': 'Count records', 'kind': 'read', 'op': 'read', 'seq': 5,
     'description': 'Count the records matching a domain.',
     'schema': schema({'model': MODEL, 'domain': DOMAIN}, ['model'])},
    {'name': 'read_records', 'title': 'Read records by id', 'kind': 'read', 'op': 'read', 'seq': 6,
     'description': 'Read specific records by id.',
     'schema': schema({'model': MODEL, 'ids': {'type': 'array', 'items': INT}, 'fields': FIELDS}, ['model', 'ids'])},
    {'name': 'aggregate', 'title': 'Aggregate and group', 'kind': 'read', 'op': 'read', 'seq': 7,
     'description': 'Group records and compute totals, for example group_by ["partner_id"] with measures '
                    '["amount_total:sum"]. Date fields accept a granularity: "date_order:month".',
     'schema': schema({'model': MODEL, 'domain': DOMAIN,
                       'group_by': {'type': 'array', 'items': STR},
                       'measures': {'type': 'array', 'items': STR,
                                    'description': 'field:sum, field:avg, field:min, field:max, field:count_distinct'},
                       'limit': INT, 'order': STR}, ['model', 'group_by'])},
    {'name': 'name_search', 'title': 'Find by name', 'kind': 'read', 'op': 'read', 'seq': 8,
     'description': 'Find records by display name, the way an Odoo many2one field does.',
     'schema': schema({'model': MODEL, 'name': STR, 'domain': DOMAIN, 'limit': INT}, ['model', 'name'])},
    {'name': 'export_records', 'title': 'Export to CSV', 'kind': 'read', 'op': 'read', 'seq': 9,
     'description': 'Export up to 10000 records to a CSV file and return a download link. Masks apply.',
     'schema': schema({'model': MODEL, 'domain': DOMAIN, 'fields': FIELDS, 'limit': INT}, ['model', 'fields'])},
    {'name': 'create_records', 'title': 'Create records', 'kind': 'write', 'op': 'create', 'seq': 20,
     'dry': True,
     'description': 'Create one or more records. Every record is written to the change journal and can be undone.',
     'schema': schema({'model': MODEL, 'records': {'type': 'array', 'items': {'type': 'object'}},
                       'dry_run': DRY, 'confirm': CONFIRM}, ['model', 'records'])},
    {'name': 'update_records', 'title': 'Update records', 'kind': 'write', 'op': 'write', 'seq': 21,
     'dry': True,
     'description': 'Update records selected by ids or by a domain. Old and new values are journaled so the change '
                    'can be undone.',
     'schema': schema({'model': MODEL, 'ids': {'type': 'array', 'items': INT}, 'domain': DOMAIN,
                       'values': {'type': 'object'}, 'dry_run': DRY, 'confirm': CONFIRM}, ['model', 'values'])},
    {'name': 'delete_records', 'title': 'Delete records', 'kind': 'write', 'op': 'unlink', 'seq': 22,
     'dry': True,
     'description': 'Delete records. Records with an active flag are archived (and can be restored with undo_change). '
                    'Others are removed only when they are in a draft or cancelled state. Posted or confirmed '
                    'documents are refused.',
     'schema': schema({'model': MODEL, 'ids': {'type': 'array', 'items': INT}, 'dry_run': DRY,
                       'confirm': CONFIRM}, ['model', 'ids'])},
    {'name': 'call_method', 'title': 'Call a method', 'kind': 'write', 'op': 'call', 'seq': 23,
     'description': 'Call a public method on records, for example action_confirm on sale.order. Private methods and '
                    'generic write, create and unlink are refused. Methods cannot be rolled back, so there is no dry '
                    'run and the default policy asks for confirmation.',
     'schema': schema({'model': MODEL, 'method': STR, 'ids': {'type': 'array', 'items': INT},
                       'args': {'type': 'array', 'items': {}}, 'kwargs': {'type': 'object'},
                       'confirm': CONFIRM}, ['model', 'method'])},
    {'name': 'undo_change', 'title': 'Undo a change', 'kind': 'write', 'op': 'write', 'seq': 24,
     'description': 'Undo a journaled change by journal id or by batch id. Refused when the record changed since.',
     'schema': schema({'journal_id': INT, 'batch': STR, 'force': BOOL, 'confirm': CONFIRM})},
    {'name': 'list_changes', 'title': 'List journaled changes', 'kind': 'read', 'op': 'none', 'seq': 25,
     'description': 'List recent changes made through MCP by this user, newest first.',
     'schema': schema({'model': STR, 'limit': INT, 'only_undoable': BOOL})},
    {'name': 'approval_status', 'title': 'Approval status', 'kind': 'read', 'op': 'none', 'seq': 26,
     'description': 'Check the state of an approval request raised by a previous call.',
     'schema': schema({'approval_id': INT}, ['approval_id'])},
    {'name': 'list_approvals', 'title': 'List approvals', 'kind': 'read', 'op': 'none', 'seq': 27,
     'description': 'List this user\'s approval requests.',
     'schema': schema({'state': STR, 'limit': INT})},
    {'name': 'start_bulk_job', 'title': 'Start a bulk job', 'kind': 'write', 'op': 'write', 'seq': 30,
     'dry': True,
     'description': 'Update or archive many records in batches in the background. The record set is fixed when the '
                    'job starts. Use job_status to follow it.',
     'schema': schema({'model': MODEL, 'domain': DOMAIN, 'operation': {'type': 'string', 'enum': ['write', 'archive']},
                       'values': {'type': 'object'}, 'dry_run': DRY, 'confirm': CONFIRM},
                      ['model', 'domain', 'operation'])},
    {'name': 'job_status', 'title': 'Bulk job status', 'kind': 'read', 'op': 'none', 'seq': 31,
     'description': 'Progress of a bulk job.', 'schema': schema({'job_id': INT}, ['job_id'])},
    {'name': 'publish_page', 'title': 'Publish a live page', 'kind': 'write', 'op': 'read', 'seq': 40,
     'description': 'Publish a read-only live page (table with optional bar chart) built from a query. Modes: '
                    'private (owner only), login (any signed in user, shown with their own rights) and link '
                    '(anyone with the link, always needs an approval).',
     'schema': schema({'title': STR, 'model': MODEL, 'domain': DOMAIN, 'fields': FIELDS, 'group_by': STR,
                       'measure': STR, 'chart': {'type': 'string', 'enum': ['none', 'bar']},
                       'mode': {'type': 'string', 'enum': ['private', 'login', 'link']},
                       'expires_in_days': INT, 'confirm': CONFIRM}, ['title', 'model'])},
    {'name': 'list_pages', 'title': 'List live pages', 'kind': 'read', 'op': 'none', 'seq': 41,
     'description': 'List the live pages published by this user.', 'schema': schema({})},
    {'name': 'unpublish_page', 'title': 'Unpublish a page', 'kind': 'write', 'op': 'none', 'seq': 42,
     'description': 'Take a live page offline.', 'schema': schema({'slug': STR}, ['slug'])},
    {'name': 'build_module', 'title': 'Build an Odoo module', 'kind': 'admin', 'op': 'none', 'seq': 50,
     'description': 'Generate a small installable Odoo module (models, fields, list and form views, menu, access '
                    'rights) from a specification and return a ZIP download link. Nothing is installed on this '
                    'server. Models must be named like x_something or vendor.thing.',
     'schema': schema({'technical_name': STR, 'title': STR, 'summary': STR,
                       'models': {'type': 'array', 'items': {'type': 'object'},
                                  'description': 'Each: {name, description, fields:[{name, type, string, required, '
                                                 'selection, relation}]}'},
                       'confirm': CONFIRM}, ['technical_name', 'title', 'models'])},
]

TOOL_BY_NAME = {t['name']: t for t in TOOLS}
