#!/usr/bin/env python3
"""Minimal ACP agent, spoken over stdio JSON-RPC to buzz-acp.

Deliberately not an LLM tool-calling loop: buzz-agent (the upstream minimal
ACP agent) leaves the *decision* to publish a reply up to the model, via a
"shell" tool it must choose to invoke. In practice, against buzz-acp's large
default system prompt, the model frequently answered the question but never
called the tool, so nothing got posted. This adapter removes that judgment
call entirely — every triggered turn deterministically calls the GCOR proxy
and posts whatever text comes back, with no LLM-driven decision in between.

Protocol notes (reverse-engineered from a real buzz-acp <-> buzz-agent
session captured with RUST_LOG=debug, and from crates/buzz-acp/src/queue.rs
in the block/buzz source at the pinned BUZZ_REF — not from published docs):
  - JSON-RPC 2.0, one object per line, on stdin/stdout. stdout is reserved
    for protocol frames only — all diagnostics go to stderr.
  - "initialize" / "session/new" just need a well-shaped result; buzz-acp
    doesn't appear to validate the contents closely.
  - "session/prompt" carries prompt blocks whose text buzz-acp itself
    formats (not model output) — see CHANNEL_RE etc. below for the exact
    fields relied on. If buzz-acp changes that formatting, these regexes
    are the first thing to check. Per queue.rs's format_event_block, the
    block always includes a "Tags: <json>" line with the triggering event's
    raw Nostr tags — including any "imeta" tag Buzz attaches for a media
    upload (Buzz's own upload validator only allows image/jpeg|png|gif|webp;
    there is no path for attaching a PDF/DOCX/etc. to a Buzz message at all).

Two ingestion paths into GCOR, both via the proxy's /v1/buzz/ingest:
  - An image attachment on the triggering message (imeta tag in Tags:) is
    downloaded with `buzz media get` (handles Blossom auth) and ingested.
  - A "<mention> ingest <name>" message ingests (or re-confirms) a file
    already sitting in MinIO — e.g. dropped into documents/inbox/ by
    something else — without needing a Buzz-native upload at all.
Anything else falls through to the existing Q&A flow unchanged.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid

BUZZ_BIN = "/usr/local/bin/buzz"
PROXY_BASE_URL = os.environ.get("OPENAI_COMPAT_BASE_URL", "http://proxy:5001/v1/buzz")
PROXY_API_KEY = os.environ["OPENAI_COMPAT_API_KEY"]
PROXY_URL = PROXY_BASE_URL.rstrip("/") + "/chat/completions"
INGEST_URL = PROXY_BASE_URL.rstrip("/") + "/ingest"

CHANNEL_RE = re.compile(r"Channel:.*\(#([0-9a-fA-F-]{36})\)")
EVENT_ID_RE = re.compile(r"Event ID:\s*([0-9a-fA-F]{64})")
CONTENT_RE = re.compile(r"^Content:\s*(.+)$", re.MULTILINE)
TAGS_RE = re.compile(r"^Tags:\s*(\[.*\])\s*$", re.MULTILINE)
MENTION_PREFIX_RE = re.compile(r"^@\S+[,:]?\s*")
INGEST_CMD_RE = re.compile(r"^ingest\s+(\S+)\s*$", re.IGNORECASE)


def log(msg: str) -> None:
    print(f"[reply_adapter] {msg}", file=sys.stderr, flush=True)


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def respond(req_id, result: dict) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def handle_initialize(req: dict) -> None:
    respond(req["id"], {
        "agentCapabilities": {
            "loadSession": False,
            "mcpCapabilities": {"http": False, "sse": False},
            "promptCapabilities": {"audio": False, "embeddedContext": False, "image": False},
        },
        "agentInfo": {"name": "gcor-reply-adapter", "version": "0.1.0"},
        "protocolVersion": 2,
    })


def handle_session_new(req: dict) -> None:
    session_id = f"ses_{uuid.uuid4().hex[:16]}"
    respond(req["id"], {
        "models": {"availableModels": [{"modelId": "openclaw", "name": "openclaw"}], "currentModelId": "openclaw"},
        "sessionId": session_id,
    })


def ask_proxy(question: str) -> str:
    body = json.dumps({"messages": [{"role": "user", "content": question}]}).encode()
    http_req = urllib.request.Request(
        PROXY_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {PROXY_API_KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(http_req, timeout=100) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"] or "(empty response)"


def extract_imeta_files(tags_json: str) -> list[dict]:
    """Pull image attachments out of a raw Nostr Tags: JSON array.

    Each "imeta" tag is ["imeta", "url <path>", "m <mime>", "x <sha256>",
    "filename <name>", ...] (see crates/buzz-relay/src/handlers/imeta.rs).
    Buzz's own upload validator only allows image/jpeg|png|gif|webp, so
    anything found here is necessarily an image.
    """
    try:
        tags = json.loads(tags_json)
    except json.JSONDecodeError:
        return []

    files = []
    for tag in tags:
        if not isinstance(tag, list) or not tag or tag[0] != "imeta":
            continue
        fields = {}
        for part in tag[1:]:
            key, _, value = part.partition(" ")
            fields[key] = value
        if fields.get("url"):
            files.append(fields)
    return files


def download_media(url_or_hash: str, out_path: str) -> None:
    """Fetch a Blossom media object via `buzz media get` (handles auth)."""
    result = subprocess.run(
        [BUZZ_BIN, "media", "get", url_or_hash, "-o", out_path],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"buzz media get failed (exit {result.returncode}): {result.stderr[:300]}")


def ingest_via_proxy(*, file_path: str | None = None, filename: str | None = None,
                      storage_key: str | None = None) -> dict:
    """POST to the proxy's /v1/buzz/ingest via curl (handles multipart encoding
    without pulling in a Python HTTP dependency the base image doesn't have)."""
    cmd = ["curl", "-sS", "--fail-with-body", "-X", "POST", INGEST_URL,
           "-H", f"Authorization: Bearer {PROXY_API_KEY}"]
    if file_path:
        cmd += ["-F", f"file=@{file_path};filename={filename or os.path.basename(file_path)}"]
    if storage_key:
        cmd += ["-F", f"storage_key={storage_key}"]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"ingest request failed (curl exit {result.returncode}): {result.stdout[:500]}")
    return json.loads(result.stdout)


