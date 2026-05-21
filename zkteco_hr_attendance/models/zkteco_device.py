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
from datetime import datetime, timedelta

from zk import ZK

from odoo import _, fields, models

_logger = logging.getLogger(__name__)


class ZKTecoDevice(models.Model):
    _name = "zkteco.device"
    _description = "ZKTeco Device"

    name = fields.Char(string="Device Name", required=True)
    ip_address = fields.Char(string="IP Address", required=True)
    port = fields.Integer(required=True, default=4370)
    password = fields.Integer()
    state = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("connected", "Connected"),
            ("error", "Error"),
        ],
        default="draft",
    )
    last_poll_date = fields.Datetime(readonly=True)
    last_error = fields.Text(readonly=True)
    last_synced_punch = fields.Datetime(readonly=True)
    allow_user_sync = fields.Boolean(
        default=False,
        help="When off, creating/editing/deleting device users in Odoo stays "
        "local and is NOT pushed to the terminal. Fetch is always allowed. "
        "Keep off on test databases.",
    )
    user_count = fields.Integer(compute="_compute_user_count")

    def _compute_user_count(self):
        data = self.env["zkteco.device.user"].read_group(
            [("device_id", "in", self.ids)], ["device_id"], ["device_id"]
        )
        counts = {row["device_id"][0]: row["device_id_count"] for row in data}
        for device in self:
            device.user_count = counts.get(device.id, 0)

    def _get_connection(self):
        """Build and open a pyzk connection for this device."""
        self.ensure_one()
        zk = ZK(
            self.ip_address,
            port=self.port,
            timeout=5,
            password=self.password or 0,
            force_udp=False,
            ommit_ping=True,
        )
        return zk.connect()

    def action_open_log(self):
        """Open the device punch logs filtered to this device."""
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "zkteco_hr_attendance.action_zkteco_device_log"
        )
        action["domain"] = [("device_id", "=", self.id)]
        action["context"] = {
            "default_device_id": self.id,
            "search_default_device_id": self.id,
        }
        return action

    def action_open_users(self):
        """Open the device users filtered to this device."""
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "zkteco_hr_attendance.action_zkteco_device_user"
        )
        action["domain"] = [("device_id", "=", self.id)]
        action["context"] = {
            "default_device_id": self.id,
            "search_default_device_id": self.id,
        }
        return action

    def action_fetch_users(self):
        """Fetch the user list from the device into Odoo (read-only)."""
        self.ensure_one()
        DeviceUser = self.env["zkteco.device.user"]
        try:
            conn = self._get_connection()
            users = conn.get_users()
            conn.disconnect()
        except Exception as e:
            self.last_error = str(e)
            self.env["bus.bus"]._sendone(
                self.env.user.partner_id,
                "simple_notification",
                {
                    "type": "danger",
                    "message": _("User Fetch Failed: %s") % str(e),
                },
            )
            return
        self.last_error = False
        for zk_user in users:
            DeviceUser._sync_from_device(self, zk_user)
        self.env["bus.bus"]._sendone(
            self.env.user.partner_id,
            "simple_notification",
            {
                "type": "success",
                "message": _("%s users fetched") % len(users),
            },
        )

    def action_reset_watermark(self):
        """Clear the sync high-water mark so the next poll re-imports the full
        device buffer. Existing dedup prevents duplicate rows."""
        self.ensure_one()
        self.last_synced_punch = False
        self.env["bus.bus"]._sendone(
            self.env.user.partner_id,
            "simple_notification",
            {
                "type": "success",
                "message": _("Watermark reset; the next poll will re-import."),
            },
        )

    def action_test_connection(self):
        self.ensure_one()

        try:
            # Create a ZK instance with the device's IP address and port
            zk = ZK(
                self.ip_address,
                port=self.port,
                timeout=5,
                password=self.password or 0,
                force_udp=False,
                ommit_ping=True,
            )

            # Connect to the device
            conn = zk.connect()
            conn.disconnect()

            self.state = "connected"
            self.last_error = False
            # If connection is successful, return a success message
            self.env["bus.bus"]._sendone(
                self.env.user.partner_id,
                "simple_notification",
                {
                    "type": "success",
                    "message": _(
                        "Connection Successful",
                    ),
                },
            )
        except Exception as e:
            self.state = "error"
            self.last_error = str(e)
            # If there is an error, return an error message
            self.env["bus.bus"]._sendone(
                self.env.user.partner_id,
                "simple_notification",
                {
                    "type": "danger",
                    "message": _(
                        "Connection Failed: %s",
                    )
                    % str(e),
                },
            )

    def get_all_device_attendance(self):
        devices = self.search([("state", "=", "connected")])
        for device in devices:
            device.with_context(zkteco_from_cron=True).action_get_attendance()
        return True

    def action_get_attendance(self):
        self.ensure_one()
        HrAttandance = self.env["hr.attendance"]
        ZKTecoDeviceLog = self.env["zkteco.device.log"]
        self.last_poll_date = fields.Datetime.now()
        try:
            # Create a ZK instance with the device's IP address and port
            zk = ZK(
                self.ip_address,
                port=self.port,
                timeout=5,
                password=self.password or 0,
                force_udp=False,
                ommit_ping=True,
            )

            # Connect to the device
            conn = zk.connect()

            zktime = conn.get_time()
            system_time = datetime.now()
            difference = (system_time - zktime).total_seconds()
            attendance_records = conn.get_attendance()

            # Capture the high-water mark from raw device timestamps before the
            # loop mutates record.timestamp below.
            max_raw_timestamp = max(
                (record.timestamp for record in attendance_records),
                default=False,
            )

            # Process attendance records, skipping any already synced.
            for record in attendance_records:
                # Correct device clock drift to get the real punch time.
                adjusted_timestamp = record.timestamp + timedelta(seconds=difference)
                if (
                    self.last_synced_punch
                    and adjusted_timestamp < self.last_synced_punch
                ):
                    continue
                ZKTecoDeviceLog.create_log_record(data=record, device_id=self)
                record.timestamp = adjusted_timestamp
                HrAttandance._process_zkteco_attendance_data(self, record)

            conn.disconnect()
            self.state = "connected"
            self.last_error = False
            if max_raw_timestamp:
                self.last_synced_punch = max_raw_timestamp + timedelta(
                    seconds=difference
                )
            if not self.env.context.get("zkteco_from_cron"):
                self.env["bus.bus"]._sendone(
                    self.env.user.partner_id,
                    "simple_notification",
                    {
                        "type": "success",
                        "message": _("Attendance fetched successfully"),
                    },
                )
        except Exception as e:
            _logger.error("Error getting attendance: %s", str(e))
            self.state = "error"
            self.last_error = str(e)
            if not self.env.context.get("zkteco_from_cron"):
                self.env["bus.bus"]._sendone(
                    self.env.user.partner_id,
                    "simple_notification",
                    {
                        "type": "danger",
                        "message": _("Attendance Fetch Failed: %s") % str(e),
                    },
                )
