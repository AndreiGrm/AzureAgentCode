"""Entry point Windows: avvia la dashboard in una finestra nativa."""
from __future__ import annotations

import threading
import time
from socket import AF_INET, SOCK_STREAM, socket

import uvicorn
import webview

from dashboard_server import app
from runtime_paths import resource_dir

HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}"


def dashboard_is_running() -> bool:
    with socket(AF_INET, SOCK_STREAM) as client:
        return client.connect_ex((HOST, PORT)) == 0


def wait_for_dashboard() -> None:
    for _ in range(100):
        if dashboard_is_running():
            return
        time.sleep(0.1)
    raise RuntimeError("La dashboard non si e' avviata sulla porta 8765.")


def main() -> None:
    if dashboard_is_running():
        return

    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))
    server_thread = threading.Thread(target=server.run, name="dashboard-server", daemon=True)
    server_thread.start()
    wait_for_dashboard()

    window = webview.create_window(
        "Azure DevOps Agent Dashboard",
        URL,
        width=1440,
        height=960,
        min_size=(1000, 700),
    )
    window.events.closed += lambda: setattr(server, "should_exit", True)
    webview.start(icon=str(resource_dir() / "dashboard.ico"))
    server_thread.join(timeout=10)


if __name__ == "__main__":
    main()
