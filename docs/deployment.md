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

This only starts the sidecar. You still need to install/configure the OpenClaw
plugin in your OpenClaw environment.

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

## SWE-Rebench

```bash
cp swe_rebench/config.example.yaml swe_rebench/config.yaml
python -m swe_rebench.runner prepare --config swe_rebench/config.yaml
python -m swe_rebench.discover --sample 20 --out swe_rebench/tasks.json
python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --dataset swe_rebench/tasks.json --sample 10 --export
```
