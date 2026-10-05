import base64
import json
import mimetypes
import os
import posixpath
import shlex
import threading
import tempfile
import time
import uuid
from urllib.parse import urlsplit

from instavm import InstaVM
from openai import OpenAI

import storage


MODEL = "abliterated-model-large-v2"
IMAGE_MODEL = "abliterated-model"
SYSTEM_MESSAGE = """You are a capable digital work agent operating in this conversation's isolated, cloud-hosted InstaVM Linux sandbox. You can run shell commands, read and write files, and operate an isolated cloud browser that can navigate pages, inspect content, click, type, fill forms, scroll, and capture screenshots.

For every new request, first inspect the sandbox with sandbox_execute using a harmless command such as `pwd && ls -la`. Next, create a concise ordered checklist with update_checklist before doing substantive work. Keep it current as steps start, finish, or become blocked. For complex tasks, work through the checklist one step at a time. Use the browser tools for websites and browser-based graphical interfaces; use shell and file tools for code and local files. For code or multi-step workflows, run the actual code/process, inspect errors and output, fix failures, and rerun the relevant checks before presenting the result. Do not substitute instructions or a hypothetical description for requested work. Publish requested Sites so they have a working URL. Preserve useful intermediate files in the sandbox, and use the artifact tool for every finished document, spreadsheet, presentation, report, or other downloadable file. Never claim an operation succeeded unless its tool result confirms it. The user may redirect you during a task; follow the latest instruction before starting further work. Reply in the user's language."""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "sandbox_execute",
            "description": "Run a shell command in this conversation's isolated InstaVM Linux sandbox. The shell has internet access and can run installed code, install packages, inspect the workspace, and create files.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_read_file",
            "description": "Read a file from the conversation's InstaVM sandbox and return its contents.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_write_file",
            "description": "Write complete text content to a file inside the conversation's InstaVM sandbox, creating parent directories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "publish_file",
            "description": "Publish a finished file from the sandbox as a downloadable artifact in the conversation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": ["path", "name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "publish_site",
            "description": "Start a web site from a sandbox directory and publish a working public InstaVM URL. Use when the user asks for a usable, shareable Site.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string"},
                    "port": {"type": "integer"},
                    "name": {"type": "string"},
                },
                "required": ["directory", "port", "name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_checklist",
            "description": "Create or update the concise execution checklist for the current task. Send the full current list each time so completed and blocked steps stay accurate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "title": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed", "blocked"],
                                },
                            },
                            "required": ["id", "title", "status"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["steps"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Open an HTTP or HTTPS page in this conversation's isolated cloud browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "wait_timeout": {"type": "integer"},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_inspect",
            "description": "Read the current page's clean text, interactive controls, and selector anchors to decide what to do next.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click a page element using a CSS selector from the current browser page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "force": {"type": "boolean"},
                },
                "required": ["selector"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_fill",
            "description": "Replace the value of a form field selected by CSS selector.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["selector", "value"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "Type text into an element selected by CSS selector without replacing existing content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["selector", "text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_scroll",
            "description": "Scroll the current page, or a selected scrollable element.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["y"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_wait",
            "description": "Wait for the page to load or for a selected element to appear.",
            "parameters": {
                "type": "object",
                "properties": {
                    "condition": {
                        "type": "string",
                        "enum": ["load", "domcontentloaded", "networkidle", "selector"],
                    },
                    "selector": {"type": "string"},
                },
                "required": ["condition"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Capture the current browser page as a downloadable PNG artifact for visual review.",
            "parameters": {
                "type": "object",
                "properties": {"full_page": {"type": "boolean"}},
                "additionalProperties": False,
            },
        },
    },
]


_running_jobs = set()
_running_lock = threading.Lock()
_browser_sessions = {}
_browser_sessions_lock = threading.Lock()


class JobCancelled(Exception):
    pass


def _get_item(value, key, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _first_item(value, keys, default=None):
    for key in keys:
        item = _get_item(value, key, None)
        if item is not None and item != "":
            return item
    return default


def _record_payload(value, key):
    nested = _get_item(value, key, None)
    if nested is not None:
        return nested
    return value


def _stringify_json(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def _merge_stream_field(existing, piece):
    piece = str(piece)
    if not existing:
        return piece
    if piece == existing or existing.endswith(piece):
        return existing
    if piece.startswith(existing):
        return piece
    return existing + piece


def _normalize_tool_calls(tool_calls):
    normalized = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(function.get("name") or "").strip()
        arguments = function.get("arguments")
        if arguments is None:
            arguments = "{}"
        elif not isinstance(arguments, str):
            arguments = _stringify_json(arguments)
        call_id = str(call.get("id") or "").strip()
        if not call_id:
            call_id = "call_" + uuid.uuid4().hex
        if not name:
            name = "unknown_tool"
        normalized.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments,
                },
            }
        )
    return normalized


def _message_content_for_api(role, content, has_tool_calls):
    if content is None:
        if role == "assistant" and has_tool_calls:
            return None
        return ""
    if role == "user":
        if isinstance(content, (str, list)):
            return content
        return _stringify_json(content)
    if isinstance(content, str):
        return content
    return _stringify_json(content)


def _messages_for_api(agent_messages):
    messages = []
    for message in agent_messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            continue
        if role == "tool":
            tool_call_id = str(message.get("tool_call_id") or "").strip()
            if not tool_call_id:
                continue
            content = message.get("content")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": content if isinstance(content, str) else _stringify_json(content),
                }
            )
            continue
        tool_calls = _normalize_tool_calls(message.get("tool_calls")) if role == "assistant" and message.get("tool_calls") else []
        api_message = {
            "role": role,
            "content": _message_content_for_api(role, message.get("content"), bool(tool_calls)),
        }
        if role == "assistant" and tool_calls:
            api_message["tool_calls"] = tool_calls
        messages.append(api_message)
    return messages


