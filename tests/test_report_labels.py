from service.report import source_label


def test_source_labels_never_name_mcp():
    for kind in ("mcp:grossrat", "mcp:opencaselaw", "mcp:data-bs", "mcp:statistik", "mcp:unknown", "web"):
        assert "mcp" not in source_label(kind).lower()
        assert "mcp" not in source_label(kind, de=False).lower()
    assert source_label("mcp:grossrat").startswith("Grosser Rat Basel-Stadt")
