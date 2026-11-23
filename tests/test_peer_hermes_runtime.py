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


def _forward(listen, target, stop):
    """Plain TCP forwarder: stands in for the T289-style SSH local forward."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", listen))
    srv.listen()
    srv.settimeout(0.2)

    def pipe(a, b):
        try:
            while data := a.recv(65536):
                b.sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    while not stop.is_set():
        try:
            c, _ = srv.accept()
        except TimeoutError:
            continue
        u = socket.create_connection(("127.0.0.1", target))
        threading.Thread(target=pipe, args=(c, u), daemon=True).start()
        threading.Thread(target=pipe, args=(u, c), daemon=True).start()
    srv.close()


HERMES_CALL = r"""
import json, re, sys
sys.path.insert(0, sys.argv[1])
import hermes_bootstrap
from plugins.platforms.a2a import tools
first = tools.a2a_call({"agent": "aerys", "message": "ping-one"})
ctx = re.search(r"context (\S+?)[ \]]", first).group(1)
second = tools.a2a_call({"agent": "aerys", "message": "ping-two", "context_id": ctx})
bad = tools.a2a_call({"agent": "aerys_bad", "message": "nope"})
print(json.dumps({"first": first, "second": second, "bad": bad, "ctx": ctx}))
"""


def test_installed_hermes_a2a_call_through_tunnel(tmp_path):
    """The real Hermes a2a_call path: card fetch -> card's JSONRPC URL -> tenant
    -> blocking SendMessage -> follow-up in Hermes's own context, all through a
    forwarder whose port differs from Aerys's bind port (the deployment shape).
    With public_url unset the card must advertise the caller's address."""
    runtime = os.environ.get("HERMES_RUNTIME_PYTHON")
    source = os.environ.get("HERMES_SOURCE_ROOT")
    if not runtime or not source:
        pytest.skip("optional installed Hermes runtime not supplied")

    seen = []

    async def execute(req):
        seen.append((req.principal, req.context_id, req.text))
        return f"AERYS_OK {req.text}"

    ports = []
    for _ in range(2):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            ports.append(sock.getsockname()[1])
    aerys_port, tunnel_port = ports
    server = uvicorn.Server(
        uvicorn.Config(None, host="127.0.0.1", port=aerys_port, log_level="error")
    )

    def serve():
        server.config.app = build_peer_app(
            registry(), ReceiptStore(tmp_path / "server.db"), execute, url=""
        )
        server.run()

    stop = threading.Event()
    thread = threading.Thread(target=serve)
    thread.start()
    threading.Thread(
        target=_forward, args=(tunnel_port, aerys_port, stop), daemon=True
    ).start()
    home = tmp_path / "hermes-home"  # isolated: no live Hermes config/state
    home.mkdir()
    (home / "config.yaml").write_text(
        "a2a_agents:\n"
        f"  aerys: {{url: 'http://127.0.0.1:{tunnel_port}', tenant: aerys, timeout: 60,"
        " auth: {type: bearer, token: alice-token}}\n"
        f"  aerys_bad: {{url: 'http://127.0.0.1:{tunnel_port}', timeout: 20,"
        " auth: {type: bearer, token: wrong-token}}\n"
    )
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES")}
        env.update(HERMES_HOME=str(home), NO_PROXY="*", no_proxy="*")
        result = subprocess.run(
            [runtime, "-I", "-c", HERMES_CALL, source],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
            env=env,
        )
        out = json.loads(result.stdout.strip().splitlines()[-1])
        assert "completed]\nAERYS_OK ping-one" in out["first"]
        assert "completed]\nAERYS_OK ping-two" in out["second"]
        assert "HTTP 401" in out["bad"]
        assert [s[1] for s in seen] == [out["ctx"], out["ctx"]]  # Hermes's context kept
        assert {s[0] for s in seen} == {"alice"}
    finally:
        stop.set()
        server.should_exit = True
        thread.join(20)
        assert not thread.is_alive()
