import atexit
import os
import shutil
import tempfile

# Qt's GLib dispatcher takes over GLib's default main context for the main
# thread; keep Qt tests from interfering with GLib-based code in other tests.
os.environ.setdefault("QT_NO_GLIB", "1")
# UI texts are asserted in Turkish (the source language, CHANGE 8)
os.environ["OTO_EQ_DIL"] = "tr"

# Runtime state written by the code under test (cihaz.json, gorulen.json, …) must
# never land in the user's real ~/.local/state/oto-eq: one temp dir per test run,
# set before any oto_eq module computes config.STATE_DIR.
if not os.environ.get("OTO_EQ_TEST_STATE"):
    _state = tempfile.mkdtemp(prefix="oto-eq-test-state-")
    os.environ["OTO_EQ_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = _state
    atexit.register(shutil.rmtree, _state, True)
# The same for ~/.config/oto-eq (1 Kas 2026: a calibration test without a write_conf mock
# overwrote the user's REAL mikrofon.conf): config.CONFIG_DIR and every module constant
# derived from it (micgen.MIC_CONF, config.SINKS_CONF …) point into a temp dir. Reads fall
# back to the shipped defaults (config.config_path), so the tests stay deterministic.
if not os.environ.get("OTO_EQ_TEST_CONFIG"):
    _cfg = tempfile.mkdtemp(prefix="oto-eq-test-config-")
    os.environ["OTO_EQ_TEST_CONFIG"] = os.environ["XDG_CONFIG_HOME"] = _cfg
    atexit.register(shutil.rmtree, _cfg, True)
