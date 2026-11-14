"""The database URL must survive any password, and DATABASE_URL must still override the POSTGRES_* parts."""
import os
import unittest
from unittest import mock

from sqlalchemy.engine import make_url

from infinity.config import build_database_url

NASTY = 'p@ss:w/rd#%?+ $x"y\\z\'q&=;'


class DatabaseUrlTest(unittest.TestCase):
    def test_special_characters_round_trip(self):
        env = {"POSTGRES_PASSWORD": NASTY, "POSTGRES_HOST": "postgres", "POSTGRES_PORT": "5432", "POSTGRES_USER": "infinity", "POSTGRES_DB": "infinity"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("DATABASE_URL", None)
            url = make_url(build_database_url())
        self.assertEqual(url.password, NASTY)
        self.assertEqual((url.username, url.host, url.port, url.database), ("infinity", "postgres", 5432, "infinity"))
        self.assertEqual(url.drivername, "postgresql+psycopg")

    def test_explicit_database_url_wins(self):
        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql+psycopg://u:p@h:1/d", "POSTGRES_PASSWORD": "ignored"}):
            self.assertEqual(build_database_url(), "postgresql+psycopg://u:p@h:1/d")


if __name__ == "__main__":
    unittest.main()
