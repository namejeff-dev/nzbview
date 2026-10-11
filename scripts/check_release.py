"""Check publishable files without printing possible secret values."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def candidate_files():
    # In a repository check exactly what Git tracks; local config is intentionally ignored.
    result = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'], capture_output=True)
    if result.returncode == 0:
        return [ROOT / name.decode() for name in result.stdout.split(b'\0') if name]
    return [p for p in ROOT.rglob('*') if p.is_file() and not any(
        part in {'.venv', 'venv', '__pycache__', '.pytest_cache', '.git', 'data'} for part in p.relative_to(ROOT).parts)]


def main():
    errors = []
    for path in candidate_files():
        name = path.relative_to(ROOT).as_posix()
        if (name == 'collector_config.json' or name == '.env' or name.endswith(('.db', '.db-wal', '.db-shm', '.log', '.zip', '.collector.lock'))):
            errors.append(f'Private/runtime file included: {name}')
            continue
        if path.suffix not in {'.py', '.md', '.json', '.yml', '.txt', '.html', '.js'}:
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        if re.search(r'(?i)[A-Z]:\\(?:Users\\(?!YOUR_USER)[^\\\s"\']+|JellyfinMedia|SAB Script)', text):
            errors.append(f'Personal installation path: {name}')
        if path.name.endswith('.example.json'):
            data = json.loads(text)
            for key, value in data.items():
                if 'api_key' in key and value and not str(value).startswith('YOUR_'):
                    errors.append(f'Non-placeholder key in example: {name}:{key}')
        for match in re.finditer(r'(?i)(?:api[_-]?key|password|access_token)\s*[=:]\s*["\']([a-z0-9]{24,})["\']', text):
            errors.append(f'Possible credential literal: {name} (value withheld)')
    if errors:
        print('\n'.join(errors))
        return 1
    print('Release files checked: no runtime data, personal installation paths or matching credential literals.')
    print('This narrow check complements manual review; it is not a general-purpose secret scanner.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
