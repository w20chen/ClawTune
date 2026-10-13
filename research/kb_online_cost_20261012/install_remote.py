"""Install the study environment without inheriting unrelated package proxies."""
import os
from pathlib import Path
import subprocess

base = Path.home() / '.cache/clawtune-kb-online-20261012'
env = {k: v for k, v in os.environ.items()
       if not k.startswith(('UV_', 'PIP_')) and not k.lower().endswith('_proxy')}
env.update(UV_HTTP_TIMEOUT='20', UV_HTTP_RETRIES='1')
raise SystemExit(subprocess.call([
    str(Path.home() / '.local/bin/uv'), '--no-config', 'pip', 'install',
    '--index-url', 'https://pypi.tuna.tsinghua.edu.cn/simple',
    '--python', str(base / 'venv/bin/python'), '-r', str(base / 'requirements.txt'),
], env=env))
