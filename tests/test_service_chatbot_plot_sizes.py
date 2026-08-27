from types import SimpleNamespace
from unittest.mock import MagicMock

from DivineService.service_chatbot import serviceChatbot


def _row(project_name, city, area_sqyd, area_sqmt, width, length, unit_count, available_count,
         unit_type="plot"):
    return SimpleNamespace(
        project_name=project_name, city=city, unit_type=unit_type,
        area_sqyd=area_sqyd, area_sqmt=area_sqmt, width_mtr=width, length_mtr=length,
        unit_count=unit_count, available_count=available_count,
    )


class FakeInventory:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def distinct_plot_sizes(self, project_name=None, unit_type=None):
        self.calls.append({"project_name": project_name, "unit_type": unit_type})
        return list(self.rows)


def _service(inventory):
    return serviceChatbot(
        persistence=MagicMock(), gemini=MagicMock(), groq=MagicMock(),
        zoho=None, loan_persistence=MagicMock(), inventory_persistence=inventory,
    )


def test_plot_size_options_counts_distinct_sizes_and_groups_by_project():
    inv = FakeInventory([
        _row("OPS Divine Greens", "Karnal", 131.43, 109.90, 9.14, 12.03, 12, 5),
        _row("OPS Divine Greens", "Karnal", 150.00, 125.42, 10.06, 12.46, 8, 2),
        _row("Suraksha Enclave", "Sonipat", 113.69, 95.06, 7.31, 13.0, 20, 9),
    ])
    svc = _service(inv)

    result = svc._tool_get_plot_size_options({})

    assert result["distinct_size_count"] == 3
    projects = {p["project_name"]: p for p in result["projects"]}
    assert set(projects) == {"OPS Divine Greens", "Suraksha Enclave"}
    assert len(projects["OPS Divine Greens"]["sizes"]) == 2
    ops_first = projects["OPS Divine Greens"]["sizes"][0]
    assert ops_first["area_sq_yd"] == 131.43
    assert ops_first["dimensions_mtr"] == "9.14 x 12.03 m"
    assert ops_first["available_units"] == 5
    assert inv.calls == [{"project_name": None, "unit_type": None}]


def test_plot_size_options_passes_project_filter_through():
    inv = FakeInventory([_row("OPS Divine Greens", "Karnal", 131.43, 109.9, None, None, 3, 1)])
    svc = _service(inv)

    result = svc._tool_get_plot_size_options({"project_name": "  OPS  "})

    assert inv.calls == [{"project_name": "OPS", "unit_type": None}]
    assert result["distinct_size_count"] == 1
    assert result["projects"][0]["sizes"][0]["dimensions_mtr"] is None


def test_plot_size_options_reports_empty_inventory():
    svc = _service(FakeInventory([]))
    result = svc._tool_get_plot_size_options({})
    assert result["distinct_size_count"] == 0
    assert result["projects"] == []
    assert "note" in result


def test_plot_size_options_when_inventory_unavailable():
    svc = _service(FakeInventory([]))
    svc._inventory = None
    assert svc._tool_get_plot_size_options({}) == {"error": "inventory_unavailable"}


def test_plot_size_options_swallows_persistence_error():
    inv = FakeInventory([])
    inv.distinct_plot_sizes = MagicMock(side_effect=RuntimeError("db down"))
    svc = _service(inv)
    assert svc._tool_get_plot_size_options({}) == {"error": "tool_failed"}


def test_execute_tool_dispatches_get_plot_size_options():
    inv = FakeInventory([_row("Suraksha Enclave", "Sonipat", 113.69, 95.06, 7.31, 13.0, 4, 4)])
    svc = _service(inv)
    session = SimpleNamespace(id="s1", lead_id="l1")

    result = svc._execute_tool(session, "get_plot_size_options", {})

    assert result["distinct_size_count"] == 1