def launch_job(job_id):
    with _running_lock:
        if job_id in _running_jobs:
            return
        _running_jobs.add(job_id)
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()


def resume_pending_jobs():
    for row in storage.pending_jobs():
        launch_job(row["job_id"])


def _event(job_id, payload):
    storage.append_event(job_id, payload)


def _progress(job_id, text):
    _event(
        job_id,
        {
            "type": "agent_event",
            "progress": text,
            "event": {"type": "progress", "step_id": text},
        },
    )


def _make_client(session_id=None):
    api_key = os.environ.get("INSTAVM_API_KEY")
    if not api_key:
        raise RuntimeError("INSTAVM_API_KEY is not configured")
    client = InstaVM(api_key=api_key, auto_start_session=False, timeout=3600, max_retries=0)
    if session_id:
        client.session_id = str(session_id)
    return client


def _ensure_sandbox(conversation_id, vm_id, session_id, job_id):
    if vm_id and session_id:
        vm_id = str(vm_id)
        session_id = str(session_id)
        client = _make_client(session_id)
        try:
            record = client.vms.get(vm_id)
            record = _record_payload(record, "vm")
            vm_status = str(_first_item(record, ("status", "state"), "") or "")
            vm_status = vm_status.strip().lower().replace("-", "_").replace(" ", "_")
            if vm_status in (
                "terminated",
                "deleted",
                "failed",
                "expired",
                "stopped",
                "killed",
                "unavailable",
                "not_available",
            ):
                vm_id = None
                session_id = None
        except Exception as error:
            status_code = getattr(getattr(error, "response", None), "status_code", None)
            if status_code not in (404, 410):
                raise
            vm_id = None
            session_id = None
        if vm_id and session_id:
            return client, vm_id, session_id
    _progress(job_id, "Új, elkülönített InstaVM sandbox indítása")
    client = _make_client()
    created = client.vms.create(
        wait=True,
        vm_lifetime_seconds=3600,
        auto_suspend=True,
        auto_resume=True,
    )
    created_vm = _record_payload(created, "vm")
    created_session = _record_payload(created, "session")
    vm_id = _first_item(created_vm, ("vm_id", "id"), None) or _first_item(created, ("vm_id", "id"), None)
    session_id = (
        _first_item(created_session, ("session_id", "id"), None)
        or _first_item(created_vm, ("session_id", "vm_session_id"), None)
        or _first_item(created, ("session_id", "vm_session_id"), None)
    )
    if not vm_id or not session_id:
        raise RuntimeError("Az InstaVM nem adott vissza VM- és session-azonosítót")
    vm_id = str(vm_id)
    session_id = str(session_id)
    client.session_id = session_id
    storage.update_sandbox(conversation_id, vm_id, session_id)
    _event(
        job_id,
        {
            "type": "agent_event",
            "event": {"type": "progress", "step_id": "Az elkülönített sandbox elindult"},
        },
    )
    return client, vm_id, session_id


