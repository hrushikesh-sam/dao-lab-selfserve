"""What does the code in the current tree do with the G-3/G-4 reverted fixtures?
Synthetic in-memory DBs only. dotenv is disabled first, as tests/conftest.py does."""
import sys
import dotenv
dotenv.load_dotenv = lambda *a, **k: False
sys.path.insert(0, sys.argv[1])
from service import migrations, models          # noqa: E402
import tests.test_migrations_guard as g          # noqa: E402

print(f"latest_version() = {migrations.latest_version()}; "
      f"has RevertedSchema: {hasattr(migrations, 'RevertedSchema')}")
for name, build, uv in [("reverted v12", g._reverted_v12, None),
                        ("reverted v13", g._reverted_v13, None),
                        ("reverted v13, user_version hand-set 11", g._reverted_v13, 11)]:
    conn = g._reverted_v13() if build is g._reverted_v13 else build()
    if uv is not None:
        conn.execute(f"PRAGMA user_version = {uv}")
    before = g._state(conn)
    try:
        applied = models.init_db(conn)
        out = f"ACCEPTED: init_db -> {applied}, user_version {migrations.current_version(conn)}"
    except Exception as e:
        out = f"{type(e).__name__}: {e}"
    print(f"{name}: {out}; DB unchanged: {g._state(conn) == before}")
