"""Incident responder for the Order Tracker.

Receives Grafana webhook alerts at POST /alerts, saves the evidence an
on-call engineer (or agent) needs to understand the problem - affected
endpoint, recent logs from Loki, traces from Tempo, metrics from
Prometheus - and then launches the coding assistant in headless mode to
investigate. The agent's answer is captured next to the evidence.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

BASE_DIR = Path(__file__).resolve().parent
ALERTS_DIR = BASE_DIR / "alerts"
REPO_ROOT = BASE_DIR.parent

APP_URL = os.getenv("RESPONDER_APP_URL", "http://localhost:8000")
LOKI_URL = os.getenv("RESPONDER_LOKI_URL", "http://localhost:3100")
TEMPO_URL = os.getenv("RESPONDER_TEMPO_URL", "http://localhost:3200")
PROMETHEUS_URL = os.getenv("RESPONDER_PROMETHEUS_URL", "http://localhost:9090")

NODE_BIN = os.getenv("RESPONDER_NODE_BIN", str(Path.home() / "AppData/Local/nvm/v24.19.0/node.exe"))
ZCODE_CLI = os.getenv(
    "RESPONDER_ZCODE_CLI", r"C:\Program Files\ZCode\resources\glm\zcode.cjs"
)
AGENT_BACKEND = os.getenv("RESPONDER_AGENT_BACKEND", "gemini")  # "gemini" or "zcode"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODEL = os.getenv("RESPONDER_GEMINI_MODEL", "gemini-3.8-flash")
MAX_TOOL_STEPS = int(os.getenv("RESPONDER_GEMINI_MAX_STEPS", "25"))
AGENT_WALL_CLOCK_S = int(os.getenv("RESPONDER_AGENT_WALL_CLOCK", "900"))
GEMINI_SYSTEM_PROMPT = f"""You are the on-call incident responder for the Order Tracker service,
a small FastAPI + SQLite order management app whose source lives in {REPO_ROOT}
(app code in app/main.py, tests in tests/). It runs on this machine via Docker Compose.

You have tools:
- run_command: run a shell command in the repository root (inspect code, curl the app,
  run tests, docker compose). Use it freely; commands are NOT interactive.
- read_file / write_file: read and edit source files.

When the alert is a real incident, fix the ROOT CAUSE in code (never mask a bug by
editing the database rows), then rebuild and restart the app with:

    docker compose up --build -d app

then verify the previously failing request now succeeds with curl, and run the test
suite with: uv run pytest -q

