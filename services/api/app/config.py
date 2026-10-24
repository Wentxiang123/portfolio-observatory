from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[3]
PRIVATE = ROOT / 'var' / 'private'
PRIVATE.mkdir(parents=True, exist_ok=True)
env_file = ROOT / '.env'
if env_file.exists():
    for line in env_file.read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip())
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if not DATABASE_URL.startswith('postgresql'):
    raise RuntimeError('请先运行 scripts/setup.ps1，或在 .env 配置 PostgreSQL DATABASE_URL。')

