# Modified for Norma.
"""Install public wheels and run real ADK tests outside this checkout."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    package = Path(__file__).resolve().parents[1]
    wheels = list((package / 'dist').glob('norma_sdk-*.whl'))
    if len(wheels) != 1:
        raise SystemExit('Expected one built Norma wheel')
    versions = sys.argv[1:] or ['1.7.0', '2.12.0']
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('GOOGLE_', 'GEMINI_', 'OTEL_')) and key != 'PYTHONPATH'}
    environment['PYTHONNOUSERSITE'] = '1'
    with tempfile.TemporaryDirectory(prefix='norma-real-adk-') as directory:
        root = Path(directory)
        script = root / 'check_google_adk.py'
        shutil.copy2(package / 'scripts/check_google_adk.py', script)
        wheel = root / wheels[0].name
        shutil.copy2(wheels[0], wheel)
        for version in versions:
            venv = root / version
            subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True)
            python = str(venv / 'bin/python')
            subprocess.run([python, '-m', 'pip', 'install', '--disable-pip-version-check',
                            '--index-url', 'https://pypi.org/simple', str(wheel),
                            f'google-adk=={version}', 'pytest', 'pytest-asyncio'],
                           check=True, cwd=root, env=environment)
            subprocess.run([python, str(script)], check=True, cwd=root, env=environment)


if __name__ == '__main__':
    main()
