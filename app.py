import os
import signal
import socket
import sys
import threading
import time

import requests
import uvicorn
import webview


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, port):
        self.port = port
        self.config = uvicorn.Config(
            "api:app", host="127.0.0.1", port=port, log_level="warning"
        )
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)

    def wait_until_ready(self, timeout=10):
        url = f"http://127.0.0.1:{self.port}/api/config"
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = requests.get(url, timeout=1)
                if resp.status_code < 500:
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False


def resource_path(relative):
    """Resolve paths that work both in dev and inside a py2app bundle."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        # py2app puts resources in ../Resources relative to the executable
        resources = os.path.join(base, "..", "Resources")
        path = os.path.join(resources, relative)
        if os.path.exists(path):
            return path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative)


def main():
    # Ensure the working directory is correct so api module and .env are found
    os.chdir(os.path.dirname(os.path.abspath(resource_path("api.py"))))

    port = find_free_port()
    server = Server(port)
    server.start()

    if not server.wait_until_ready():
        print("Failed to start backend server", file=sys.stderr)
        server.stop()
        sys.exit(1)

    window = webview.create_window(
        "Productive Time Sync",
        f"http://127.0.0.1:{port}",
        width=900,
        height=700,
        resizable=True,
    )
    webview.start()

    server.stop()


if __name__ == "__main__":
    main()