def _browser_session_id(created):
    if isinstance(created, str):
        return created
    created_session = _record_payload(created, "session")
    session_id = (
        _first_item(created_session, ("session_id", "browser_session_id", "id"), None)
        or _first_item(created, ("session_id", "browser_session_id", "id"), None)
    )
    return str(session_id) if session_id else None


def _ensure_browser_session(job, client):
    session_id = getattr(client, "_work_browser_session_id", None)
    if session_id:
        return session_id

    conversation_id = job["conversation_id"]
    with _browser_sessions_lock:
        session_id = _browser_sessions.get(conversation_id)
        if session_id:
            get_browser_session = getattr(client, "get_browser_session", None)
            if callable(get_browser_session):
                try:
                    info = get_browser_session(session_id)
                    details = _record_payload(info, "session")
                    status = str(_first_item(details, ("status", "state"), "") or "").strip().lower()
                    if status in {"closed", "deleted", "expired", "failed", "killed"}:
                        session_id = None
                except Exception as error:
                    message = str(error).lower()
                    if "not found" in message or "expired" in message:
                        session_id = None
                    else:
                        raise

        if not session_id:
            create_browser_session = getattr(client, "create_browser_session", None)
            if not callable(create_browser_session):
                raise RuntimeError("Az InstaVM kliens nem támogatja a böngésző-session létrehozását")
            session_id = _browser_session_id(create_browser_session())
            if not session_id:
                raise RuntimeError("Az InstaVM nem adott vissza böngésző-session azonosítót")
            _browser_sessions[conversation_id] = session_id

    client._work_browser_session_id = session_id
    return session_id


def _update_checklist(job, args):
    steps = args.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
        raise ValueError("Az ellenőrzőlista 1–8 lépésből álljon")
    normalized = []
    seen_ids = set()
    allowed_statuses = {"pending", "in_progress", "completed", "blocked"}
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Minden ellenőrzőlista-lépés objektum legyen")
        step_id = str(step.get("id") or "").strip()
        title = str(step.get("title") or "").strip()
        status = str(step.get("status") or "").strip()
        if not step_id or step_id in seen_ids or not title or status not in allowed_statuses:
            raise ValueError("Az ellenőrzőlista-lépés azonosítója, címe vagy állapota érvénytelen")
        seen_ids.add(step_id)
        normalized.append({"id": step_id[:80], "title": title[:200], "status": status})
    _event(
        job["job_id"],
        {
            "type": "agent_event",
            "progress": "Ellenőrzőlista frissítve",
            "event": {"type": "checklist", "steps": normalized},
        },
    )
    return {"steps": normalized}


def _browser_inspect(client, session_id):
    result = client.browser_extract_content(
        session_id=session_id,
        include_interactive=True,
        include_anchors=True,
        max_anchors=40,
    )
    if not isinstance(result, dict):
        return result
    output = {
        key: result[key]
        for key in ("url", "title", "readable_content")
        if key in result
    }
    readable = output.get("readable_content")
    if isinstance(readable, dict) and isinstance(readable.get("content"), str):
        readable = dict(readable)
        readable["content"] = readable["content"][:30000]
        output["readable_content"] = readable
    elif isinstance(readable, str):
        output["readable_content"] = readable[:30000]
    for key in ("interactive_elements", "content_anchors"):
        if key in result:
            output[key] = result[key][:40] if isinstance(result[key], list) else result[key]
    return output


def _extract_base64_image(value):
    if isinstance(value, bytes):
        return value
    if isinstance(value, dict):
        for key in ("base64", "image", "data", "screenshot", "content"):
            item = value.get(key)
            if item:
                return _extract_base64_image(item)
        raise RuntimeError("A böngésző nem adott vissza képernyőképet")
    if not isinstance(value, str) or not value:
        raise RuntimeError("A böngésző nem adott vissza képernyőképet")
    encoded = value
    if encoded.startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]
    return base64.b64decode(encoded, validate=True)


def _browser_screenshot(job, client, session_id, full_page):
    image = _extract_base64_image(client.browser_screenshot(session_id=session_id, full_page=full_page, format="png"))
    artifact_id = str(uuid.uuid4())
    name = "browser-screenshot.png"
    storage.create_artifact(
        artifact_id,
        job["conversation_id"],
        job["job_id"],
        name,
        "image/png",
        image,
    )
    artifact = {
        "artifact_id": artifact_id,
        "name": name,
        "content_type": "image/png",
        "size": len(image),
    }
    _event(job["job_id"], {"type": "agent_event", "event": {"type": "artifact", "artifact": artifact}})
    return artifact


