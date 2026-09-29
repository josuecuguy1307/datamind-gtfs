from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from datamind_console.views.sample_region_audit_view import _dsn


class SampleRegionAuditDsnTests(unittest.TestCase):
    def test_local_only_mode_never_uses_server_dsn(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DATAMIND_LOCAL_ONLY_MODE": "true",
                "DB_DSN": "postgresql://server.example/should-not-be-used",
                "LOCAL_DB_DSN": "",
                "DATAMIND_LOCAL_DB_DSN": "",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "Remote database fallbacks are disabled"):
                _dsn()

    def test_local_only_mode_uses_explicit_local_dsn(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DATAMIND_LOCAL_ONLY_MODE": "true",
                "DB_DSN": "postgresql://server.example/should-not-be-used",
                "LOCAL_DB_DSN": "postgresql://localhost/local_audit",
            },
            clear=False,
        ):
            self.assertEqual(_dsn(), "postgresql://localhost/local_audit")

