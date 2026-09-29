from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import phase1_nodes.datamind.services.openmaps_extractor.src.db.repo as phase1_repo


class Phase1DatabaseBoundaryTests(unittest.TestCase):
    def test_missing_local_dsn_fails_before_libpq_default_connection(self) -> None:
        with patch.object(phase1_repo, "DB_DSN", ""), patch.dict(
            os.environ, {"DATAMIND_LOCAL_ONLY_MODE": "true"}, clear=False
        ), patch.object(phase1_repo.psycopg2, "connect") as connect:
            with self.assertRaisesRegex(RuntimeError, "LOCAL_DB_DSN"):
                with phase1_repo.db_conn():
                    pass
            connect.assert_not_called()

