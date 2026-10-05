import json
import mimetypes
import os
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote_from_bytes, unquote, urlsplit

import agent
import storage


ROOT = Path(__file__).resolve().parent


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode()


def conversation_title(messages):
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()[:120]
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip():
                    return part["text"].strip()[:120]
            if content:
                return "Csatolt fájl"
    return "Beszélgetés"


class ApplicationServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ChatWorkbench"
    sys_version = ""

    def log_message(self, format_string, *args):
        return

    def _send(self, status, content, content_type="application/json; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if content:
            self.wfile.write(content)

    def _json(self, status, value, headers=None):
        self._send(status, json_bytes(value), headers=headers)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid Content-Length")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("A JSON object is required")
        return value

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if path == "/api/health":
            self._json(200, {"status": "ok"})
            return
        if path == "/api/conversations":
            query = parse_qs(parsed.query)
            ids = [value for item in query.get("ids", []) for value in item.split(",") if value]
            conversations = storage.list_conversations(ids)
            self._json(200, {"conversations": conversations})
            return
        if path.startswith("/api/chat/jobs/") and path.endswith("/events"):
            job_id = path[len("/api/chat/jobs/") : -len("/events")].strip("/")
            after = parse_qs(parsed.query).get("after", ["0"])[0]
            try:
                after_id = max(0, int(after))
            except ValueError:
                self._json(400, {"error": "Érvénytelen eseményazonosító"})
                return
            self._stream_job_events(job_id, after_id)
            return
        if path.startswith("/api/artifacts/"):
            self._serve_artifact(path.rsplit("/", 1)[-1])
            return
        if path.startswith("/api/"):
            self._json(404, {"error": "Nem található"})
            return
        self._serve_static(path)

    def do_POST(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})
            return
        if path == "/api/chat":
            self._start_chat(body)
            return
        match = re.fullmatch(r"/api/chat/jobs/([^/]+)/stop", path)
        if match:
            stopped = storage.request_cancel(match.group(1))
            self._json(200 if stopped else 409, {"stopped": stopped})
            return
        match = re.fullmatch(r"/api/chat/jobs/([^/]+)/redirect", path)
        if match:
            message = body.get("message", body.get("content"))
            valid_text = isinstance(message, str) and bool(message.strip())
            valid_parts = isinstance(message, list) and any(
                isinstance(part, dict)
                and (
                    (part.get("type") == "text" and isinstance(part.get("text"), str) and part["text"].strip())
                    or part.get("type") in ("image_url", "input_image")
                    or "image_url" in part
                )
                for part in message
            )
            if not valid_text and not valid_parts:
                self._json(400, {"error": "Az új irány nem lehet üres"})
                return
            redirect_content = message.strip() if valid_text else message
            conversation_id = storage.add_redirect(match.group(1), redirect_content)
            if not conversation_id:
                self._json(409, {"error": "A Work-feladat már nem fut"})
                return
            self._json(202, {"accepted": True})
            return
        self._json(404, {"error": "Nem található"})

    def _start_chat(self, body):
        supplied_id = body.get("conversation_id")
        conversation_id = supplied_id if isinstance(supplied_id, str) and supplied_id.strip() else str(uuid.uuid4())
        supplied_messages = body.get("messages")
        if not isinstance(supplied_messages, list):
            self._json(400, {"error": {"message": "A messages mezőnek tömbnek kell lennie"}})
            return
        messages = []
        for item in supplied_messages:
            if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
                continue
            content = item.get("content")
            if isinstance(content, (str, list)):
                messages.append({"role": item["role"], "content": content})
        if not messages or not any(message["role"] == "user" for message in messages):
            self._json(400, {"error": {"message": "A beszélgetéshez legalább egy felhasználói üzenet szükséges"}})
            return
        title = conversation_title(messages)
        storage.upsert_conversation(conversation_id, messages, title)
        job_id = str(uuid.uuid4())
        agent_messages = [{"role": "system", "content": agent.SYSTEM_MESSAGE}]
        agent_messages.extend(messages)
        storage.create_job(job_id, conversation_id, agent_messages)
        storage.append_event(
            job_id,
            {"type": "meta", "conversation_id": conversation_id, "job_id": job_id, "work_task": True},
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Chat-Job-Id", job_id)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        agent.launch_job(job_id)
        self._stream_job_events(job_id, 0, headers_sent=True)

    def _stream_job_events(self, job_id, after, headers_sent=False):
        if not headers_sent:
            job = storage.get_job(job_id)
            if not job:
                self._json(404, {"error": "A Work-feladat nem található"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
        last_event_id = after
        try:
            while True:
                events = storage.list_events(job_id, last_event_id)
                for event in events:
                    data = b"id: " + str(event["event_id"]).encode() + b"\ndata: " + json_bytes(event["payload"]) + b"\n\n"
                    self.wfile.write(data)
                    self.wfile.flush()
                    last_event_id = event["event_id"]
                current = storage.get_job(job_id)
                if not current or current["status"] not in ("queued", "running"):
                    return
                if not events:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                time.sleep(0.7)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def _serve_artifact(self, artifact_id):
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", artifact_id):
            self._json(404, {"error": "A fájl nem található"})
            return
        artifact = storage.get_artifact(artifact_id)
        if not artifact:
            self._json(404, {"error": "A fájl nem található"})
            return
        if artifact["content_type"] == "application/vnd.instavm.site-link+json":
            try:
                site = json.loads(bytes(artifact["content"]).decode())
                url = site["url"]
            except (ValueError, KeyError, UnicodeDecodeError):
                self._json(500, {"error": "A Site-hivatkozás sérült"})
                return
            if not isinstance(url, str) or not url.startswith("https://"):
                self._json(500, {"error": "A Site-hivatkozás érvénytelen"})
                return
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        name = artifact["name"].replace("\r", "").replace("\n", "").replace('"', "")
        encoded_name = quote_from_bytes(name.encode("utf-8"))
        fallback_name = name.encode("ascii", "ignore").decode() or "artifact"
        disposition = f"attachment; filename=\"{fallback_name}\"; filename*=UTF-8''{encoded_name}"
        self._send(
            200,
            bytes(artifact["content"]),
            artifact["content_type"],
            {"Content-Disposition": disposition},
        )

    def _serve_static(self, request_path):
        relative = Path(request_path.lstrip("/") or "index.html")
        target = (ROOT / relative).resolve()
        try:
            target.relative_to(ROOT)
        except ValueError:
            self._json(404, {"error": "Nem található"})
            return
        if not target.is_file():
            self._json(404, {"error": "Nem található"})
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/manifest+json"):
            content_type += "; charset=utf-8"
        try:
            content = target.read_bytes()
        except OSError:
            self._json(500, {"error": "A fájl nem olvasható"})
            return
        self._send(200, content, content_type)


def main():
    port = int(os.environ.get("PORT", "5000"))
    server = ApplicationServer(("0.0.0.0", port), RequestHandler)
    agent.resume_pending_jobs()
    server.serve_forever()


if __name__ == "__main__":
    main()
