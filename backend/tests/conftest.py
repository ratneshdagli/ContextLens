"""Test bootstrap: isolate the DB and disable the scheduler BEFORE any backend module is imported."""

import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="contextlens-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["DISABLE_SCHEDULER"] = "1"
os.environ["CAMOFOX_URL"] = ""                             # nor drive the real stealth browser
os.environ["CLOUD_FALLBACK"] = "0"                        # tests must never spend real cloud-provider quota (test_cloud re-enables it with fakes)
os.environ["DISABLE_LMS"] = "1"                           # never let tests load/unload real models through the `lms` CLI
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9/v1"       # nothing listens here → LLM "offline" in tests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ["EMBED_FALLBACK"] = "0"
