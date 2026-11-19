# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import io
import keyword
import re
import zipfile
from xml.sax.saxutils import escape

import odoo
from odoo import _, api, models

from .engine import McpError

TECH_RE = re.compile(r'^[a-z][a-z0-9_]{2,40}$')
MODEL_RE = re.compile(r'^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$')
FIELD_RE = re.compile(r'^[a-z][a-z0-9_]{0,40}$')
SEL_RE = re.compile(r'^[a-z0-9_]{1,30}$')
RESERVED_FIELDS = {'id', 'create_date', 'create_uid', 'write_date', 'write_uid', 'display_name', 'active_test'}
TYPES = {'char': 'Char', 'text': 'Text', 'integer': 'Integer', 'float': 'Float', 'boolean': 'Boolean',
         'date': 'Date', 'datetime': 'Datetime', 'selection': 'Selection', 'many2one': 'Many2one',
         'html': 'Html'}
MAX_MODELS, MAX_FIELDS = 8, 40


class McpEngineBuilder(models.AbstractModel):
    _inherit = 'axsync.mcp.engine'

    @api.model
    def _tool_build_module(self, args, ctx):
        tech = args.get('technical_name')
        title = str(args.get('title') or '').strip()[:60]
        summary = str(args.get('summary') or '').strip()[:200]
        if not isinstance(tech, str) or not TECH_RE.match(tech):
            raise McpError(_('technical_name must be lowercase letters, digits and underscores, 3 to 41 characters.'))
        if not title:
            raise McpError(_('title is required.'))
        specs = args.get('models')
        if not isinstance(specs, list) or not specs or len(specs) > MAX_MODELS:
            raise McpError(_('Give between 1 and %d models.', MAX_MODELS))
        models_clean = self._validate_specs(specs)
        files = self._render_module(tech, title, summary, models_clean)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for path, content in files.items():
                zf.writestr('%s/%s' % (tech, path), content)
        att = self.env['ir.attachment'].sudo().create({
            'name': '%s.zip' % tech, 'raw': buf.getvalue(), 'mimetype': 'application/zip',
            'res_model': 'axsync.mcp.setting', 'res_id': 0,
        })
        token = att.generate_access_token()[0]
        return {'module': tech, 'files': sorted(files),
                'download_url': '%s/mcp/download/%d?access_token=%s' % (self.base_url(), att.id, token),
                'odoo_version': odoo.release.series,
                'note': _('Nothing was installed. Unzip into an addons path, update the apps list and install it.')}

    @api.model
    def _validate_specs(self, specs):
        names = [s.get('name') for s in specs if isinstance(s, dict)]
        out = []
        for spec in specs:
            if not isinstance(spec, dict):
                raise McpError(_('Each model must be an object.'))
            name = spec.get('name')
            if not isinstance(name, str) or not MODEL_RE.match(name):
                raise McpError(_('Bad model name %s. Use the form vendor.thing.', name))
            if name in self.env:
                raise McpError(_('The model %s already exists in this database.', name))
            if name.replace('.', '_') in [o['name'].replace('.', '_') for o in out]:
                raise McpError(_('Model names must stay unique once dots become underscores.'))
            fields_in = spec.get('fields') or []
            if not isinstance(fields_in, list) or len(fields_in) > MAX_FIELDS:
                raise McpError(_('A model can have at most %d fields.', MAX_FIELDS))
            fields_out, seen = [], set()
            for f in fields_in:
                if not isinstance(f, dict):
                    raise McpError(_('Each field must be an object.'))
                fname, ftype = f.get('name'), f.get('type')
                if (not isinstance(fname, str) or not FIELD_RE.match(fname) or fname in RESERVED_FIELDS
                        or keyword.iskeyword(fname) or hasattr(models.Model, fname)):
                    raise McpError(_('Bad field name %s.', fname))
                if fname in seen:
                    raise McpError(_('Duplicate field %s.', fname))
                seen.add(fname)
                if ftype not in TYPES:
                    raise McpError(_('Field type %s is not supported.', ftype))
                entry = {'name': fname, 'type': ftype,
                         'string': str(f.get('string') or fname.replace('_', ' ').title())[:60],
                         'required': bool(f.get('required'))}
                if ftype == 'selection':
                    opts = f.get('selection')
                    if not isinstance(opts, list) or not opts or len(opts) > 20 or not all(
                            isinstance(o, str) and SEL_RE.match(o) for o in opts):
                        raise McpError(_('selection needs 1 to 20 lowercase option codes.'))
                    entry['selection'] = opts
                if ftype == 'many2one':
                    rel = f.get('relation')
                    if not isinstance(rel, str) or not MODEL_RE.match(rel) or not (rel in self.env or rel in names):
                        raise McpError(_('Field %s needs a relation to an existing model or to a model in this spec.', fname))
                    if self._blocked_model(rel) and rel not in names:
                        raise McpError(_('Relation %s is not allowed.', rel))
                    entry['relation'] = rel
                fields_out.append(entry)
            if 'name' not in seen:
                fields_out.insert(0, {'name': 'name', 'type': 'char', 'string': 'Name', 'required': True})
            out.append({'name': name, 'description': str(spec.get('description') or name)[:80],
                        'fields': fields_out})
        return out

    @api.model
    def _render_module(self, tech, title, summary, specs):
        major = odoo.release.version_info[0]
        list_tag = 'list' if major >= 18 else 'tree'
        view_mode = 'list,form' if major >= 18 else 'tree,form'
        files, data_files = {}, []
        py_imports, access_rows, views, menus = [], [], [], []
        for spec in specs:
            name = spec['name']
            ident = name.replace('.', '_')
            cls = ''.join(p.capitalize() for p in ident.split('_'))
            lines = ['# -*- coding: utf-8 -*-', 'from odoo import fields, models', '', '',
                     'class %s(models.Model):' % cls, '    _name = %r' % name,
                     '    _description = %r' % spec['description'], '']
            for f in spec['fields']:
                kw = ['string=%r' % f['string']]
                if f['required']:
                    kw.append('required=True')
                if f['type'] == 'selection':
                    kw.insert(0, repr([(o, o.replace('_', ' ').title()) for o in f['selection']]))
                if f['type'] == 'many2one':
                    kw.insert(0, repr(f['relation']))
                lines.append('    %s = fields.%s(%s)' % (f['name'], TYPES[f['type']], ', '.join(kw)))
            files['models/%s.py' % ident] = '\n'.join(lines) + '\n'
            py_imports.append('from . import %s' % ident)
            access_rows.append((ident, name))
            shown = [f['name'] for f in spec['fields'] if f['type'] not in ('text', 'html')][:6]
            form = [f['name'] for f in spec['fields']]
            views.append(
                '    <record id="view_%(i)s_list" model="ir.ui.view">\n'
                '        <field name="name">%(n)s.list</field>\n        <field name="model">%(n)s</field>\n'
                '        <field name="arch" type="xml">\n            <%(t)s>\n%(lf)s            </%(t)s>\n'
                '        </field>\n    </record>\n'
                '    <record id="view_%(i)s_form" model="ir.ui.view">\n'
                '        <field name="name">%(n)s.form</field>\n        <field name="model">%(n)s</field>\n'
                '        <field name="arch" type="xml">\n            <form>\n                <sheet>\n'
                '                    <group>\n%(ff)s                    </group>\n                </sheet>\n'
                '            </form>\n        </field>\n    </record>\n'
                '    <record id="action_%(i)s" model="ir.actions.act_window">\n'
                '        <field name="name">%(d)s</field>\n        <field name="res_model">%(n)s</field>\n'
                '        <field name="view_mode">%(vm)s</field>\n    </record>\n' % {
                    'i': ident, 'n': escape(name), 't': list_tag, 'd': escape(spec['description']), 'vm': view_mode,
                    'lf': ''.join('                <field name="%s"/>\n' % n for n in shown),
                    'ff': ''.join('                        <field name="%s"/>\n' % n for n in form)})
            menus.append('    <menuitem id="menu_%s" name="%s" parent="menu_%s_root" action="action_%s"/>\n' % (
                ident, escape(spec['description']), tech, ident))
        files['__init__.py'] = 'from . import models\n'
        files['models/__init__.py'] = '\n'.join(py_imports) + '\n'
        files['views/views.xml'] = (
            '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n' + ''.join(views) +
            '    <menuitem id="menu_%s_root" name="%s"/>\n' % (tech, escape(title)) + ''.join(menus) + '</odoo>\n')
        data_files.append('views/views.xml')
        if major >= 20:
            sec = 'security/ir.access.csv'
            rows = ['id,name,model_id,group_id/id,operation,domain']
            for ident, name in access_rows:
                rows.append('access_%s_user,%s user,%s,base.group_user,crud,' % (ident, name, name))
        else:
            sec = 'security/ir.model.access.csv'
            rows = ['id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink']
            for ident, name in access_rows:
                rows.append('access_%s_user,%s user,model_%s,base.group_user,1,1,1,1' % (ident, name, ident))
        files[sec] = '\n'.join(rows) + '\n'
        data_files.insert(0, sec)
        files['__manifest__.py'] = (
            "# -*- coding: utf-8 -*-\n{\n    'name': %r,\n    'summary': %r,\n    'version': '%d.0.1.0.0',\n"
            "    'category': 'Customizations',\n    'author': 'Generated with AXSync MCP Server',\n"
            "    'license': 'LGPL-3',\n    'depends': ['base'],\n    'data': %r,\n    'installable': True,\n"
            "    'application': True,\n}\n" % (title, summary or title, major, data_files))
        return files
