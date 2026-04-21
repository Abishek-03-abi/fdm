"""
FDM Desktop App – Entry point for the packaged application.
Starts the Flask server in a background thread and opens a native window via pywebview.
"""

import sys
import os
import threading
import time
import socket

# ── Ensure the bundled directory is on the path ──────────────────────────────
if getattr(sys, 'frozen', False):
    os.chdir(os.path.dirname(sys.executable))


def _find_free_port(preferred=6800):
    """Return `preferred` if available, otherwise pick a random free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def _wait_for_server(port, timeout=10):
    """Block until the Flask server is accepting connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def main():
    port = _find_free_port()
    url = f"http://127.0.0.1:{port}"

    # Import the Flask app from server.py
    from server import app

    # Start Flask in a daemon thread
    server_thread = threading.Thread(
        target=lambda: app.run(
            host="127.0.0.1",
            port=port,
            debug=False,
            threaded=True,
            use_reloader=False,
        ),
        daemon=True,
    )
    server_thread.start()

    # Wait for the server to be ready
    if not _wait_for_server(port):
        print("ERROR: Flask server did not start in time.")
        sys.exit(1)

    # Try to open a native webview window
    try:
        import webview
        window = webview.create_window(
            "FDM – Free Download Manager",
            url,
            width=1200,
            height=800,
            min_size=(900, 600),
        )
        webview.start()
    except ImportError:
        # Fallback: open system browser
        import webbrowser
        print(f"pywebview not installed — opening browser at {url}")
        webbrowser.open(url)
        print("Press Ctrl+C to stop the server.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
