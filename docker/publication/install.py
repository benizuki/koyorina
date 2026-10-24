"""Install declared dependencies without invoking the generated project's build backend."""
import subprocess
import sys
import tomllib
with open('/src/pyproject.toml', 'rb') as handle:
    config = tomllib.load(handle)
requirements = config.get('project', {}).get('dependencies', [])
if not isinstance(requirements, list) or not all(isinstance(r, str) and not r.startswith('-') for r in requirements):
    raise ValueError('Invalid dependency list')
subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-cache-dir',
    'fastapi', 'uvicorn', 'httpx', *requirements], check=True)
