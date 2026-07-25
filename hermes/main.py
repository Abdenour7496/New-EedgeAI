import os
import json
import time
import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

app = FastAPI(title="Hermes Agent")

MODEL_MAP = {
    "hermes-haiku":  "claude-haiku-4-5-20251001",
    "hermes-sonnet": "claude-sonnet-4-6",
    "hermes-opus":   "claude-opus-4-7",
}
HERMES_MODELS = list(MODEL_MAP.keys())
HERMES_MODEL  = os.getenv("HERMES_MODEL", "hermes-sonnet")


def _build_prompt(messages: list) -> str:
    parts = []
    system = None
    for msg in messages:
        role    = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            system = content
        elif role == "assistant":
            parts.append(f"Assistant: {content}")
        else:
            parts.append(content if not parts else f"Human: {content}")
    body = "\n\n".join(parts)
    return f"{system}\n\n{body}" if system else body


def _sse_chunk(text: str, model: str, finish: str | None = None) -> str:
    return "data: " + json.dumps({
        "id":      f"chatcmpl-hermes-{int(time.time())}",
        "object":  "chat.completion.chunk",
        "created": int(time.time()),
        "model":   model,
        "choices": [{"index": 0,
                     "delta": {"content": text} if text else {},
                     "finish_reason": finish}],
    }) + "\n\n"


def _claude_cmd(model: str) -> list[str]:
    return [
        "claude", "--print", "--model", model,
        "--output-format", "text",
    ]


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/models")
def models_list():
    return {"models": HERMES_MODELS}


@app.get("/v1/models")
def v1_models():
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "owned_by": "hermes"} for m in HERMES_MODELS],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body   = await request.json()
    alias  = body.get("model", HERMES_MODEL)
    model  = MODEL_MAP.get(alias, "claude-sonnet-4-6")
    prompt = _build_prompt(body.get("messages", []))
    cmd    = _claude_cmd(model)

    if body.get("stream"):
        async def stream_gen():
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                proc.stdin.write(prompt.encode())
                await proc.stdin.drain()
                proc.stdin.close()

                while True:
                    chunk = await proc.stdout.read(128)
                    if not chunk:
                        break
                    yield _sse_chunk(chunk.decode(errors="replace"), alias)

                await proc.wait()
                if proc.returncode != 0:
                    err = await proc.stderr.read()
                    yield f'data: {{"error": "claude: {err.decode()[:120]}"}}\n\n'
                    return
                yield _sse_chunk("", alias, finish="stop")
                yield "data: [DONE]\n\n"
            except Exception as exc:
                yield f'data: {{"error": "{exc}"}}\n\n'

        return StreamingResponse(stream_gen(), media_type="text/event-stream")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode()), timeout=120
        )
        if proc.returncode != 0:
            raise HTTPException(status_code=502, detail=stderr.decode()[:300])

        text = stdout.decode(errors="replace").strip()
        return JSONResponse({
            "id":      f"chatcmpl-hermes-{int(time.time())}",
            "object":  "chat.completion",
            "created": int(time.time()),
            "model":   alias,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="claude CLI timeout")