def _execute_browser_tool(name, args, job, client):
    session_id = _ensure_browser_session(job, client)
    if name == "browser_navigate":
        url = str(args["url"]).strip()
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("A böngésző csak érvényes HTTP- vagy HTTPS-URL-t nyit meg")
        return client.browser_navigate(
            url=url,
            session_id=session_id,
            wait_timeout=max(1000, min(int(args.get("wait_timeout", 30000)), 60000)),
        )
    if name == "browser_inspect":
        return _browser_inspect(client, session_id)
    if name == "browser_click":
        return client.browser_click(
            selector=str(args["selector"]),
            session_id=session_id,
            force=bool(args.get("force", False)),
        )
    if name == "browser_fill":
        return client.browser_fill(
            selector=str(args["selector"]),
            value=str(args["value"]),
            session_id=session_id,
        )
    if name == "browser_type":
        return client.browser_type(
            selector=str(args["selector"]),
            text=str(args["text"]),
            session_id=session_id,
        )
    if name == "browser_scroll":
        return client.browser_scroll(
            session_id=session_id,
            selector=args.get("selector"),
            x=int(args.get("x", 0)),
            y=int(args["y"]),
        )
    if name == "browser_wait":
        condition = str(args["condition"])
        selector = args.get("selector")
        if condition == "selector" and not selector:
            raise ValueError("A selector feltételhez CSS-szelektor is szükséges")
        return client.browser_wait(
            condition=condition,
            session_id=session_id,
            selector=selector,
        )
    if name == "browser_screenshot":
        return _browser_screenshot(job, client, session_id, bool(args.get("full_page", True)))
    raise ValueError(f"Ismeretlen böngészőeszköz: {name}")


def _text_from_execution(result):
    if isinstance(result, str):
        return result
    parts = []
    for key in ("stdout", "output", "result", "stderr", "error", "error_message"):
        value = _get_item(result, key, None)
        if value is None or value == "":
            continue
        if isinstance(value, (dict, list)):
            parts.append(_stringify_json(value))
        else:
            parts.append(str(value))
    if parts:
        return "\n".join(parts)
    return _stringify_json(result)


def _execution_failed(result):
    if result is None or isinstance(result, str):
        return False
    if _get_item(result, "success", None) is False:
        return True
    status = _get_item(result, "status", None)
    if isinstance(status, str) and status.lower() in ("error", "failed", "failure"):
        return True
    for key in ("exit_code", "return_code", "returncode"):
        if _get_item(result, key, None) not in (None, 0):
            return True
    return bool(_get_item(result, "error", None) or _get_item(result, "error_message", None))


def _run_command(client, command):
    result = client.execute(command, language="bash", timeout=3600)
    if _execution_failed(result):
        raise RuntimeError(_text_from_execution(result))
    return {"result": result, "output": _text_from_execution(result)}


def _remote_output(client, command, allow_empty=False):
    result = client.execute(command, language="bash", timeout=3600)
    if _execution_failed(result):
        raise RuntimeError(_text_from_execution(result))
    if isinstance(result, dict):
        output = result.get("stdout") or result.get("output") or result.get("result") or ""
        if isinstance(output, dict):
            output = output.get("stdout") or output.get("output") or ""
        if isinstance(output, (list, tuple)):
            output = "\n".join(str(item) for item in output)
    else:
        output = _text_from_execution(result)
    if not output and not allow_empty:
        raise RuntimeError(_text_from_execution(result))
    return str(output).strip()


def _publish_file(job, client, args):
    path = str(args["path"])
    name = str(args["name"] or "").strip() or os.path.basename(path) or "artifact"
    encoded = _remote_output(
        client,
        "python3 -c "
        + shlex.quote(
            "import base64,pathlib,sys;sys.stdout.write(base64.b64encode(pathlib.Path(sys.argv[1]).read_bytes()).decode())"
        )
        + " "
        + shlex.quote(path),
        allow_empty=True,
    )
    content = base64.b64decode(encoded, validate=True) if encoded else b""
    artifact_id = str(uuid.uuid4())
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    storage.create_artifact(
        artifact_id,
        job["conversation_id"],
        job["job_id"],
        name,
        content_type,
        content,
    )
    artifact = {
        "artifact_id": artifact_id,
        "name": name,
        "content_type": content_type,
        "size": len(content),
    }
    _event(job["job_id"], {"type": "agent_event", "event": {"type": "artifact", "artifact": artifact}})
    return artifact


