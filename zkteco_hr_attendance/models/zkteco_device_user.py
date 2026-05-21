# Copyright (C) 2025 Ahmet Yiğit Budak (https://github.com/yibudak)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

ZKTECO_PRIVILEGE_ADMIN = 14


class ZKTecoDeviceUser(models.Model):
    _name = "zkteco.device.user"
    _description = "ZKTeco Device User"
    _order = "device_id, device_user_id"

    device_id = fields.Many2one(
        comodel_name="zkteco.device",
        required=True,
        ondelete="cascade",
    )
    device_user_id = fields.Char(
        string="Device User ID",
        required=True,
        help="External user ID employees punch with (pyzk User.user_id). "
        "On ZK6 firmware this must be numeric; ZK8 face devices accept text.",
    )
    zkteco_uid = fields.Integer(string="ZKTeco UID", readonly=True)
    name = fields.Char()
    privilege = fields.Selection(
        selection=[("0", "Normal"), ("14", "Admin")],
        default="0",
    )
    password = fields.Char()
    group_id = fields.Char(string="Group ID")
    card = fields.Integer(default=0)
    employee_id = fields.Many2one(
        comodel_name="hr.employee",
        help="Optional link to an Odoo employee for autofill. Not required.",
    )

    _sql_constraints = [
        (
            "unique_device_user",
            "UNIQUE(device_id, device_user_id)",
            "A user with this Device User ID already exists for this device.",
        )
    ]

    @api.onchange("employee_id")
    def _onchange_employee_id(self):
        """Autofill name and device user id from the linked employee."""
        for record in self:
            if record.employee_id:
                record.name = record.employee_id.name
                if record.employee_id.zkteco_user_id:
                    record.device_user_id = str(record.employee_id.zkteco_user_id)

    def _sync_from_device(self, device, zk_user):
        """Upsert one device-reported user into Odoo (no push back)."""
        vals = {
            "device_id": device.id,
            "device_user_id": str(zk_user.user_id),
            "zkteco_uid": zk_user.uid,
            "name": zk_user.name,
            "privilege": "14"
            if int(zk_user.privilege) >= ZKTECO_PRIVILEGE_ADMIN
            else "0",
            "password": zk_user.password or False,
            "group_id": zk_user.group_id or False,
            "card": int(zk_user.card or 0),
        }
        existing = self.search(
            [
                ("device_id", "=", device.id),
                ("device_user_id", "=", str(zk_user.user_id)),
            ],
            limit=1,
        )
        if existing:
            existing.with_context(zkteco_skip_push=True).write(vals)
            return existing
        return self.with_context(zkteco_skip_push=True).create(vals)

    def _push_to_device(self):
        """Push each record to its device via set_user (kill-switch gated)."""
        for record in self:
            device = record.device_id
            if not device.allow_user_sync:
                _logger.info(
                    "ZKTeco: user sync disabled on %s, skipping push of %s.",
                    device.name,
                    record.device_user_id,
                )
                continue
            try:
                conn = device._get_connection()
                # Prime next_uid / user_packet_size before set_user.
                conn.get_users()
                conn.set_user(
                    uid=record.zkteco_uid or None,
                    name=record.name or "",
                    privilege=int(record.privilege or 0),
                    password=record.password or "",
                    group_id=record.group_id or "",
                    user_id=record.device_user_id or "",
                    card=record.card or 0,
                )
                if not record.zkteco_uid:
                    pushed = next(
                        (
                            user
                            for user in conn.get_users()
                            if str(user.user_id) == record.device_user_id
                        ),
                        None,
                    )
                    if pushed:
                        record.with_context(
                            zkteco_skip_push=True
                        ).zkteco_uid = pushed.uid
                conn.disconnect()
                device.last_error = False
            except Exception as e:
                _logger.error("ZKTeco set_user failed: %s", str(e))
                device.last_error = str(e)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if not self.env.context.get("zkteco_skip_push"):
            records._push_to_device()
        return records

    def write(self, vals):
        res = super().write(vals)
        push_fields = {
            "name",
            "privilege",
            "password",
            "group_id",
            "card",
            "device_user_id",
        }
        if not self.env.context.get("zkteco_skip_push") and (push_fields & set(vals)):
            self._push_to_device()
        return res

    def unlink(self):
        if not self.env.context.get("zkteco_skip_push"):
            for record in self:
                device = record.device_id
                if not device.allow_user_sync:
                    continue
                try:
                    conn = device._get_connection()
                    conn.delete_user(
                        uid=record.zkteco_uid or 0,
                        user_id=record.device_user_id or "",
                    )
                    conn.disconnect()
                    device.last_error = False
                except Exception as e:
                    _logger.error("ZKTeco delete_user failed: %s", str(e))
                    device.last_error = str(e)
        return super().unlink()
