import pytest
from sqlalchemy import create_engine, inspect, text

from assay import migrations, store


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'migrations_test.db'}")
    return eng


def test_schema_migrations_table_creation(engine):
    migrations.ensure_migrations_table(engine)
    insp = inspect(engine)
    assert insp.has_table("schema_migrations")
    assert migrations.get_applied_versions(engine) == []


def test_migration_up_and_down_lifecycle(engine):
    migrations.ensure_migrations_table(engine)

    # Create dummy table to test column rename migration
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE test_sample (id INTEGER PRIMARY KEY, old_name TEXT)"))
        conn.execute(text("INSERT INTO test_sample (id, old_name) VALUES (1, 'Alice')"))

    # Register test migration
    test_m = migrations.Migration(
        version="20261020_99_test_rename",
        name="Rename old_name to new_name",
        up=lambda conn: migrations.rename_column(conn, "test_sample", "old_name", "new_name"),
        down=lambda conn: migrations.rename_column(conn, "test_sample", "new_name", "old_name"),
    )

    # 1. Apply migration
    applied = migrations.apply_migration(engine, test_m)
    assert applied is True
    assert "20261020_99_test_rename" in migrations.get_applied_versions(engine)

    # Verify column was renamed
    cols = {c["name"] for c in inspect(engine).get_columns("test_sample")}
    assert "new_name" in cols
    assert "old_name" not in cols

    # Re-applying should be a no-op (idempotent)
    assert migrations.apply_migration(engine, test_m) is False

    # 2. Rollback migration
    rolled_back = migrations.rollback_migration(engine, test_m)
    assert rolled_back is True
    assert "20261020_99_test_rename" not in migrations.get_applied_versions(engine)

    # Verify column was renamed back
    cols_after_down = {c["name"] for c in inspect(engine).get_columns("test_sample")}
    assert "old_name" in cols_after_down
    assert "new_name" not in cols_after_down


def test_store_upgrade_runs_migrations_and_creates_tracking(engine):
    store.upgrade(engine)
    insp = inspect(engine)
    assert insp.has_table("schema_migrations")
    applied = migrations.get_applied_versions(engine)
    assert "20260901_00_initial_schema" in applied