def handle_ingest_attachments(files: list[dict]) -> str:
    """Download and ingest each image attachment. Returns a summary line."""
    results = []
    for f in files:
        name = f.get("filename") or (f.get("x", "file") + _ext_from_mime(f.get("m", "")))
        with tempfile.NamedTemporaryFile(suffix=_ext_from_mime(f.get("m", "")), delete=False) as tmp:
            tmp_path = tmp.name
        try:
            download_media(f["url"], tmp_path)
            resp = ingest_via_proxy(file_path=tmp_path, filename=name)
            results.append(f"'{name}' -> document {resp.get('document_id')} ({resp.get('chunks')} chunks)")
        except Exception as exc:
            log(f"attachment ingest failed for {name}: {exc!r}")
            results.append(f"'{name}' failed: {exc}")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return "Ingested " + "; ".join(results) if results else "No image attachments found to ingest."


def _ext_from_mime(mime: str) -> str:
    return {
        "image/jpeg": ".jpg", "image/png": ".png",
        "image/gif": ".gif", "image/webp": ".webp",
    }.get(mime, "")


def handle_ingest_command(name: str) -> str:
    """'<mention> ingest <name>' — ingest (or re-confirm) a file already in MinIO."""
    try:
        resp = ingest_via_proxy(storage_key=name)
        return f"Ingested '{name}' as document {resp.get('document_id')} ({resp.get('chunks')} chunks, collection '{resp.get('collection')}')."
    except Exception as exc:
        log(f"storage_key ingest failed for {name!r}: {exc!r}")
        return f"Couldn't ingest '{name}': {exc}"


def handle_session_prompt(req: dict) -> None:
    blocks = req.get("params", {}).get("prompt", [])
    full_text = "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict))

    channel_m = CHANNEL_RE.search(full_text)
    event_m = EVENT_ID_RE.search(full_text)
    content_m = CONTENT_RE.search(full_text)
    tags_m = TAGS_RE.search(full_text)

    if not channel_m or not content_m:
        log(f"could not parse channel/content from prompt, skipping turn. prompt={full_text[:500]!r}")
        respond(req["id"], {"stopReason": "end_turn"})
        return

    channel_id = channel_m.group(1)
    reply_to = event_m.group(1) if event_m else None
    question = MENTION_PREFIX_RE.sub("", content_m.group(1).strip())

    attachments = extract_imeta_files(tags_m.group(1)) if tags_m else []
    ingest_cmd_m = INGEST_CMD_RE.match(question)

    log(f"channel={channel_id} reply_to={reply_to} question={question[:200]!r} attachments={len(attachments)}")

    if attachments:
        answer = handle_ingest_attachments(attachments)
    elif ingest_cmd_m:
        answer = handle_ingest_command(ingest_cmd_m.group(1))
    else:
        try:
            answer = ask_proxy(question)
        except urllib.error.HTTPError as exc:
            answer = f"Sorry, the knowledge base returned an error ({exc.code})."
            log(f"proxy HTTP error: {exc.code} {exc.read()[:300]!r}")
        except Exception as exc:  # noqa: BLE001 - always want to post *something*
            answer = "Sorry, I hit an error trying to answer that."
            log(f"proxy call failed: {exc!r}")

    cmd = [BUZZ_BIN, "messages", "send", "--channel", channel_id, "--content", answer]
    if reply_to:
        cmd += ["--reply-to", reply_to]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        log(f"buzz messages send failed (exit {result.returncode}): {result.stderr[:500]}")
    else:
        log(f"posted reply: {result.stdout[:200]}")

    respond(req["id"], {"stopReason": "end_turn"})


HANDLERS = {
    "initialize": handle_initialize,
    "session/new": handle_session_new,
    "session/prompt": handle_session_prompt,
}


def main() -> None:
    log(f"starting, proxy_url={PROXY_URL}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            log(f"bad JSON-RPC line, ignoring: {line[:200]!r}")
            continue

        method = req.get("method")
        handler = HANDLERS.get(method)
        if handler is None:
            # Notifications (no "id") and methods we don't implement are
            # silently ignored — buzz-acp is tolerant of a minimal agent.
            continue
        try:
            handler(req)
        except Exception as exc:  # noqa: BLE001
            log(f"handler for {method} raised: {exc!r}")
            if "id" in req:
                send({"jsonrpc": "2.0", "id": req["id"],
                      "error": {"code": -32000, "message": str(exc)}})


if __name__ == "__main__":
    main()
