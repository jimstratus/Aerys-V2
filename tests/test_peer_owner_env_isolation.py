"""The peer listener must never read the owner's .env (explicit-config-only).

Review finding on PR #3: importing the shared graph code (factory -> house_names)
read `.env` at import time when AERYS_HOUSE_NAMES was absent. Run the real
peer entry path in a fresh interpreter whose cwd holds a sentinel .env, and
record every file open with an audit hook.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBE = textwrap.dedent(
    """
    import json, sys
    opened = []
    sys.addaudithook(lambda ev, args: ev == "open" and opened.append(str(args[0])))
    if sys.argv[1] == "isolated":
        from aerys_v2.a2a.config import isolate_from_owner_environment
        isolate_from_owner_environment()
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from aerys_v2.a2a.contracts import PeerRequest
    from aerys_v2.a2a.executor import PeerAssistant
    req = PeerRequest(principal="p", task_id="t", context_id="c", thread_id="peer:p:c", text="hi")
    PeerAssistant(FakeListChatModel(responses=["ok"]))._ask(req)
    print(json.dumps([p for p in opened if p.endswith(".env")]))
    """
)


def _opened_env_files(tmp_path, mode):
    (tmp_path / ".env").write_text('AERYS_HOUSE_NAMES={"wife": "SENTINEL"}\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith("AERYS_")}
    env["PYTHONPATH"] = str(ROOT / "src")
    out = subprocess.run(
        [sys.executable, "-c", PROBE, mode],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_peer_launcher_isolation_never_opens_owner_env(tmp_path):
    assert _opened_env_files(tmp_path, "isolated") == []


def test_negative_control_unisolated_import_does_read_env(tmp_path):
    # Proves the probe can see the leak, so the test above is meaningful.
    assert _opened_env_files(tmp_path, "raw") == [".env"]


def test_main_isolates_before_creating_app(tmp_path, monkeypatch):
    import aerys_v2.a2a.config as cfg

    calls = []
    monkeypatch.setattr(
        cfg, "isolate_from_owner_environment", lambda: calls.append("isolate")
    )
    monkeypatch.setattr(
        cfg.PeerSettings,
        "model_validate_json",
        classmethod(
            lambda c, _: calls.append("parse") or (_ for _ in ()).throw(ValueError())
        ),
    )
    (tmp_path / "c.json").write_text("{}")
    monkeypatch.setattr(
        "sys.argv", ["aerys-v2-a2a", "--config", str(tmp_path / "c.json")]
    )
    try:
        cfg.main()
    except SystemExit:
        pass
    assert calls[:2] == ["isolate", "parse"]