def _publish_site(job, client, vm_id, args):
    directory = str(args["directory"])
    port = int(args["port"])
    if port < 1 or port > 65535:
        raise ValueError("A port értéke 1 és 65535 közé essen")
    launch = (
        "nohup python3 -m http.server "
        + str(port)
        + " --bind 0.0.0.0 --directory "
        + shlex.quote(directory)
        + " >/tmp/instavm-site-"
        + str(port)
        + ".log 2>&1 </dev/null &"
    )
    _run_command(client, launch)
    time.sleep(1)
    share = client.shares.create(port=port, vm_id=vm_id, is_public=True)
    share_record = _record_payload(share, "share")
    url = _first_item(share_record, ("url", "public_url", "share_url"), None) or _first_item(share, ("url", "public_url", "share_url"), None)
    if not url:
        raise RuntimeError("Az InstaVM nem adott vissza nyilvános webhely-URL-t")
    share_id = _first_item(share_record, ("share_id", "id"), None) or _first_item(share, ("share_id", "id"), None)
    artifact_id = str(uuid.uuid4())
    content = json.dumps({"url": url, "share_id": share_id}, ensure_ascii=False).encode()
    artifact_name = str(args["name"] or "").strip() or "site"
    storage.create_artifact(
        artifact_id,
        job["conversation_id"],
        job["job_id"],
        artifact_name,
        "application/vnd.instavm.site-link+json",
        content,
    )
    artifact = {
        "artifact_id": artifact_id,
        "name": artifact_name,
        "content_type": "text/html",
        "url": url,
    }
    _event(job["job_id"], {"type": "agent_event", "event": {"type": "artifact", "artifact": artifact}})
    return artifact


def _execute_tool(name, args, job, client, vm_id):
    if not isinstance(args, dict):
        raise ValueError("Az eszközargumentumoknak objektumnak kell lenniük")
    if name == "update_checklist":
        return _update_checklist(job, args)
    if name.startswith("browser_"):
        return _execute_browser_tool(name, args, job, client)
    if name == "sandbox_execute":
        return _run_command(client, str(args["command"]))
    if name == "sandbox_read_file":
        return _run_command(client, "cat -- " + shlex.quote(str(args["path"])))
    if name == "sandbox_write_file":
        path = str(args["path"])
        content = str(args["content"]).encode()
        parent = posixpath.dirname(path)
        if parent:
            _run_command(client, "mkdir -p -- " + shlex.quote(parent))
        with tempfile.NamedTemporaryFile(delete=False) as temporary:
            temporary.write(content)
            temporary_path = temporary.name
        try:
            result = client.upload_file(temporary_path, path)
            if _execution_failed(result):
                raise RuntimeError(_text_from_execution(result))
            return result if result is not None else {"path": path, "uploaded": True}
        finally:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
    if name == "publish_file":
        return _publish_file(job, client, args)
    if name == "publish_site":
        return _publish_site(job, client, vm_id, args)
    raise ValueError(f"Ismeretlen eszköz: {name}")


def _new_model_client():
    api_key = os.environ.get("ABLITERATION_API_KEY")
    if not api_key:
        raise RuntimeError("ABLITERATION_API_KEY is not configured")
    return OpenAI(
        api_key=api_key,
        base_url="https://api.abliteration.ai/v1",
        timeout=3600,
        max_retries=2,
    )


def _append_redirects(job_id, agent_messages):
    redirects = storage.take_redirects(job_id)
    if not redirects:
        return False
    after_assistant = bool(agent_messages and agent_messages[-1].get("role") == "assistant")
    for index, item in enumerate(redirects):
        if after_assistant and index == 0:
            prefix = "The user redirected this work. Follow the latest instruction and continue without repeating completed output: "
        elif index == 0:
            prefix = "The user redirected this work. Follow the latest instruction: "
        else:
            prefix = "Additional instruction from the user: "
        if isinstance(item, list):
            content = [{"type": "text", "text": prefix}]
            content.extend(item)
        else:
            content = prefix + str(item)
        agent_messages.append({"role": "user", "content": content})
    storage.save_agent_messages(job_id, agent_messages)
    _progress(job_id, "A felhasználó frissítette a feladatot; az új irányt követem")
    return True


