"""
 Copyright (c) 2025, WSO2 LLC. (http://www.wso2.com). All Rights Reserved.

  Smart Employee Agent v2 — Client Dev Server

  Serves the client SPA (default port 3001, override with CLIENT_PORT) with
  a /config endpoint that exposes non-secret environment variables to the browser.
"""

import json
import os
from http.server import SimpleHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

PORT = int(os.getenv("CLIENT_PORT", "3001"))
DIRECTORY = Path(__file__).parent

# Public config exposed to the browser (no secrets)
CONFIG = {
    "asgardeoBaseUrl": os.getenv("ASGARDEO_BASE_URL", ""),
    "clientId": os.getenv("CLIENT_ID", ""),
    "redirectUri": os.getenv("REDIRECT_URI", f"http://localhost:{PORT}/callback"),
    "agentServerUrl": os.getenv("AGENT_SERVER_URL", "http://localhost:5001"),
    "hrServerUrl": os.getenv("HR_SERVER_URL", "http://localhost:8000"),
}


class ClientHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIRECTORY), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # /config endpoint — serve public config as JSON
        if path == "/config":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(json.dumps(CONFIG).encode())
            return

        # SPA routing: non-file paths serve index.html
        file_path = DIRECTORY / path.lstrip("/")
        if not file_path.is_file() and not path.startswith("/api"):
            self.path = "/index.html"

        return super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        super().end_headers()

    def log_message(self, format, *args):
        # Quieter logging — suppress 200/304 noise but always show 4xx/5xx errors
        # so failed asset fetches and config 500s are visible.
        status = str(args[1]) if len(args) > 1 else ""
        if status.startswith(("4", "5")):
            super().log_message(format, *args)


if __name__ == "__main__":
    server = HTTPServer(("", PORT), ClientHandler)
    print(f"Client running at http://localhost:{PORT}")
    print(f"Config: {json.dumps(CONFIG, indent=2)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()
