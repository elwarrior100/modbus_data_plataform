"""API HTTP de controlo: ativar e cancelar cenários em execução, ver o estado."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .scenarios import ScenarioError


def make_handler(plant):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"status": "ok"})
            elif self.path == "/scenarios":
                self._send(200, plant.engine.listing())
            elif self.path == "/state":
                self._send(200, plant.snapshot())
            else:
                self._send(404, {"error": "não encontrado"})

        def do_POST(self):
            if self.path != "/scenarios":
                return self._send(404, {"error": "não encontrado"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                scn = plant.engine.activate(body["asset"], body["kind"], body.get("duration_s"),
                                            body.get("params"), body.get("start_in_s", 0.0))
            except (KeyError, json.JSONDecodeError):
                return self._send(400, {"error": "esperado JSON com asset e kind"})
            except ScenarioError as exc:
                return self._send(400, {"error": str(exc)})
            self._send(201, scn.as_dict(plant.clock.monotonic()))

        def do_DELETE(self):
            prefix = "/scenarios/"
            if not self.path.startswith(prefix) or not self.path[len(prefix):].isdigit():
                return self._send(404, {"error": "não encontrado"})
            found = plant.engine.cancel(int(self.path[len(prefix):]))
            self._send(200 if found else 404, {"cancelled": found})

        def log_message(self, *args):
            pass

    return Handler


def start_control(plant, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(plant))
    threading.Thread(target=server.serve_forever, daemon=True, name="control").start()
    return server
