"""Framework-free modules import no Streamlit and do no I/O at import time."""
import importlib
import subprocess
import sys

BACKEND = ("ads_auth", "ads_auth.settings", "ads_auth.db", "ads_auth.models", "ads_auth.hashing", "ads_auth.policy",
           "ads_auth.store", "ads_auth.usage.events", "ads_auth.usage.reports", "ads_auth.maintenance",
           "ads_auth.preflight", "ads_auth.cli")


def test_backend_modules_do_not_import_streamlit():
    code = "import sys\n" + "".join(f"import {m}\n" for m in BACKEND) + "print('streamlit' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_import_does_no_io(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in BACKEND + ("ads_auth.ui", "ads_auth.ui.gate", "ads_auth.ui.account", "ads_auth.ui.reports", "ads_auth.ui.about"):
        importlib.import_module(name)
    assert not list(tmp_path.iterdir())
