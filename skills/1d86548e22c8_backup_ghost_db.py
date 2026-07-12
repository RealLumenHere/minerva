#!/usr/bin/env python3
"""backup_ghost_db: mandatory Ghost DB backup per standing rule."""
import json, shutil, sys
from datetime import datetime, timezone
from pathlib import Path

src = Path('/home/lumen/ghost-content/data/ghost.db')
if not src.exists():
    print(json.dumps({'ok': False, 'error': f'ghost.db missing at {src}'}))
    sys.exit(1)
ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
dst = src.with_suffix(f'.db.bak.{ts}')
shutil.copy2(src, dst)
print(json.dumps({'ok': True, 'backup': str(dst), 'size_bytes': dst.stat().st_size}))
sys.exit(0)
