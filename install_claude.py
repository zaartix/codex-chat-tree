#!/usr/bin/env python3
"""Install or update Chat Tree for Claude Code (Claude Desktop, Code tab).

From GitHub:   curl -fsSL https://raw.githubusercontent.com/zaartix/codex-chat-tree/main/install_claude.py | python3 -
From a clone:  python3 install_claude.py              (installs the working copy, for development)
Remove:        python3 install_claude.py --uninstall  (saved trees are kept)

Running the installer again updates the plugin.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = 'zaartix/codex-chat-tree'
MARKETPLACE = 'chat-tree'
PLUGIN = 'chat-tree@' + MARKETPLACE


def fail(message):
    raise SystemExit('Chat Tree: ' + message)


def find_claude():
    if shutil.which('claude'):
        return shutil.which('claude')
    # Claude Desktop ships its own Claude Code; take the newest one.
    bundled = sorted((Path.home() / 'Library' / 'Application Support' / 'Claude' / 'claude-code').glob(
        '*/*/claude.app/Contents/MacOS/claude'), key=lambda p: p.stat().st_mtime)
    if bundled:
        return str(bundled[-1])
    fail('Claude Code not found. Install Claude Desktop or the Claude Code CLI, then run the installer again.')


def check_python():
    # Claude starts the server and the hook with `python3` from PATH, not with this interpreter.
    python = shutil.which('python3')
    if not python:
        fail('python3 not found on PATH. Install Python 3.9 or newer.')
    ok = subprocess.run([python, '-c', 'import sys, sqlite3; print(sys.version_info >= (3, 9))'],
                        capture_output=True, text=True).stdout.strip()
    if ok != 'True':
        fail(python + ' is older than Python 3.9 or lacks sqlite3.')


def claude(*args, check=True):
    result = subprocess.run([CLAUDE, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if check and result.returncode:
        fail('`claude ' + ' '.join(args) + '` failed:\n' + (result.stderr or result.stdout).strip())
    return result.stdout


def configured_source():
    for market in json.loads(claude('plugin', 'marketplace', 'list', '--json')):
        if market['name'] == MARKETPLACE:
            return market
    return None


def installed():
    return next((p for p in json.loads(claude('plugin', 'list', '--json')) if p['id'] == PLUGIN), None)


def main():
    global CLAUDE
    CLAUDE = find_claude()
    here = Path(__file__).resolve().parent if '__file__' in globals() else None
    local = here if here and (here / '.claude-plugin' / 'marketplace.json').is_file() else None
    current = configured_source()
    if '--uninstall' in sys.argv:
        if installed():
            claude('plugin', 'uninstall', PLUGIN)
        if current is not None:
            claude('plugin', 'marketplace', 'remove', MARKETPLACE)
        print('Chat Tree removed. Saved trees remain in ~/.claude/chat-tree.')
        return
    check_python()
    same = current is not None and (os.path.realpath(current.get('path') or '') == str(local) if local
                                    else current.get('repo') == REPO)
    if current is not None and not same:
        claude('plugin', 'marketplace', 'remove', MARKETPLACE)
    if not same:
        claude('plugin', 'marketplace', 'add', str(local) if local else REPO)
    else:
        claude('plugin', 'marketplace', 'update', MARKETPLACE)
    if installed():
        claude('plugin', 'update', PLUGIN)
    else:
        claude('plugin', 'install', PLUGIN)
    version = installed()['version']
    print('Chat Tree ' + version + ' installed for Claude Code from ' + ('this checkout' if local else 'GitHub') + '.')
    print('It works in the Code tab of Claude Desktop. New sessions load it; restart Claude Desktop to update open ones.')


if __name__ == '__main__':
    main()
