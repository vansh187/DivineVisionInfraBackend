import os

# Must run before any test module imports DivineService/Divinepersistence/DivineAPI.
# pytest always imports conftest.py before collecting test files in this directory,
# which guarantees this fires first regardless of which test file happens to sort
# alphabetically first. Without this, whichever file is collected first and doesn't
# set these itself causes Divinepersistence.persistence_db's module-level `engine`
# (a singleton, created once per process on first import) to bind to the real
# DATABASE_URL from .env instead of the local sqlite test database - silently
# pointing every test in the session at the production database. This happened for
# real: adding a new test file that sorted before test_auth.py and didn't set these
# vars caused a full-suite run to write test signups into production.
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_db.sqlite")
os.environ.setdefault("JWT_SECRET_KEY", "testsecret")
