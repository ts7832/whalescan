import whalecore


def test_extension_imports_and_reports_version():
    assert whalecore.version() == "0.1.0"
