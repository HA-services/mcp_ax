# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import base64
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from datetime import date, datetime, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError
from odoo.tools import config

from .tool_defs import TOOL_BY_NAME, TOOLS

_logger = logging.getLogger(__name__)

SUPPORTED_PROTOCOLS = ['2025-06-18', '2025-03-26', '2024-11-05']
BLOCKED_PREFIXES = ('ir.', 'base.', 'bus.', 'axsync.mcp.', 'res.users.apikeys', 'res.config.settings',
                    'res.users.identitycheck', 'auth.', 'change.password', 'mail.mail', 'mail.template')
WRITE_BLOCKED = {'res.users', 'res.groups', 'res.users.settings', 'res.company', 'res.config', 'mail.message',
                 'mail.followers'}
MAGIC = {'id', 'create_uid', 'create_date', 'write_uid', 'write_date', '__last_update', 'display_name'}
SECRET_RE = re.compile(r'(password|passwd|secret|token|api_?key|private_?key|oauth|signature_key|otp)', re.I)
OPERATORS = {'=', '!=', '<', '>', '<=', '>=', 'like', 'ilike', 'not like', 'not ilike', '=like', '=ilike',
             'in', 'not in', 'child_of', 'parent_of', '=?'}
BLOCKED_METHODS = {
    'unlink', 'write', 'create', 'copy', 'sudo', 'with_user', 'with_env', 'with_context', 'with_company',
    'browse', 'invalidate_cache', 'flush', 'init', 'button_immediate_install', 'button_immediate_uninstall',
    'button_install', 'button_uninstall', 'module_uninstall', 'execute', 'check_access_rights', 'load',
    'import_data', 'action_archive_all', 'toggle_active', 'run_python', 'exec', 'eval', 'safe_eval',
    'reset_password', 'action_reset_password', 'signup_prepare', 'api_key', 'change_password', '_login',
    'authenticate', 'sudo_user', 'message_post_with_source', 'send_mail_all',
}
REL_TYPES = ('many2one', 'one2many', 'many2many')


class McpError(Exception):
    """A refusal or failure that is reported to the agent as a tool error."""

    def __init__(self, message, status='error'):
        super().__init__(message)
        self.status = status


class _DryRunRollback(Exception):
    def __init__(self, result):
        super().__init__('dry run')
        self.result = result


