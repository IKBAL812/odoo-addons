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
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Longest plausible single shift including overtime. A punch beyond this gap
# from an open check-in is treated as a forgotten check-out, not a shift close.
# Commit 6 may promote this to a res.config.settings field (keep as fallback).
MAX_SHIFT_GAP_HOURS = 14


class HrAttendance(models.Model):
    _inherit = "hr.attendance"

    zkteco_entry_device_id = fields.Many2one(
        "zkteco.device",
        string="Entry Device",
    )

    zkteco_exit_device_id = fields.Many2one(
        "zkteco.device",
        string="Exit Device",
    )

    zkteco_entry_uid = fields.Integer(
        string="ZKTeco UID",
        help="Unique identifier for the ZKTeco device records.",
    )

    zkteco_exit_uid = fields.Integer(
        string="ZKTeco Exit UID",
        help="Unique identifier for the ZKTeco exit records.",
    )

    attendance_day = fields.Date(
        compute="_compute_attendance_day",
        store=True,
    )

    zkteco_entry_punch = fields.Integer(
        string="ZKTeco Entry Punch",
        help="Raw 'punch' value reported by the device for the check-in "
        "scan. Audit-only: not used in pairing logic.",
    )

    zkteco_exit_punch = fields.Integer(
        string="ZKTeco Exit Punch",
        help="Raw 'punch' value reported by the device for the check-out "
        "scan. Audit-only: not used in pairing logic.",
    )

    zkteco_entry_status = fields.Integer(
        string="ZKTeco Entry Status",
        help="Raw 'status' value reported by the device for the check-in "
        "scan. Audit-only: not used in pairing logic.",
    )

    zkteco_exit_status = fields.Integer(
        string="ZKTeco Exit Status",
        help="Raw 'status' value reported by the device for the check-out "
        "scan. Audit-only: not used in pairing logic.",
    )

    is_dangling = fields.Boolean(
        string="Dangling Attendance",
        default=False,
        index=True,
        help="Set when this record was auto-closed because the employee "
        "never scanned a check-out. Cleared automatically when a real "
        "check-out is recorded.",
    )

    @api.depends("check_in", "check_out")
    def _compute_attendance_day(self):
        """
        Compute the attendance day based on check_in and check_out timestamps.
        If check_in is set, attendance_day is set to the date of check_in.
        If check_out is set, attendance_day is set to the date of check_out.
        """
        for record in self:
            if record.check_in:
                record.attendance_day = record.check_in.date()
            elif record.check_out:
                record.attendance_day = record.check_out.date()
            else:
                record.attendance_day = False

    def _check_zkteco_attendance_duplicate(self, employee_id, data):
        exist_record = self.search(
            [
                "|",
                ("zkteco_entry_uid", "=", data.uid),
                ("zkteco_exit_uid", "=", data.uid),
            ],
            limit=1,
        )
        if exist_record:
            # The record already exists, do nothing.
            return True

        # Search for existing attendance records for this employee and device
        # where check_in or check_out is within ±5 minutes of the current timestamp
        time_window_start = data.timestamp - timedelta(minutes=5)
        time_window_end = data.timestamp + timedelta(minutes=5)
        exist_record = self.search(
            [
                ("employee_id", "=", employee_id.id),
                "|",
                "&",
                ("check_in", ">=", time_window_start),
                ("check_in", "<=", time_window_end),
                "&",
                ("check_out", ">=", time_window_start),
                ("check_out", "<=", time_window_end),
                "|",
                ("zkteco_entry_uid", "=", data.uid),
                ("zkteco_exit_uid", "=", data.uid),
            ],
            limit=1,
        )
        if exist_record:
            return True

    def _process_zkteco_attendance_data(self, device_id, data):
        """
        Process a single ZKTeco punch and maintain hr.attendance pairs.

        Pairing is device-independent and does NOT branch on ``data.punch``:
        face-only terminals frequently emit punch=0 for every scan, so the
        raw ``punch``/``status`` values are captured into audit fields only.

        Logic:
          * Resolve the employee; skip duplicates.
          * Find the employee's most recent OPEN record (no check_out),
            regardless of day -- this is what makes night shifts work.
          * If an open record exists and the new punch is within
            ``MAX_SHIFT_GAP_HOURS`` of its check_in, the punch CLOSES it.
          * If the gap is larger, the open record is a forgotten
            check-out: auto-close it at zero duration, flag it
            ``is_dangling``, then open a fresh check-in for this punch.
          * If no open record exists, open a new check-in.

        Known limitation: with three scans (in / out / accidental re-scan),
        the third opens a new record that dangles until a later punch.
        This is inherent to elapsed-time pairing without a trustworthy
        device direction flag.

        :param device_id: zkteco.device recordset (single).
        :param data: pyzk Attendance object (.user_id, .timestamp,
            .status, .punch, .uid); ``timestamp`` is already drift-corrected.
        :return: True if a record was created or updated, False otherwise.
        """
        employee_id = self.env["hr.employee"].search(
            [("zkteco_user_id", "=", int(data.user_id))], limit=1
        )
        if not employee_id:
            return False

        if self._check_zkteco_attendance_duplicate(employee_id, data):
            # If a duplicate record exists, do not create a new one
            return False

        open_record = self.search(
            [
                ("employee_id", "=", employee_id.id),
                ("check_in", "!=", False),
                ("check_out", "=", False),
            ],
            order="check_in desc",
            limit=1,
        )

        if open_record:
            if data.timestamp <= open_record.check_in:
                # Punches are processed chronologically (watermarked), so a
                # punch at or before the open check-in should not happen.
                _logger.warning(
                    "ZKTeco: punch %s at %s for employee %s is not after "
                    "the open check-in at %s; skipping to avoid corrupting "
                    "data.",
                    data.uid,
                    data.timestamp,
                    employee_id.id,
                    open_record.check_in,
                )
                return False

            gap = data.timestamp - open_record.check_in
            if gap <= timedelta(hours=MAX_SHIFT_GAP_HOURS):
                # Normal pairing: this punch closes the open shift
                # (covers midnight-crossing night shifts).
                open_record.write(
                    {
                        "check_out": data.timestamp,
                        "zkteco_exit_device_id": device_id.id,
                        "zkteco_exit_uid": data.uid,
                        "zkteco_exit_punch": data.punch,
                        "zkteco_exit_status": data.status,
                    }
                )
                return True

            # Forgotten check-out: auto-close the stale record at zero
            # duration and flag it BEFORE opening a new one, so the core
            # `_check_validity` constraint never sees two open records.
            open_record.write(
                {
                    "check_out": open_record.check_in,
                    "is_dangling": True,
                }
            )
            open_record.flush_recordset(["check_out", "is_dangling"])

        self.create(
            {
                "employee_id": employee_id.id,
                "check_in": data.timestamp,
                "zkteco_entry_device_id": device_id.id,
                "zkteco_entry_uid": data.uid,
                "zkteco_entry_punch": data.punch,
                "zkteco_entry_status": data.status,
            }
        )
        return True

    def write(self, vals):
        """
        Auto-clear ``is_dangling`` when a real check-out is recorded.

        When HR corrects an auto-closed record by giving it a genuine
        check_out (different from check_in), the dangling flag is no
        longer meaningful and is cleared. The inner write targets only
        ``is_dangling`` via ``super()`` so it cannot recurse.
        """
        res = super().write(vals)
        if "check_out" in vals:
            resolved = self.filtered(
                lambda a: a.is_dangling and a.check_out and a.check_out != a.check_in
            )
            if resolved:
                super(HrAttendance, resolved).write({"is_dangling": False})
        return res
