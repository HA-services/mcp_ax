# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from datetime import timedelta

from odoo import fields, http
from odoo.http import Response, request
from odoo.tools import consteq

HTML = [('Content-Type', 'text/html; charset=utf-8'), ('Cache-Control', 'no-store'),
        ('X-Robots-Tag', 'noindex, nofollow'), ('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; img-src data:")]


class McpPages(http.Controller):

    @http.route('/mcp/page/<string:slug>', type='http', auth='public', methods=['GET'], website=False, readonly=False,
                sitemap=False)
    def page(self, slug, **kw):
        env = request.env
        page = env['axsync.mcp.page'].sudo().search([('slug', '=', slug)], limit=1)
        if not page or not page.is_viewable() or not env['axsync.mcp.setting'].sudo().get().enabled:
            return Response('Not found', status=404)
        user = env.user
        if page.mode == 'link':
            viewer = page.owner_id
        else:
            if user._is_public():
                return request.redirect('/web/login?redirect=/mcp/page/%s' % slug)
            if page.mode == 'private' and user != page.owner_id:
                return Response('Forbidden', status=403)
            viewer = user
        if not viewer.active:
            return Response('Not found', status=404)
        html = page.sudo().render_html(viewer)
        env.cr.execute('UPDATE axsync_mcp_page SET view_count = COALESCE(view_count, 0) + 1 WHERE id = %s', [page.id])
        return Response(html, headers=HTML)

    @http.route('/mcp/download/<int:att_id>', type='http', auth='public', methods=['GET'], website=False, readonly=False,
                sitemap=False)
    def download(self, att_id, access_token=None, **kw):
        att = request.env['ir.attachment'].sudo().browse(att_id).exists()
        if not att or att.res_model != 'axsync.mcp.setting' or not access_token or not att.access_token \
                or not consteq(att.access_token, access_token):
            return Response('Not found', status=404)
        if att.create_date < fields.Datetime.now() - timedelta(hours=1):
            return Response('This link has expired.', status=410)
        name = att.name.replace('"', '')
        raw = att.raw
        if not isinstance(raw, (bytes, bytearray)):
            raw = raw.read() if hasattr(raw, 'read') else bytes(raw)
        return Response(bytes(raw), headers=[
            ('Content-Type', att.mimetype or 'application/octet-stream'),
            ('Content-Disposition', 'attachment; filename="%s"' % name),
            ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff')])