Finish with a concise incident report: what happened, root cause with evidence, what
you changed, and how you verified the fix. End with a final line in the format:
Status: <firing|resolved|escalated|no-incident>
"""
GEMINI_TOOLS = [
    {
        "functionDeclarations": [
            {
                "name": "run_command",
                "description": "Run a shell command in the repository working directory and return stdout/stderr and exit code. Use for inspecting code, curl-ing the app, running tests, and docker compose operations.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "The shell command to run"},
                        "timeout_s": {"type": "integer", "description": "Timeout in seconds (default 180, max 600)"},
                    },
                    "required": ["command"],
                },
            },
            {
                "name": "read_file",
                "description": "Read a text file (first 8000 characters). Paths are relative to the repository root or absolute.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "File path"}},
                    "required": ["path"],
                },
            },
            {
                "name": "write_file",
                "description": "Create or overwrite a text file with the given content. Use to apply a code fix.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path"},
                        "content": {"type": "string", "description": "Full new file content"},
                    },
                    "required": ["path", "content"],
                },
            },
        ]
    }
]
AGENT_TIMEOUT_S = int(os.getenv("RESPONDER_AGENT_TIMEOUT", "900"))
LOG_WINDOW_MINUTES = int(os.getenv("RESPONDER_LOG_WINDOW", "15"))

app = FastAPI(title="order-tracker-incident-responder")

_lock = threading.Lock()
_incidents: dict[str, dict] = {}


def _now_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")[:-3]


def _slugify(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")[:60] or "alert"


def _fetch(url: str, **kwargs):
    try:
        resp = httpx.get(url, timeout=10, **kwargs)
        resp.raise_for_status()
        return resp
    except Exception as exc:  # noqa: BLE001 - evidence gathering must not crash the handler
        print(f"[responder] fetch failed: {url}: {exc}")
        return None


def gather_context(ctx_dir: Path, labels: dict) -> dict:
    """Save logs, traces, metrics and app state for the investigation."""
    context = {}
    saved = ctx_dir / "context"
    saved.mkdir(parents=True, exist_ok=True)

    # Recent application logs from Loki (structured metadata carries trace ids).
    loki = _fetch(
        f"{LOKI_URL}/loki/api/v1/query_range",
        params={
            "query": '{service_name="order-tracker"}',
            "since": f"{LOG_WINDOW_MINUTES}m",
            "limit": 200,
            "direction": "backward",
        },
    )
    if loki is not None:
        (saved / "loki-logs.json").write_text(loki.text, encoding="utf-8")
        lines = []
        trace_ids = []
        for stream in loki.json().get("data", {}).get("result", []):
            if (tid := stream.get("stream", {}).get("trace_id")) and tid not in trace_ids:
                trace_ids.append(tid)
            for ts, line in stream.get("values", []):
                lines.append(f"{ts} {line}")
        (saved / "logs.txt").write_text("\n".join(lines[:200]), encoding="utf-8")
        context["log_lines"] = min(len(lines), 200)
        context["recent_trace_ids"] = trace_ids[:5]

    # Fetch the most recent traces from Tempo, including any referenced by logs.
    tempo_traces = []
    search = _fetch(
        f"{TEMPO_URL}/api/search",
        params={"tags": "service.name=order-tracker", "limit": 5},
    )
    if search is not None:
        (saved / "tempo-search.json").write_text(search.text, encoding="utf-8")
        tempo_traces = [
            t.get("traceID")
            for t in search.json().get("traces", [])
            if t.get("traceID")
        ]
    for tid in (trace_ids[:2] + tempo_traces[:3])[:4]:
        trace = _fetch(f"{TEMPO_URL}/api/traces/{tid}", headers={"Accept": "application/json"})
        if trace is not None:
            (saved / f"trace-{tid}.json").write_text(trace.text, encoding="utf-8")
            context.setdefault("traces_saved", []).append(tid)

    # Request metrics from Prometheus.
    metrics = _fetch(
        f"{PROMETHEUS_URL}/api/v1/query", params={"query": "http_requests_total"}
    )
    if metrics is not None:
        (saved / "metrics.json").write_text(metrics.text, encoding="utf-8")
        series = metrics.json().get("data", {}).get("result", [])
        context["metric_series"] = [
            {
                "route": s["metric"].get("http_route"),
                "status_code": s["metric"].get("http_response_status_code"),
                "value": s["value"][1],
            }
            for s in series
        ]

    # Current orders from the app itself.
    orders = _fetch(f"{APP_URL}/api/orders")
    if orders is not None:
        (saved / "orders.json").write_text(orders.text, encoding="utf-8")
        context["orders_snapshot"] = True

    return context


def build_briefing(incident_dir: Path, alert: dict, context: dict) -> str:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    endpoint = labels.get("http_route", "not specified in the alert")
    status_code = labels.get("http_response_status_code", "?")
    lines = [
        "# Incident briefing",
        "",
        f"- Incident directory: {incident_dir}",
        f"- Received (UTC): {datetime.now(timezone.utc).isoformat()}",
        f"- Alert: {labels.get('alertname', 'unknown')}",
        f"- Alert status: {alert.get('status', 'firing')}",
        f"- Summary: {annotations.get('summary', 'n/a')}",
        f"- Affected endpoint: {endpoint}",
        f"- HTTP status code from the metric: {status_code}",
        f"- Evaluation window: last {LOG_WINDOW_MINUTES} minutes of saved evidence",
        "",
        "## Evidence saved in this directory",
        "",
        "- `alert.json` - the raw Grafana webhook alert entry",
        "- `context/logs.txt` and `context/loki-logs.json` - recent app logs from Loki",
        "- `context/trace-*.json` - traces from Tempo (error traces first)",
        "- `context/metrics.json` - http_requests_total series from Prometheus",
        "- `context/orders.json` - current orders from the app",
        "",
        "## Environment cheatsheet",
        "",
        f"- App: {APP_URL} (FastAPI, code in `app/main.py`, tests in `tests/`)",
        f"- Loki UI/API: {LOKI_URL} (query: {{service_name=\"order-tracker\"}})",
        f"- Tempo API: {TEMPO_URL}/api/traces/<trace_id>",
        f"- Prometheus API: {PROMETHEUS_URL}/api/v1/query?query=http_requests_total",
        "- Docker: `docker compose ps`, `docker compose logs app`",
        "",
        "## Your task",
        "",
        "You are the on-call engineer. Investigate the root cause of this alert using",
        "the evidence above, the service code, and live commands. Then reply with a",
        "concise incident report as your final message containing:",
        "1. What happened and the affected endpoint",
        "2. The root cause (with evidence: log lines, trace ids, metric values)",
        "3. Your action: what you fixed, or why you escalate to the developers",
        "",
        "If the alert labels contain test=true, this is only a pipeline test:",
        "verify the evidence is readable and reply briefly that no incident needs fixing.",
        "",
        "End your reply with a final line in the format: `Status: <firing|resolved|escalated|no-incident>`",
    ]
    return "\n".join(lines)


def _agent_command() -> list[str]:
    cmd = [NODE_BIN, ZCODE_CLI, "--prompt"]
    if shutil.which(NODE_BIN) is None and not os.path.exists(NODE_BIN):
        # Fall back to whatever node is on PATH.
        cmd = ["node", ZCODE_CLI, "--prompt"]
    return cmd


def _gemini_api_key() -> str | None:
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if key:
        return key.strip()
    key_file = BASE_DIR / "gemini-api-key.txt"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip() or None
    return None


def _evidence_prompt(incident_dir: Path, alert: dict) -> str:
    """Build a self-contained prompt: the Gemini API has no shell or file access,
    so the saved evidence is inlined."""
    ctx = incident_dir / "context"

    logs = "(log file missing)"
    logs_file = ctx / "logs.txt"
    if logs_file.exists():
        lines = logs_file.read_text(encoding="utf-8").splitlines()
        logs = "\n".join(lines[-120:]) or "(no log lines recorded in the evidence window)"

    metrics = "(metrics file missing)"
    metrics_file = ctx / "metrics.json"
    if metrics_file.exists():
        try:
            series = json.loads(metrics_file.read_text(encoding="utf-8")).get("data", {}).get("result", [])
            rows = [
                f"- {s['metric'].get('http_route')} -> HTTP {s['metric'].get('http_response_status_code')}: {s['value'][1]} requests"
                for s in series
            ]
            metrics = "\n".join(rows) or "(no metric series recorded)"
        except Exception:  # noqa: BLE001
            metrics = metrics_file.read_text(encoding="utf-8")[:2000]

    traces = sorted(ctx.glob("trace-*.json"))
    trace_note = "\n".join(f"- {p.name} (saved next to this report)" for p in traces) or "- none captured"

    return f"""You are the on-call incident responder for the Order Tracker service
