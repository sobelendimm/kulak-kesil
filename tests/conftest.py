def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: runs a real AppImage (needs KULAK_KESIL_E2E_APPIMAGE)")