def _contains_image(value):
    if isinstance(value, dict):
        if value.get("type") in ("image", "image_url", "input_image") or "image_url" in value:
            return True
        return any(_contains_image(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_image(item) for item in value)
    return False


def _model_for_messages(agent_messages):
    if any(message.get("role") == "user" and _contains_image(message.get("content")) for message in agent_messages if isinstance(message, dict)):
        return IMAGE_MODEL
    return MODEL


def _extended_completion_options_error(error):
    text = str(error).lower()
    needles = (
        "unknown parameter",
        "unsupported parameter",
        "unrecognized request argument",
        "unexpected keyword",
        "extra inputs are not permitted",
        "web_search_options",
        "reasoning_effort",
        "include_reasoning",
        "extra_body",
    )
    return any(needle in text for needle in needles)


def _completion_stream(client, model, agent_messages, tool_choice):
    request = {
        "model": model,
        "temperature": 1,
        "top_p": 1,
        "messages": _messages_for_api(agent_messages),
        "max_tokens": 120000,
        "reasoning_effort": "max",
        "web_search_options": {"search_context_size": "medium"},
        "tools": TOOLS,
        "tool_choice": tool_choice,
        "stream": True,
        "extra_body": {"include_reasoning": True},
    }
    try:
        return client.chat.completions.create(**request)
    except Exception as error:
        if not _extended_completion_options_error(error):
            raise
        fallback = dict(request)
        fallback.pop("reasoning_effort", None)
        fallback.pop("web_search_options", None)
        fallback.pop("extra_body", None)
        return client.chat.completions.create(**fallback)


def _consume_stream(
    job,
    client,
    sandbox,
    vm_id,
    agent_messages,
    model,
    require_shell=False,
    require_checklist=False,
):
    tool_choice = (
        {"type": "function", "function": {"name": "sandbox_execute"}}
        if require_shell
        else (
            {"type": "function", "function": {"name": "update_checklist"}}
            if require_checklist
            else "auto"
        )
    )
    completion = _completion_stream(client, model, agent_messages, tool_choice)
    text = []
    reasoning = []
    tool_calls = {}
    finish_reason = None
    was_redirected = False
    try:
        for chunk in completion:
            if storage.is_cancel_requested(job["job_id"]):
                raise JobCancelled()
            if storage.has_redirects(job["job_id"]):
                was_redirected = True
                break
            choices = _get_item(chunk, "choices", None) or []
            choice = choices[0] if choices else None
            if not choice:
                continue
            delta = _get_item(choice, "delta", None)
            content_delta = _get_item(delta, "content", None)
            reasoning_delta = _get_item(delta, "reasoning_content", None) or _get_item(delta, "reasoning", None)
            if isinstance(content_delta, str) and content_delta:
                text.append(content_delta)
                _event(job["job_id"], {"type": "delta", "content": content_delta})
            if isinstance(reasoning_delta, str) and reasoning_delta:
                reasoning.append(reasoning_delta)
                _event(job["job_id"], {"type": "delta", "reasoning_content": reasoning_delta})
            for call_delta in _get_item(delta, "tool_calls", None) or []:
                call_index = _get_item(call_delta, "index", None)
                if call_index is None:
                    call_index = 0 if not tool_calls else max(tool_calls.keys()) + 1
                record = tool_calls.setdefault(
                    int(call_index),
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                call_id = _get_item(call_delta, "id", None)
                call_type = _get_item(call_delta, "type", None)
                function = _get_item(call_delta, "function", None)
                function_name = _get_item(function, "name", None)
                function_arguments = _get_item(function, "arguments", None)
                if call_id:
                    record["id"] = _merge_stream_field(record["id"], call_id)
                if call_type:
                    record["type"] = str(call_type)
                if function_name:
                    record["function"]["name"] = _merge_stream_field(record["function"]["name"], function_name)
                if function_arguments:
                    record["function"]["arguments"] += str(function_arguments)
            choice_finish_reason = _get_item(choice, "finish_reason", None)
            if choice_finish_reason:
                finish_reason = choice_finish_reason
    finally:
        close = getattr(completion, "close", None)
        if callable(close):
            close()
    return "".join(text), "".join(reasoning), _normalize_tool_calls([tool_calls[index] for index in sorted(tool_calls)]), finish_reason, was_redirected


def _repair_dangling_tool_calls(job_id, agent_messages):
    changed = False
    repaired_events = []
    index = 0
    while index < len(agent_messages):
        message = agent_messages[index]
        tool_calls = message.get("tool_calls") if isinstance(message, dict) and message.get("role") == "assistant" else None
        if not isinstance(tool_calls, list) or not tool_calls:
            index += 1
            continue
        normalized_calls = _normalize_tool_calls(tool_calls)
        if normalized_calls != tool_calls:
            message["tool_calls"] = normalized_calls
            tool_calls = normalized_calls
            changed = True
        expected_ids = {
            call.get("id")
            for call in tool_calls
            if isinstance(call, dict) and call.get("id")
        }
        cursor = index + 1
        completed_ids = set()
        while cursor < len(agent_messages) and isinstance(agent_messages[cursor], dict) and agent_messages[cursor].get("role") == "tool":
            call_id = agent_messages[cursor].get("tool_call_id")
            if call_id in expected_ids:
                completed_ids.add(call_id)
            cursor += 1
        missing_calls = [
            call
            for call in tool_calls
            if isinstance(call, dict) and call.get("id") and call.get("id") not in completed_ids
        ]
        for call in missing_calls:
            name = (call.get("function") or {}).get("name", "tool")
            agent_messages.insert(
                cursor,
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(
                        {
                            "ok": False,
                            "error": (
                                "The server restarted before this tool result was saved. "
                                "The tool was not rerun because it may already have completed."
                            ),
                        },
                        ensure_ascii=False,
                    ),
                },
            )
            repaired_events.append((name, call["id"]))
            cursor += 1
            changed = True
        index = cursor
    if changed:
        storage.save_agent_messages(job_id, agent_messages)
        for name, _ in repaired_events:
            _event(
                job_id,
                {"type": "agent_event", "event": {"type": "tool_result", "tool": name, "ok": False}},
            )
    return changed


def _handle_tool_calls(job, client, sandbox, vm_id, agent_messages, assistant_text, reasoning, tool_calls):
    tool_calls = _normalize_tool_calls(tool_calls)
    assistant_message = {"role": "assistant", "content": assistant_text or None, "tool_calls": tool_calls}
    if reasoning:
        assistant_message["reasoning_content"] = reasoning
    agent_messages.append(assistant_message)
    storage.save_agent_messages(job["job_id"], agent_messages)
    for index, tool_call in enumerate(tool_calls):
        if storage.is_cancel_requested(job["job_id"]):
            raise JobCancelled()
        if storage.has_redirects(job["job_id"]):
            skipped_calls = tool_calls[index:]
            for skipped_call in skipped_calls:
                agent_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": skipped_call["id"],
                        "content": json.dumps(
                            {
                                "ok": False,
                                "error": "The user redirected the task before this tool ran; it was skipped.",
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
                _event(
                    job["job_id"],
                    {
                        "type": "agent_event",
                        "event": {
                            "type": "tool_result",
                            "tool": (skipped_call.get("function") or {}).get("name", "tool"),
                            "ok": False,
                        },
                    },
                )
            storage.save_agent_messages(job["job_id"], agent_messages)
            return
        name = tool_call["function"]["name"]
        call_id = tool_call["id"]
        _event(
            job["job_id"],
            {"type": "agent_event", "event": {"type": "tool_start", "tool": name}},
        )
        try:
            args = json.loads(tool_call["function"]["arguments"] or "{}")
            if not isinstance(args, dict):
                raise ValueError("Az eszközargumentumoknak JSON-objektumnak kell lenniük")
            result = _execute_tool(name, args, job, sandbox, vm_id)
            tool_result = {"ok": True, "result": result}
            _event(
                job["job_id"],
                {"type": "agent_event", "event": {"type": "tool_result", "tool": name, "ok": True}},
            )
        except JobCancelled:
            raise
        except Exception as error:
            tool_result = {"ok": False, "error": str(error)}
            _event(
                job["job_id"],
                {"type": "agent_event", "event": {"type": "tool_result", "tool": name, "ok": False}},
            )
        agent_messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(tool_result, ensure_ascii=False, default=str),
            }
        )
        storage.save_agent_messages(job["job_id"], agent_messages)


def _tool_names(tool_calls):
    names = set()
    for call in tool_calls:
        if isinstance(call, dict):
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = function.get("name")
            if name:
                names.add(name)
    return names


def _run_job(job_id):
    try:
        job = storage.get_job_and_conversation(job_id)
        if not job or job["status"] != "running":
            return
        if storage.is_cancel_requested(job_id):
            raise JobCancelled()
        agent_messages = job["agent_messages"] or []
        if not agent_messages:
            agent_messages = [{"role": "system", "content": SYSTEM_MESSAGE}]
            agent_messages.extend(
                item
                for item in (job["conversation_messages"] or [])
                if isinstance(item, dict) and item.get("role") in ("user", "assistant")
            )
        recovered_tool_calls = _repair_dangling_tool_calls(job_id, agent_messages)
        if job["response_content"] and not recovered_tool_calls:
            if not agent_messages or agent_messages[-1].get("role") != "assistant":
                agent_messages.append({"role": "assistant", "content": job["response_content"]})
                agent_messages.append(
                    {
                        "role": "user",
                        "content": "Continue the unfinished work from the previous response without repeating completed content.",
                    }
                )
        storage.save_agent_messages(job_id, agent_messages)
        _progress(job_id, "Work-feladat indítása")
        sandbox, vm_id, session_id = _ensure_sandbox(
            job["conversation_id"], job["vm_id"], job["vm_session_id"], job_id
        )
        if storage.is_cancel_requested(job_id):
            raise JobCancelled()
        model_client = _new_model_client()
        last_model = None
        require_shell = True
        require_checklist = False
        while True:
            if storage.is_cancel_requested(job_id):
                raise JobCancelled()
            if _append_redirects(job_id, agent_messages):
                continue
            model = _model_for_messages(agent_messages)
            if model != last_model:
                _event(
                    job_id,
                    {
                        "type": "agent_event",
                        "event": {"type": "model_start", "model": model},
                    },
                )
                last_model = model
            text, reasoning, tool_calls, finish_reason, redirected = _consume_stream(
                job,
                model_client,
                sandbox,
                vm_id,
                agent_messages,
                model,
                require_shell=require_shell,
                require_checklist=require_checklist,
            )
            if redirected or storage.has_redirects(job_id):
                if text or reasoning:
                    interrupted_message = {"role": "assistant", "content": text or None}
                    if reasoning:
                        interrupted_message["reasoning_content"] = reasoning
                    agent_messages.append(interrupted_message)
                storage.save_agent_messages(job_id, agent_messages)
                _append_redirects(job_id, agent_messages)
                continue
            names = _tool_names(tool_calls)
            if require_shell and "sandbox_execute" not in names:
                raise RuntimeError("A modell nem indította el a kötelező shell-ellenőrzést.")
            if require_checklist and "update_checklist" not in names:
                raise RuntimeError("A modell nem hozta létre a kötelező feladat-ellenőrzőlistát.")
            if tool_calls:
                if require_shell:
                    require_shell = False
                    require_checklist = "update_checklist" not in names
                elif require_checklist:
                    require_checklist = False
                _handle_tool_calls(
                    job,
                    model_client,
                    sandbox,
                    vm_id,
                    agent_messages,
                    text,
                    reasoning,
                    tool_calls,
                )
                continue
            if text or reasoning:
                if text:
                    agent_messages.append({"role": "assistant", "content": text})
                elif reasoning:
                    agent_messages.append({"role": "assistant", "content": ""})
                if reasoning:
                    _event(job_id, {"type": "agent_event", "event": {"type": "model_output"}})
                storage.save_agent_messages(job_id, agent_messages)
                if finish_reason == "length":
                    agent_messages.append(
                        {
                            "role": "user",
                            "content": "Continue exactly where you stopped. Do not repeat completed content.",
                        }
                    )
                    storage.save_agent_messages(job_id, agent_messages)
                    continue
                break
            if finish_reason == "stop":
                break
            raise RuntimeError("A modell üres választ adott")
        status = "cancelled" if storage.is_cancel_requested(job_id) else "completed"
        if status == "completed":
            _event(job_id, {"type": "done"})
        storage.finish_job(job_id, status)
    except JobCancelled:
        _event(job_id, {"type": "done", "cancelled": True})
        storage.finish_job(job_id, "cancelled")
    except Exception as error:
        message = str(error) or type(error).__name__
        _event(job_id, {"type": "error", "message": message})
        storage.finish_job(job_id, "failed", message)
    finally:
        with _running_lock:
            _running_jobs.discard(job_id)