(a small FastAPI + SQLite order management app). A Grafana alert fired and the
on-call engineer must report what happened.

## Alert payload
{json.dumps(alert, indent=2)}

## Request metric (Prometheus counter http_requests_total, latest values)
{metrics}

## Recent application logs (Loki, tail of the evidence window)
{logs}

## Traces captured from Tempo
{trace_note}

## Your task
Write a concise incident report:
1. What happened and the affected endpoint.
2. The root cause, citing the evidence (log lines, metric values, trace ids).
3. Your action: fix it here, or escalate to the developers - and why.

If the alert labels contain test=true, this is only a pipeline test: confirm
the evidence is readable and reply briefly that no incident needs fixing.

End your reply with a final line in the format: Status: <firing|resolved|escalated|no-incident>
"""


def _resolve_repo_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else (REPO_ROOT / path)


def _execute_tool(name: str, args: dict) -> dict:
    try:
        if name == "run_command":
            command = str(args.get("command", ""))
            timeout = max(5, min(int(args.get("timeout_s", 180)), 600))
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            out = proc.stdout or ""
            if proc.stderr:
                out += "\n[stderr]\n" + proc.stderr
            return {"exit_code": proc.returncode, "output": out.strip()[-6000:]}
        if name == "read_file":
            path = _resolve_repo_path(str(args.get("path", "")))
            return {"path": str(path), "content": path.read_text(encoding="utf-8", errors="replace")[:8000]}
        if name == "write_file":
            path = _resolve_repo_path(str(args.get("path", "")))
            content = str(args.get("content", ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return {"path": str(path), "bytes_written": len(content)}
        return {"error": f"unknown tool: {name}"}
    except Exception as exc:  # noqa: BLE001 - tool failures go back to the model
        return {"error": f"{type(exc).__name__}: {exc}"}


def _gemini_generate(key: str, body: dict, meta: dict):
    """POST to Gemini with retry on transient status codes. Returns (data, error_text)."""
    for attempt in range(1, 9):
        try:
            resp = httpx.post(
                GEMINI_API_URL.format(model=GEMINI_MODEL),
                headers={"x-goog-api-key": key},
                json=body,
                timeout=180,
            )
        except Exception as exc:  # noqa: BLE001
            return None, f"Gemini API call failed: {exc}"
        meta["http_status"] = resp.status_code
        if resp.status_code in (429, 500, 503) and attempt < 8:
            # Free tier: 5 requests/min. The 429 body often carries "Please retry in Xs".
            wait_s = 20 * attempt
            match = re.search(r"retry in ([\d.]+)s", resp.text)
            if match:
                wait_s = max(wait_s, min(float(match.group(1)) + 1, 90))
            meta.setdefault("retries", []).append({"status": resp.status_code, "waited_s": round(wait_s, 1)})
            print(f"[responder] Gemini {resp.status_code}, retrying in {wait_s:.0f}s")
            time.sleep(wait_s)
            continue
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return None, f"Gemini API returned non-JSON response ({resp.status_code}): {resp.text[:500]}"
        if resp.status_code != 200 or "error" in data:
            err = data.get("error", {}).get("message", resp.text[:500]) if isinstance(data, dict) else str(data)[:500]
            return None, f"Gemini API error ({resp.status_code}): {err}"
        return data, None
    return None, "Gemini API kept failing after retries"


def run_gemini_agent(incident_dir: Path, alert: dict) -> tuple[str, dict]:
    meta: dict = {"backend": "gemini", "model": GEMINI_MODEL}
    key = _gemini_api_key()
    if not key:
        return (
            "Gemini backend selected but no API key was found. Set the GEMINI_API_KEY "
            "(or GOOGLE_API_KEY) environment variable, or create "
            "incident-response/gemini-api-key.txt containing the key, and resend the alert.",
            meta,
        )
    contents: list = [{"role": "user", "parts": [{"text": _evidence_prompt(incident_dir, alert)}]}]
    body = {
        "contents": contents,
        "tools": GEMINI_TOOLS,
        "systemInstruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
    }
    started = time.monotonic()
    steps = 0
    while True:
        steps += 1
        if steps > MAX_TOOL_STEPS or time.monotonic() - started > AGENT_WALL_CLOCK_S:
            return "(agent stopped: tool step or wall-clock limit reached before a final report)", meta
        data, error = _gemini_generate(key, body, meta)
        if error:
            return error, meta
        usage = data.get("usageMetadata", {})
        meta["usage"] = {
            k: usage.get(k)
            for k in ("promptTokenCount", "candidatesTokenCount", "totalTokenCount")
        }
        candidates = data.get("candidates") or []
        parts = (candidates[0].get("content") or {}).get("parts", []) if candidates else []
        calls = [p["functionCall"] for p in parts if "functionCall" in p]
        texts = [p.get("text", "") for p in parts if p.get("text")]
        if not calls:
            return ("\n".join(texts).strip() or "(empty response from Gemini)"), meta
        # Hand each function call's result back to the model.
        body["contents"] = body["contents"] + [
            {"role": "model", "parts": parts},
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": call["name"],
                            "response": _execute_tool(call["name"], call.get("args") or {}),
                        }
                    }
                    for call in calls
                ],
            },
        ]
        for call in calls:
            print(f"[responder] agent step {steps}: {call['name']} {str(call.get('args'))[:120]}")


def run_zcode_agent(incident_dir: Path) -> tuple[str, dict]:
    prompt = (
        "You are the on-call incident responder for the Order Tracker service. "
        f"Read the incident briefing at {incident_dir / 'incident.md'} and the evidence "
        f"under {incident_dir / 'context'}. Investigate as instructed there and give your "
        "incident report as your final message. Work inside "
        f"{REPO_ROOT}. Do not start long-running services."
    )
    cmd = _agent_command() + [prompt, "--cwd", str(REPO_ROOT), "--no-color"]
    meta: dict = {"backend": "zcode", "command": cmd}
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            out, _ = proc.communicate(timeout=AGENT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            meta["timed_out"] = True
        meta["exit_code"] = proc.returncode
        return (out or ""), meta
    except Exception as exc:  # noqa: BLE001
        return f"agent launch failed: {exc}", meta


def run_agent(incident: dict) -> None:
    incident_dir = Path(incident["dir"])
    started_at = time.monotonic()
    print(f"[responder] launching agent ({AGENT_BACKEND}) for {incident['id']}")
    incident["agent_status"] = "running"
    if AGENT_BACKEND == "gemini":
        out, extra_meta = run_gemini_agent(incident_dir, incident.get("alert", {}))
    else:
        out, extra_meta = run_zcode_agent(incident_dir)
    meta = {
        "started": datetime.now(timezone.utc).isoformat(),
        **extra_meta,
        "duration_s": round(time.monotonic() - started_at, 1),
        "finished": datetime.now(timezone.utc).isoformat(),
    }
    (incident_dir / "agent-response.txt").write_text(out, encoding="utf-8")
    (incident_dir / "agent-meta.json").write_text(
        json.dumps(meta, indent=2, default=str), encoding="utf-8"
    )
    incident["agent_status"] = "done"
    incident["response_tail"] = out.strip()[-2000:]
    print(f"[responder] agent finished for {incident['id']}")


def create_incident(alert: dict, envelope: dict) -> dict:
    labels = alert.get("labels", {})
    incident_id = f"{_now_slug()}-{_slugify(labels.get('alertname', 'alert'))}"
    incident_dir = ALERTS_DIR / incident_id
    incident_dir.mkdir(parents=True, exist_ok=True)

    (incident_dir / "alert.json").write_text(
        json.dumps({"envelope": envelope, "alert": alert}, indent=2),
        encoding="utf-8",
    )
    print(f"[responder] incident {incident_id}: gathering evidence")
    context = gather_context(incident_dir, labels)
    (incident_dir / "incident.md").write_text(
        build_briefing(incident_dir, alert, context), encoding="utf-8"
    )

    incident = {
        "id": incident_id,
        "dir": str(incident_dir),
        "alertname": labels.get("alertname", "unknown"),
        "endpoint": labels.get("http_route"),
        "status_code": labels.get("http_response_status_code"),
        "summary": alert.get("annotations", {}).get("summary", ""),
        "alert": alert,
        "backend": AGENT_BACKEND,
        "context": context,
        "agent_status": "pending",
        "received": datetime.now(timezone.utc).isoformat(),
    }
    with _lock:
        _incidents[incident_id] = incident
    return incident


@app.post("/alerts")
async def receive_alerts(request: Request):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    envelope = payload if isinstance(payload, dict) else {"raw": payload}
    accepted = []
    skipped = []
    for alert in envelope.get("alerts", []) or []:
        if isinstance(alert, str):
            try:
                alert = json.loads(alert)
            except json.JSONDecodeError:
                continue
        if not isinstance(alert, dict):
            continue
        if alert.get("status", "firing") not in ("firing",):
            continue  # resolved notifications need no investigation
        labels = alert.get("labels", {})
        if labels.get("alertname") == "DatasourceNoData":
            # Synthetic instance Grafana emits while a rule has no data - that is the
            # quiet period our alert rule is configured to tolerate, not an incident.
            skipped.append("DatasourceNoData")
            continue
        incident = create_incident(alert, envelope)
        threading.Thread(target=run_agent, args=(incident,), daemon=True).start()
        accepted.append(incident["id"])
    return JSONResponse(
        {"received": len(accepted), "incidents": accepted, "skipped": skipped},
        status_code=202,
    )


@app.get("/incidents")
async def list_incidents():
    with _lock:
        return [
            {
                "id": i["id"],
                "alertname": i["alertname"],
                "endpoint": i["endpoint"],
                "status_code": i["status_code"],
                "agent_status": i["agent_status"],
                "received": i["received"],
            }
            for i in _incidents.values()
        ]


@app.get("/incidents/{incident_id}")
async def get_incident(incident_id: str):
    with _lock:
        incident = _incidents.get(incident_id)
    if incident is None:
        return JSONResponse({"error": "unknown incident"}, status_code=404)
    response_file = Path(incident["dir"]) / "agent-response.txt"
    incident = dict(incident)
    incident["response"] = (
        response_file.read_text(encoding="utf-8") if response_file.exists() else None
    )
    return incident


if __name__ == "__main__":
    ALERTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[responder] listening on :8001, incidents in {ALERTS_DIR}")
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="warning")
