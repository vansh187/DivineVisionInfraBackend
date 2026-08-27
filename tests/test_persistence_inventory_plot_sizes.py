import os
import uuid

os.environ["DATABASE_URL"] = "sqlite:///./test_db.sqlite"
os.environ["JWT_SECRET_KEY"] = "testsecret"

from Divinepersistence.persistence_db import PersistenceDB
from Divinepersistence.persistence_inventory import persistenceInventory


def _seed(inv, project, city, unit_number, area_sqyd, area_sqmt, width, length, status="available"):
    inv.upsert_unit(
        id=str(uuid.uuid4()), project_name=project, city=city, unit_number=unit_number,
        unit_type="plot", width_mtr=width, length_mtr=length,
        area_sqmt=area_sqmt, area_sqyd=area_sqyd, status=status,
    )


def setup_module(module):
    db_file = os.path.join(os.getcwd(), "test_db.sqlite")
    try:
        if os.path.exists(db_file):
            os.remove(db_file)
    except Exception:
        pass
    PersistenceDB().create_tables()

    inv = persistenceInventory()
    _seed(inv, "OPS Divine Greens", "Karnal", "A-1", 131.43, 109.90, 9.14, 12.03)
    _seed(inv, "OPS Divine Greens", "Karnal", "A-2", 131.43, 109.90, 9.14, 12.03, status="sold")
    _seed(inv, "OPS Divine Greens", "Karnal", "B-1", 150.00, 125.42, 10.06, 12.46)
    _seed(inv, "Suraksha Enclave", "Sonipat", "C-1", 113.69, 95.06, 7.31, 13.0)


def test_distinct_plot_sizes_collapses_duplicates_and_counts_units():
    inv = persistenceInventory()
    rows = inv.distinct_plot_sizes()

    # 3 distinct (project, size) rows: OPS 131.43 (x2 units), OPS 150.00, Suraksha 113.69
    assert len(rows) == 3

    ops_131 = next(r for r in rows if r.project_name == "OPS Divine Greens" and float(r.area_sqyd) == 131.43)
    assert int(ops_131.unit_count) == 2
    assert int(ops_131.available_count) == 1  # one of the two is sold


def test_distinct_plot_sizes_filters_by_project():
    inv = persistenceInventory()
    rows = inv.distinct_plot_sizes(project_name="suraksha")
    assert len(rows) == 1
    assert rows[0].project_name == "Suraksha Enclave"
    assert float(rows[0].area_sqyd) == 113.69
