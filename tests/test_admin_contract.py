import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _schema_fields(class_name: str):
    tree = ast.parse((ROOT / "services.py").read_text(encoding="utf-8"))
    schema = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return [
        node.target.id for node in schema.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ]


def test_admin_room_form_and_table_cover_schema_fields():
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    fields = _schema_fields("RoomCreateUpdateSchema")
    assert len(fields) == 24
    assert not [field for field in fields if not re.search(rf'id=["\']{field}["\']', html)]
    assert not [field for field in fields if not re.search(rf'r\.{field}\b', html)]


def test_admin_order_crud_routes_and_form_fields_exist():
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    for route in (
        '@app.get("/api/admin/orders")',
        '@app.post("/api/admin/orders")',
        '@app.put("/api/admin/orders/{order_id}")',
        '@app.delete("/api/admin/orders/{order_id}")',
    ):
        assert route in main_source
    for element_id in (
        "oCode", "oTenantPhone", "oTenantZalo",
        "oLandlordPhone", "oLandlordZalo", "oViewing",
    ):
        assert f'id="{element_id}"' in html
