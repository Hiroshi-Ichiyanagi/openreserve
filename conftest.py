"""pytest rootdir marker — ensures the package root is on sys.path for tests."""


def pytest_configure(config):
    # The legacy reserve generators and verify_proof_with_node_prefix emit a
    # PendingDeprecationWarning on every call. Existing tests keep calling them on purpose;
    # tests/test_names_and_deprecation.py checks the warnings themselves with pytest.warns,
    # which records them whatever this filter says.
    config.addinivalue_line(
        "filterwarnings",
        r"ignore:.*will be deprecated.*:PendingDeprecationWarning",
    )
