# Incident Responder

A small service that plays the on-call engineer for the Order Tracker: it
receives Grafana webhook alerts, saves the evidence needed to understand the
problem, and launches the coding assistant in headless mode to investigate.

## How it works

```
Grafana alert ──POST /alerts──▶ responder (port 8001)
                                  │
                                  ├─ saves alert.json, incident.md
                                  ├─ saves context/ : Loki logs, Tempo traces,
                                  │                   Prometheus metrics, app snapshot
                                  └─ launches the coding assistant
                                      ├─ backend "gemini": Gemini API call with the
                                      │                    evidence inlined (default)
                                      └─ backend "zcode":  `zcode --prompt ...` headless
                                          │
                                          └─ agent-response.txt  (the investigation report)
```

Every alert becomes an incident directory under `alerts/<timestamp>-<name>/`:

| File | Contents |
| --- | --- |
| `alert.json` | raw Grafana webhook payload |
| `incident.md` | briefing for the investigating agent |
| `context/logs.txt`, `context/loki-logs.json` | recent Order Tracker logs from Loki |
| `context/trace-*.json` | traces from Tempo (logs' trace ids first) |
| `context/metrics.json` | `http_requests_total` series from Prometheus |
| `context/orders.json` | snapshot of `GET /api/orders` |
| `agent-response.txt` | the coding assistant's final report |
| `agent-meta.json` | command, exit code, duration |

## Run it

```bash
# from the repository root (needs the telemetry stack from docker compose running)
uv run python incident-response/responder.py
```

The service listens on `0.0.0.0:8001`.

## Send a test alert

```bash
curl -X POST http://localhost:8001/alerts \
  -H 'Content-Type: application/json' \
  -d '{"alerts":[{"status":"firing","labels":{"alertname":"ResponderTest","test":"true"},"annotations":{"summary":"Test notification; no incident to fix"}}]}'
```

Then inspect progress and the agent's answer:

```bash
curl -s http://localhost:8001/incidents | python -m json.tool
curl -s http://localhost:8001/incidents/<id> | python -m json.tool
```

Only alerts with `status: "firing"` trigger an investigation; resolved
notifications are acknowledged and ignored.

## Connecting Grafana

Create a webhook contact point pointing at
`http://host.docker.internal:8001/alerts` (the responder runs on the host
because it launches the local coding assistant). Until then the responder can
be exercised with the curl command above.

## Configuration (environment variables)

| Variable | Default | Purpose |
| --- | --- | --- |
| `RESPONDER_AGENT_BACKEND` | `gemini` | `gemini` (API call with inlined evidence) or `zcode` (headless CLI with shell access) |
| `GEMINI_API_KEY` or `GOOGLE_API_KEY` | - | Google AI API key for the gemini backend; alternatively put the key in `incident-response/gemini-api-key.txt` |
| `RESPONDER_GEMINI_MODEL` | `gemini-3.8-flash` | Gemini model id |
| `RESPONDER_NODE_BIN` | `%LOCALAPPDATA%\nvm\v24.19.0\node.exe` | Node >= 22.5 for the ZCode CLI (`node:sqlite`) |
| `RESPONDER_ZCODE_CLI` | `C:\Program Files\ZCode\resources\glm\zcode.cjs` | ZCode CLI entry script |
| `RESPONDER_AGENT_TIMEOUT` | `900` | seconds before the agent run is killed |
| `RESPONDER_LOG_WINDOW` | `15` | minutes of log/trace history to save |
| `RESPONDER_LOKI_URL` / `RESPONDER_TEMPO_URL` / `RESPONDER_PROMETHEUS_URL` / `RESPONDER_APP_URL` | localhost ports | telemetry endpoints |

## Note on the two agent backends

- **gemini** (default) calls the Google Gemini API with the saved evidence
  inlined into the prompt - logs, metric series, trace list and the alert
  payload. It needs a Gemini API key but no local CLI setup, and it always
  works for reporting; it cannot run commands.
- **zcode** launches the ZCode CLI headlessly, which can read the repo and run
  commands for a deeper investigation. It is currently blocked by provider
  error 1113 (the CLI's configured `zai-api` key has no balance; the Z.ai
  Start Plan is only signed into the desktop app). Running `zcode login` once
  writes the plan credentials for the CLI and unblocks this backend.
