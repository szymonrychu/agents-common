import agents_common


def test_package_imports_and_reports_version():
    assert agents_common.__version__ == "0.1.0"
