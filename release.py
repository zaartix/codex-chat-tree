#!/usr/bin/env python3
"""Publish a release: python3 release.py 0.3.0

Runs the tests, sets the plugin version, commits, tags v<version> and pushes.
Users update by running the installer again.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
manifest = root / 'plugins' / 'chat-tree' / '.codex-plugin' / 'plugin.json'


def run(*args):
    subprocess.run(args, cwd=root, check=True)


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r'\d+\.\d+\.\d+', sys.argv[1]):
        raise SystemExit('Usage: python3 release.py X.Y.Z')
    version = sys.argv[1]
    data = json.loads(manifest.read_text())
    if tuple(map(int, version.split('.'))) <= tuple(map(int, data['version'].split('.'))):
        raise SystemExit('Version must be greater than ' + data['version'])
    if subprocess.run(['git', 'symbolic-ref', '--short', 'HEAD'], cwd=root, capture_output=True, text=True).stdout.strip() != 'main':
        raise SystemExit('Release from main')
    run(sys.executable, '-m', 'unittest', 'discover', '-s', 'tests')
    data['version'] = version
    manifest.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    run('git', 'add', '-A')
    run('git', 'commit', '-m', 'release: ' + version)
    run('git', 'tag', 'v' + version)
    run('git', 'push', 'origin', 'main', 'v' + version)
    print('Released ' + version + '. Users update by running the installer again.')


if __name__ == '__main__':
    main()