def _jsonable(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=' ') if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, bytes):
        return '<binary %d bytes>' % len(value)
    if isinstance(value, models.BaseModel):
        return value.ids
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class McpEngine(models.AbstractModel):
    _name = 'axsync.mcp.engine'
    _description = 'MCP Engine'

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @api.model
    def clear_runtime_cache(self):
        return True

    @api.model
    def _param(self, key):
        icp = self.env['ir.config_parameter'].sudo()
        getter = icp.get_param if hasattr(icp, 'get_param') else icp.get_str
        return getter(key) or ''

    @api.model
    def base_url(self):
        setting = self.env['axsync.mcp.setting'].get()
        url = setting.public_base_url or self._param('web.base.url') or ''
        return url.rstrip('/')

    @api.model
    def _secret(self):
        return (self._param('database.secret') or 'axsync').encode()

    @api.model
    def _has_access(self, model, op):
        if hasattr(model, 'has_access'):
            return model.has_access(op)
        return model.check_access_rights(op, raise_exception=False)

    @api.model
    def _blocked_model(self, name):
        return any(name == p.rstrip('.') or name.startswith(p) for p in BLOCKED_PREFIXES)

    @api.model
    def _model(self, name, op='read', profiles=None):
        if not isinstance(name, str) or name not in self.env:
            raise McpError(_('Unknown model %s.', name))
        if self._blocked_model(name):
            raise McpError(_('Model %s is not available through MCP.', name), 'denied')
        if op != 'read' and name in WRITE_BLOCKED:
            raise McpError(_('Model %s cannot be changed through MCP.', name), 'denied')
        Model = self.env[name]
        if Model._transient:
            raise McpError(_('Transient models are not available.'), 'denied')
        if op in ('read', 'create', 'write', 'unlink') and not self._has_access(Model, op):
            raise McpError(_('You have no %(op)s access to %(model)s.', op=op, model=name), 'denied')
        if profiles:
            for profile in profiles:
                if not profile.allows(name, op if op != 'call' else 'call'):
                    raise McpError(_('The access level "%(p)s" does not allow %(op)s on %(model)s.',
                                     p=profile.name, op=op, model=name), 'denied')
        return Model

    @api.model
    def _masks(self, model):
        masks = self.env['axsync.mcp.mask'].masks_for(self.env.user, model)
        if self.env['axsync.mcp.setting'].get().mask_default_secrets:
            for fname in self.env[model]._fields:
                if SECRET_RE.search(fname) and fname not in masks:
                    masks[fname] = 'hidden'
        return masks

    @api.model
    def _date_macro(self, value):
        if not isinstance(value, str) or not value.startswith('@'):
            return value
        today = fields.Date.context_today(self)
        now = fields.Datetime.now()
        m = re.fullmatch(r'@today(?:([+-])(\d+)d)?', value)
        if m:
            delta = int(m.group(2) or 0) * (1 if m.group(1) != '-' else -1)
            return fields.Date.to_string(today + timedelta(days=delta))
        if value == '@month_start':
            return fields.Date.to_string(today.replace(day=1))
        if value == '@year_start':
            return fields.Date.to_string(today.replace(month=1, day=1))
        if value == '@now':
            return fields.Datetime.to_string(now)
        return value

    @api.model
    def _clean_domain(self, model, domain):
        if domain in (None, False, ''):
            return []
        if not isinstance(domain, list):
            raise McpError(_('The domain must be a list.'))
        out = []
        for leaf in domain:
            if isinstance(leaf, str):
                if leaf not in ('&', '|', '!'):
                    raise McpError(_('Bad domain operator %s.', leaf))
                out.append(leaf)
                continue
            if not isinstance(leaf, (list, tuple)) or len(leaf) != 3:
                raise McpError(_('Each domain condition must be [field, operator, value].'))
            path, op, value = leaf
            if not isinstance(path, str) or not isinstance(op, str):
                raise McpError(_('Bad domain condition.'))
            op = op.lower()
            if op not in OPERATORS:
                raise McpError(_('Operator %s is not allowed.', op))
            self._check_path(model, path)
            if isinstance(value, list):
                value = [self._date_macro(v) for v in value]
            else:
                value = self._date_macro(value)
            out.append((path, op, value))
        return out

    @api.model
    def _check_path(self, model, path):
        current = model
        parts = path.split('.')
        for i, part in enumerate(parts):
            Model = self.env[current]
            part = part.split(':')[0]
            if part == 'id':
                continue
            field = Model._fields.get(part)
            if not field:
                raise McpError(_('Unknown field %(f)s on %(m)s.', f=part, m=current))
            if part in self._masks(current):
                raise McpError(_('Field %(f)s on %(m)s is masked and cannot be used here.', f=part, m=current), 'denied')
            if i < len(parts) - 1:
                if field.type not in REL_TYPES:
                    raise McpError(_('Field %s is not a relation.', part))
                current = field.comodel_name
                if self._blocked_model(current):
                    raise McpError(_('Model %s is not available through MCP.', current), 'denied')

    @api.model
    def _check_fields(self, model, names, for_write=False, allow_binary=False):
        masks = self._masks(model)
        Model = self.env[model]
        out = []
        for name in names:
            if not isinstance(name, str):
                raise McpError(_('Field names must be text.'))
            field = Model._fields.get(name)
            if not field:
                raise McpError(_('Unknown field %(f)s on %(m)s.', f=name, m=model))
            if masks.get(name) == 'hidden':
                raise McpError(_('Field %(f)s on %(m)s is masked.', f=name, m=model), 'denied')
            if for_write:
                if name in MAGIC or (not field.store and not getattr(field, 'inverse', None)):
                    raise McpError(_('Field %s cannot be written.', name))
            elif field.type == 'binary' and not allow_binary:
                raise McpError(_('Binary field %s cannot be read through MCP.', name))
            out.append(name)
        return out

    @api.model
    def _default_fields(self, model):
        Model = self.env[model]
        masks = self._masks(model)
        names = ['id']
        for name, field in Model._fields.items():
            if name in masks and masks[name] == 'hidden':
                continue
            if name in MAGIC or field.type in ('binary', 'html', 'properties', 'properties_definition', 'json'):
                continue
            if field.type in ('one2many', 'many2many'):
                continue
            if not field.store:
                continue
            if name.startswith('message_') or name.startswith('activity_') or name.startswith('website_'):
                continue
            names.append(name)
            if len(names) >= 25:
                break
        if 'display_name' not in names:
            names.insert(1, 'display_name')
        return names

    @api.model
    def _mask_value(self, mode, value):
        if value in (False, None, ''):
            return value
        if mode == 'hash':
            salt = self._secret()
            return 'h_' + hashlib.sha256(salt + str(value).encode()).hexdigest()[:10]
        text = str(value)
        if len(text) <= 6:
            return '***'
        return '%s***%s' % (text[:2], text[-2:])

    @api.model
    def _serialize(self, records, names, masks=None):
        """Read the records and return plain JSON data with masks applied."""
        model = records._name
        masks = masks if masks is not None else self._masks(model)
        wanted = [n for n in names if masks.get(n) != 'hidden']
        data = records.read(wanted) if records else []
        comask = {}
        for row in data:
            for name in list(row):
                field = records._fields[name]
                value = row[name]
                if name in masks and name != 'id':
                    row[name] = self._mask_value(masks[name], value)
                    continue
                if field.type == 'many2one' and value:
                    co = field.comodel_name
                    if co not in comask:
                        comask[co] = self._masks(co) if co in self.env and not self._blocked_model(co) else {}
                    cm = comask[co]
                    hit = [k for k in ('name', 'display_name', 'complete_name') if k in cm]
                    if hit:
                        row[name] = [value[0], self._mask_value(cm[hit[0]], value[1]) if cm[hit[0]] != 'hidden' else '***']
            if 'display_name' in row and 'display_name' in masks:
                row['display_name'] = self._mask_value(masks['display_name'], row['display_name'])
        return _jsonable(data)

    # ------------------------------------------------------------------
    # confirmation tokens
    # ------------------------------------------------------------------
    @api.model
    def _canonical(self, tool, args):
        clean = {k: v for k, v in (args or {}).items() if k not in ('confirm', 'dry_run')}
        return json.dumps(clean, sort_keys=True, default=str, separators=(',', ':'))

    @api.model
    def make_confirm_token(self, tool, args):
        expiry = int(time.time()) + 600
        payload = '%d|%d|%s|%s' % (expiry, self.env.uid, tool, self._canonical(tool, args))
        mac = hmac.new(self._secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        return '%d.%s' % (expiry, mac)

    @api.model
    def check_confirm_token(self, tool, args, token):
        if not token or not isinstance(token, str) or '.' not in token:
            return False
        try:
            expiry = int(token.split('.', 1)[0])
        except ValueError:
            return False
        if expiry < time.time():
            return False
        payload = '%d|%d|%s|%s' % (expiry, self.env.uid, tool, self._canonical(tool, args))
        mac = hmac.new(self._secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(mac, token.split('.', 1)[1])

    # ------------------------------------------------------------------
    # MCP protocol
    # ------------------------------------------------------------------
    @api.model
    def server_instructions(self):
        return (
            'This server gives access to live Odoo data under the signed-in user\'s own rights. '
            'Start with whoami, then list_models and describe_model before querying. Prefer aggregate over '
            'reading many rows. Writes are journaled and can be undone with undo_change. Some actions need '
            'confirmation or an approval: when a call returns status confirmation_required, show the preview to '
            'the user and repeat the call with the confirm token only after they agree. Masked fields are never '
            'returned.')

    @api.model
    def _rpc_error(self, mid, code, message):
        return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': code, 'message': message}}

    @api.model
    def handle_message(self, msg, ctx):
        """Process one JSON-RPC message. Returns a response dict or None (notification)."""
        if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0':
            return self._rpc_error(None, -32600, 'Invalid request')
        mid = msg.get('id')
        method = msg.get('method')
        params = msg.get('params') or {}
        if method is None:
            return None  # a response from the client, nothing to do
        if 'id' not in msg:
            return None  # notification
        if not isinstance(method, str) or not isinstance(params, dict):
            return self._rpc_error(mid, -32600, 'Invalid request')
        try:
            if method == 'initialize':
                asked = params.get('protocolVersion')
                version = asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
                ctx['client_name'] = ((params.get('clientInfo') or {}).get('name') or '')[:80]
                self._log(ctx, method='initialize', status='ok')
                return {'jsonrpc': '2.0', 'id': mid, 'result': {
                    'protocolVersion': version,
                    'capabilities': {'tools': {'listChanged': False}, 'resources': {'listChanged': False},
                                     'prompts': {'listChanged': False}},
                    'serverInfo': {'name': 'AXSync MCP Server for Odoo', 'version': '1.0.0'},
                    'instructions': self.server_instructions(),
                }}
            if method == 'ping':
                return {'jsonrpc': '2.0', 'id': mid, 'result': {}}
            if method == 'tools/list':
                return {'jsonrpc': '2.0', 'id': mid, 'result': {'tools': self.list_tools(ctx)}}
            if method == 'tools/call':
                name = params.get('name')
                if not isinstance(name, str):
                    return self._rpc_error(mid, -32602, 'Missing tool name')
                result = self.call_tool(name, params.get('arguments') or {}, ctx)
                text = json.dumps(result, default=str, ensure_ascii=False)
                limit = self.env['axsync.mcp.setting'].get().max_text_chars
                if limit and len(text) > limit:
                    text = json.dumps({
                        'status': 'error',
                        'message': 'The response was larger than %d characters. Ask for fewer fields or rows, '
                                   'or use aggregate.' % limit}, ensure_ascii=False)
                    result = {'status': 'error'}
                is_error = result.get('status') in ('error', 'denied')
                return {'jsonrpc': '2.0', 'id': mid, 'result': {
                    'content': [{'type': 'text', 'text': text}], 'isError': is_error}}
            if method == 'resources/list':
                return {'jsonrpc': '2.0', 'id': mid, 'result': {'resources': [
                    {'uri': 'odoo://help', 'name': 'help', 'title': 'How to use this server',
                     'mimeType': 'text/markdown'},
                    {'uri': 'odoo://models', 'name': 'models', 'title': 'Models you can read',
                     'mimeType': 'application/json'}]}}
            if method == 'resources/templates/list':
                return {'jsonrpc': '2.0', 'id': mid, 'result': {'resourceTemplates': [
                    {'uriTemplate': 'odoo://schema/{model}', 'name': 'schema', 'title': 'Model schema',
                     'mimeType': 'application/json'}]}}
            if method == 'resources/read':
                return self._read_resource(mid, params.get('uri'), ctx)
            if method == 'prompts/list':
                prompts = self.env['axsync.mcp.prompt'].sudo().search([])
                return {'jsonrpc': '2.0', 'id': mid, 'result': {'prompts': [{
                    'name': p.name, 'title': p.title, 'description': p.description or '',
                    'arguments': [{'name': a, 'required': False} for a in p.arguments()]} for p in prompts]}}
            if method == 'prompts/get':
                prompt = self.env['axsync.mcp.prompt'].sudo().search([('name', '=', params.get('name'))], limit=1)
                if not prompt:
                    return self._rpc_error(mid, -32602, 'Unknown prompt')
                return {'jsonrpc': '2.0', 'id': mid, 'result': {
                    'description': prompt.description or '',
                    'messages': [{'role': 'user', 'content': {
                        'type': 'text', 'text': prompt.render(params.get('arguments') or {})}}]}}
            if method in ('logging/setLevel', 'completion/complete'):
                return {'jsonrpc': '2.0', 'id': mid, 'result': {}}
            return self._rpc_error(mid, -32601, 'Method not found: %s' % method)
        except Exception:  # pylint: disable=broad-except
            _logger.exception('MCP protocol error')
            return self._rpc_error(mid, -32603, 'Internal error')

    @api.model
    def _read_resource(self, mid, uri, ctx):
        text, mime = None, 'application/json'
        if uri == 'odoo://help':
            text, mime = self.server_instructions(), 'text/markdown'
        elif uri == 'odoo://models':
            text = json.dumps(self._tool_list_models({}, ctx), default=str)
        elif isinstance(uri, str) and uri.startswith('odoo://schema/'):
            try:
                text = json.dumps(self._tool_describe_model({'model': uri[len('odoo://schema/'):]}, ctx), default=str)
            except McpError as exc:
                return self._rpc_error(mid, -32602, str(exc))
        if text is None:
            return self._rpc_error(mid, -32002, 'Resource not found')
        return {'jsonrpc': '2.0', 'id': mid, 'result': {'contents': [{'uri': uri, 'mimeType': mime, 'text': text}]}}

    @api.model
    def list_tools(self, ctx):
        enabled = {t.code for t in self.env['axsync.mcp.tool'].sudo().search([('enabled', '=', True)])}
        profiles = self._profiles(ctx)
        out = []
        for tool in TOOLS:
            if tool['name'] not in enabled:
                continue
            if any(not p.allows_tool(tool['name']) for p in profiles):
                continue
            out.append({
                'name': tool['name'], 'title': tool['title'], 'description': tool['description'],
                'inputSchema': tool['schema'],
                'annotations': {'readOnlyHint': tool['kind'] == 'read',
                                'destructiveHint': tool['name'] in ('delete_records', 'call_method'),
                                'openWorldHint': False},
            })
        return out

    @api.model
    def _profiles(self, ctx):
        token = self.env['axsync.mcp.token'].sudo().browse(ctx.get('token_id') or 0).exists()
        setting = self.env['axsync.mcp.setting'].get()
        return self.env['axsync.mcp.profile'].effective_for(
            self.env.user, token.profile_id, setting.default_profile_id)

    # ------------------------------------------------------------------
    # audit log
    # ------------------------------------------------------------------
    @api.model
    def _log(self, ctx, **vals):
        """Write the audit entry with its own cursor so that refusals survive a rollback."""
        try:
            args = vals.pop('arguments', None)
            row = {
                'user_id': self.env.uid, 'token_id': ctx.get('token_id') or False,
                'client_name': ctx.get('client_name'), 'ip': ctx.get('ip'),
                'arguments': json.dumps(_jsonable(args), default=str)[:4000] if args is not None else False,
            }
            row.update(vals)
            with self.env.registry.cursor() as cr:
                env = api.Environment(cr, 1, {})
                env['axsync.mcp.log'].create(row)
        except Exception:  # pylint: disable=broad-except
            _logger.exception('Could not write the MCP audit log')
