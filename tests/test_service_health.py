import os
from unittest.mock import MagicMock

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from DivineService.service_health import serviceHealth


def test_check_connection_delegates_to_db_and_returns_true():
    db = MagicMock()
    db.test_connection.return_value = True
    svc = serviceHealth(db)
    assert svc.check_connection() is True
    db.test_connection.assert_called_once()


def test_check_connection_returns_false_when_db_unreachable():
    db = MagicMock()
    db.test_connection.return_value = False
    svc = serviceHealth(db)
    assert svc.check_connection() is False


def test_init_db_delegates_to_create_tables():
    db = MagicMock()
    svc = serviceHealth(db)
    svc.init_db()
    db.create_tables.assert_called_once()


def test_constructor_defaults_db_when_none_given():
    svc = serviceHealth()
    assert svc._db is not None
