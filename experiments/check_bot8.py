"""Run repository regressions in isolated processes; preserve full logs per suite.

Usage: python3 experiments/check_bot8.py --logs /tmp/bot8-checks
Runtime dependencies: bot8/requirements.txt, snowballstemmer, ruff, ansible-core.
No credentials, external services, embeddings downloads or deployment required.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--logs', default='ci-logs/local')
    args = parser.parse_args()
    logs = Path(args.logs).resolve()
    logs.mkdir(parents=True, exist_ok=True)
    python = sys.executable
    # Each version imports its own filosof/rag modules, so never discover all versions
    # into a single Python process (their module names intentionally match).
    suites = sorted((ROOT / 'experiments').glob('test_*.py'))
    smokes = sorted((ROOT / 'experiments').glob('smoke_bot*_main.py'))
    checks = [(path.stem, [python, str(path), '-v']) for path in suites]
    checks += [(path.stem, [python, str(path)]) for path in smokes]
    checks += [
        ('structure', [python, 'experiments/validate_structure.py']),
        ('markdown-existing', [python, 'experiments/check_markdown_links.py']),
        ('markdown-bot8', [python, 'experiments/check_markdown_links.py',
                           'Прочее/README.md', 'Прочее/bot8/README.md', 'Прочее/bot8/RAG_IDEAS.md',
                           'experiments/issue19-plan.md']),
        ('lint', [python, '-m', 'ruff', 'check', '--isolated', '--select', 'E9,F63,F7,F82,F401,F841',
                  'Прочее/bot8', *[str(p) for p in suites if 'bot8' in p.stem],
                  'experiments/smoke_bot8_main.py', 'experiments/check_bot8.py']),
        ('compile', [python, '-m', 'compileall', '-q', 'Прочее/bot8']),
        ('ansible', ['ansible-playbook', '--syntax-check', '-i',
                     'Прочее/bot8/ansible/inventory.example.ini', 'Прочее/bot8/ansible/playbook.yml']),
    ]
    failures, tests = [], 0
    env = dict(os.environ, PYTHONUNBUFFERED='1', PYTHONHASHSEED='0')
    for name, command in checks:
        path = logs / f'{name}.log'
        try:
            with path.open('w', encoding='utf-8') as log:
                result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                        timeout=120, check=False)
            status = result.returncode
        except (subprocess.TimeoutExpired, OSError) as error:
            with path.open('a', encoding='utf-8') as log:
                log.write(f'\nCHECK FAILED: {error}\n')
            status = 1
        content = path.read_text(encoding='utf-8', errors='replace')
        match = re.search(r'^Ran (\d+) tests? in ', content, re.MULTILINE)
        if match:
            tests += int(match[1])
        print(f'{"PASS" if status == 0 else "FAIL"} {name}: {path}', flush=True)
        if status:
            failures.append(name)
            # Full output is kept in the artifact; bound console output on failure.
            print('\n'.join(content.splitlines()[-60:]), flush=True)
    print(f'{len(checks)} checks, {tests} unittest cases, {len(failures)} failures: {failures}', flush=True)
    return bool(failures)


if __name__ == '__main__':
    sys.exit(main())
