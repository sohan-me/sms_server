"""Regression test: a registered device must authenticate regardless of the
casing used when its fingerprint was typed into the admin panel or re-reported
by the PC.

The original matcher compared signatures byte-exactly, so a lowercase MAC or an
uppercase GUID made an already-registered device return 404 "Device not found".
"""
import os
import sys
import unittest

os.environ.setdefault("SECRET_KEY", "conformance-secret")
os.environ.pop("REDIS_URL", None)
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db  # noqa: E402
from models import AdminUser, DeviceUser  # noqa: E402

MAC = "ORBIT3SMW93W6EUMHCAXYPGSY"
GUID = "a8853b29-b587-442f-a6eb-3c3d4e95c90c"


class CaseInsensitiveFingerprintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        with app.app_context():
            db.create_all()
            DeviceUser.query.delete()
            db.session.commit()
            db.session.add(
                DeviceUser(
                    name="RINKU Zbook",
                    mac_address=MAC,
                    motherboard_serial="",
                    machine_guid=GUID,
                    bios_serial="",
                    is_active=True,
                )
            )
            db.session.commit()
        cls.client = app.test_client()

    def auth(self, payload):
        return self.client.post("/api/authenticate", json=payload)

    def test_exact_casing_still_authenticates(self):
        r = self.auth({"mac_address": MAC, "machine_guid": GUID})
        self.assertEqual(r.status_code, 200)

    def test_lowercase_mac_matches_stored_uppercase(self):
        r = self.auth({"mac_address": MAC.lower(), "machine_guid": GUID})
        self.assertEqual(
            r.status_code, 200, f"lowercase MAC rejected: {r.get_json()}"
        )

    def test_uppercase_guid_matches_stored_lowercase(self):
        r = self.auth({"mac_address": MAC, "machine_guid": GUID.upper()})
        self.assertEqual(
            r.status_code, 200, f"uppercase GUID rejected: {r.get_json()}"
        )

    def test_all_fields_lowercase(self):
        r = self.auth({"mac_address": MAC.lower(), "machine_guid": GUID.lower()})
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_all_fields_uppercase(self):
        r = self.auth({"mac_address": MAC.upper(), "machine_guid": GUID.upper()})
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_device_stored_lowercase_matches_upper_input(self):
        """Stored value casing must not matter either."""
        with app.app_context():
            u = DeviceUser(
                name="lower stored",
                mac_address="orbitlower000000000001",
                motherboard_serial="",
                machine_guid="abc-def-lower-guid-0001",
                bios_serial="",
                is_active=True,
            )
            db.session.add(u)
            db.session.commit()
        r = self.auth(
            {
                "mac_address": "ORBITLOWER000000000001",
                "machine_guid": "ABC-DEF-LOWER-GUID-0001",
            }
        )
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_surrounding_whitespace_tolerated(self):
        r = self.auth({"mac_address": f"  {MAC}  ", "machine_guid": GUID})
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_single_field_still_rejected(self):
        """The 2-of-4 rule must not be weakened by the fix."""
        r = self.auth({"mac_address": MAC})
        self.assertEqual(r.status_code, 400)

    def test_genuinely_unknown_device_still_404s(self):
        r = self.auth(
            {
                "mac_address": "ORBITUNKNOWN0000000001",
                "machine_guid": "00000000-0000-0000-0000-000000000000",
            }
        )
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
