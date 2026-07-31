# Deployment

## Local Development

```bash
python -m pip install -e "services/scheduler[dev]"

cd packages/openclaw-plugin
npm install
npm run build
cd ../..
```

Start sidecar:

```bash
cp .env.example .env
python -m agent_scheduler.main --host 127.0.0.1 --port 8765
```

Link plugin:

```bash
openclaw plugins install --link ./packages/openclaw-plugin
openclaw plugins enable agent-scheduler
```

## Docker Sidecar

```bash
docker compose up --build scheduler
```

This starts the sidecar. Install and configure the OpenClaw plugin separately.

## Package Builds

Python sidecar:

```bash
cd services/scheduler
python -m build
```

Plugin tarball:

```bash
cd packages/openclaw-plugin
npm pack
```

## Prerequisites

- Python 3.10+
- Node.js and npm
- OpenClaw CLI 2026.7.1 or newer
- Docker for SWE-Rebench

Windows notes:

- Use `npm.cmd` or `openclaw.cmd` if PowerShell blocks `.ps1` shims.
- Use `--basetemp .pytest-tmp-root` for pytest.

## Validate

```bash
python tools/validate_contracts.py
python -m pytest tests -q --basetemp .pytest-tmp-root

cd packages/openclaw-plugin
npm test
npm run typecheck
```
