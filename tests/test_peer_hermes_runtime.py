"""Optional installed-Hermes interoperability, no live profile/config/database writes.

Set HERMES_RUNTIME_PYTHON to the --print-runtime-command interpreter and
HERMES_SOURCE_ROOT to that launcher's discovered source root. Uses actual Hermes
outbound HTTP/protocol helpers against an ephemeral Aerys TCP listener.
"""

import json
import os
import socket
import subprocess
import threading
import time

import pytest
import uvicorn
from test_peer_server import registry

from aerys_v2.a2a.task_store import ReceiptStore
from aerys_v2.transports.a2a_server import build_peer_app


def test_installed_hermes_outbound_reaches_isolated_aerys(tmp_path):
    runtime = os.environ.get("HERMES_RUNTIME_PYTHON")
    source = os.environ.get("HERMES_SOURCE_ROOT")
    if not runtime or not source:
        pytest.skip("optional installed Hermes runtime not supplied")

    async def execute(req):
        return "isolated-aerys:" + req.principal + ":" + req.text

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/"
    store = ReceiptStore(tmp_path / "receipts.db")
    app = build_peer_app(registry(), store, execute, url=url)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )

    # Construct store in the server thread: sqlite connections are thread-affine.
    def serve():
        server.config.app = build_peer_app(
            registry(), ReceiptStore(tmp_path / "server.db"), execute, url=url
        )
        server.run()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        script = "import sys,json; sys.path.insert(0,sys.argv[1]); import hermes_bootstrap; from plugins.platforms.a2a import tools,protocol; body={'jsonrpc':'2.0','id':'native-probe','method':'SendMessage','params':{'tenant':'aerys','message':protocol.text_message(protocol.ROLE_USER,'native-nonce',context_id='native-fixture')}}; print(json.dumps(tools._http_post_json(sys.argv[2],body,{'Authorization':'Bearer alice-token'},20)))"
        result = subprocess.run(
            [runtime, "-I", "-c", script, source, url],
            capture_output=True,
            text=True,
            timeout=40,
            check=True,
        )
        task = json.loads(result.stdout)["result"]["task"]
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        assert (
            task["artifacts"][0]["parts"][0]["text"]
            == "isolated-aerys:alice:native-nonce"
        )
    finally:
        server.should_exit = True
        thread.join(20)
        store.close()
        assert not thread.is_alive()
