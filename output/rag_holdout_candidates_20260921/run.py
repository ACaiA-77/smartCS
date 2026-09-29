"""Capture exact commands, UTF-8 output and exit status for candidate authoring."""
import json
import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
out = root / 'artifacts/rag_holdout_candidates_20260921'
out.mkdir(parents=True, exist_ok=True)
mode = sys.argv[1]
commands = {
    'check': [sys.executable, '-X', 'utf8', str(Path(__file__).with_name('build_candidates.py')), '--check'],
    'build': [sys.executable, '-X', 'utf8', str(Path(__file__).with_name('build_candidates.py'))],
    'validate': [sys.executable, '-X', 'utf8', str(Path(__file__).with_name('validate_package.py'))],
    'validate_complete': [sys.executable, '-X', 'utf8', str(Path(__file__).with_name('validate_package.py'))],
    'diff': ['git', 'diff', '--check'],
    'status': ['git', 'status', '--short'],
}
argv = commands[mode]
env = {**os.environ, 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'PYTHONIOENCODING': 'utf-8'}
log_path = out / f'{mode}.log'
if log_path.exists():
    raise FileExistsError(log_path)
with log_path.open('w', encoding='utf-8') as log:
    result = subprocess.run(argv, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
(out / f'{mode}_command.json').write_text(json.dumps({'argv': argv, 'cwd': str(root),
    'exit_code': result.returncode, 'output': str(log_path.relative_to(root)),
    'env': {k: env[k] for k in ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'PYTHONIOENCODING')}}, indent=2) + '\n', encoding='utf-8')
print(f'{mode}: exit={result.returncode}; output={log_path}')
sys.exit(result.returncode)
