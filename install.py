#!/usr/bin/env python3
"""Install or update Chat Tree for Codex.

From GitHub:   curl -fsSL https://raw.githubusercontent.com/zaartix/codex-chat-tree/main/install.py | python3 -
From a clone:  python3 install.py              (installs the working copy, for development)
Remove:        python3 install.py --uninstall  (saved trees are kept)

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
CODEX_CANDIDATES = ('/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex',
                    '/Applications/Codex.app/Contents/Resources/codex')


def fail(message):
    raise SystemExit('Chat Tree: ' + message)


def find_codex():
    for path in (os.environ.get('CODEX_CLI_PATH'), shutil.which('codex'), *CODEX_CANDIDATES):
        if path and Path(path).is_file():
            return path
    fail('Codex CLI not found. Install Codex Desktop or the Codex CLI, then run the installer again.')


def check_python():
    # Codex starts the server and hooks with `python3` from PATH, not with this interpreter.
    python = shutil.which('python3')
    if not python:
        fail('python3 not found on PATH. Install Python 3.9 or newer.')
    ok = subprocess.run([python, '-c', 'import sys, sqlite3; print(sys.version_info >= (3, 9))'],
                        capture_output=True, text=True).stdout.strip()
    if ok != 'True':
        fail(python + ' is older than Python 3.9 or lacks sqlite3.')


def codex(*args, check=True):
    result = subprocess.run([CODEX, *args], capture_output=True, text=True)
    if check and result.returncode:
        fail('`codex ' + ' '.join(args) + '` failed:\n' + (result.stderr or result.stdout).strip())
    return result.stdout


def configured_source():
    for market in json.loads(codex('plugin', 'marketplace', 'list', '--json'))['marketplaces']:
        if market['name'] == MARKETPLACE:
            return market.get('marketplaceSource') or {}
    return None


class AppServer:
    def __init__(self):
        self.process = subprocess.Popen([CODEX, 'app-server', '--stdio'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=str(Path.home()))
        self.next_id = 0
        self.call('initialize', {'clientInfo': {'name': 'chat_tree_installer', 'version': '1'}})

    def call(self, method, params):
        self.next_id += 1
        self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': self.next_id, 'method': method, 'params': params}) + '\n')
        self.process.stdin.flush()
        for line in self.process.stdout:
            message = json.loads(line)
            if message.get('id') == self.next_id and 'method' not in message:
                if 'error' in message:
                    fail(method + ' failed: ' + message['error'].get('message', ''))
                return message['result']
        fail('Codex app-server closed during ' + method)

    def close(self):
        self.process.terminate()


def ask(question):
    try:
        with open('/dev/tty') as tty:
            print(question, end=' ', flush=True)
            return tty.readline().strip().lower() in ('y', 'yes')
    except OSError:
        return False


def review_hooks():
    server = AppServer()
    try:
        groups = server.call('hooks/list', {'cwds': [str(Path.home())]})['data']
        hooks = [h for g in groups for h in g['hooks'] if (h.get('pluginId') or '') == PLUGIN]
        pending = [h for h in hooks if h['trustStatus'] != 'trusted' or not h['enabled']]
        if not hooks:
            fail('Codex did not load the plugin hooks.')
        if not pending:
            print('Hooks: already trusted.')
            return
        print('\nChat Tree needs four hooks. They pass branch context to the agent and record when a chat is busy,')
        print('so completion and deletion never touch a running chat. Each runs:')
        print('  ' + pending[0]['command'])
        print('Events: ' + ', '.join(h['eventName'] for h in pending))
        if not ask('Trust these hooks now? [y/N]'):
            print('Hooks not trusted. Run the installer again in a terminal, or review them in Codex.')
            return
        edits = []
        for h in pending:
            key = h['key'].replace('\\', '\\\\').replace('"', '\\"')
            edits.append({'keyPath': 'hooks.state."' + key + '".trusted_hash', 'mergeStrategy': 'replace', 'value': h['currentHash']})
            if not h['enabled']:
                edits.append({'keyPath': 'hooks.state."' + key + '".enabled', 'mergeStrategy': 'replace', 'value': True})
        server.call('config/batchWrite', {'edits': edits, 'reloadUserConfig': True})
        print('Hooks: trusted.')
    finally:
        server.close()


def main():
    global CODEX
    CODEX = find_codex()
    here = Path(__file__).resolve().parent if '__file__' in globals() else None
    local = here if here and (here / '.agents' / 'plugins' / 'marketplace.json').is_file() else None
    current = configured_source()
    if '--uninstall' in sys.argv:
        codex('plugin', 'remove', PLUGIN, '--json', check=False)
        if current is not None:
            codex('plugin', 'marketplace', 'remove', MARKETPLACE)
        print('Chat Tree removed. Saved trees remain in $CODEX_HOME/chat-tree. Restart Codex.')
        return
    check_python()
    wanted = str(local) if local else REPO
    same = current is not None and (current.get('source') == wanted if local else REPO in (current.get('source') or ''))
    if current is not None and not same:
        codex('plugin', 'marketplace', 'remove', MARKETPLACE)
    if not same:
        codex('plugin', 'marketplace', 'add', wanted, '--json')
    elif not local:
        codex('plugin', 'marketplace', 'upgrade', MARKETPLACE)
    installed = json.loads(codex('plugin', 'add', PLUGIN, '--json'))
    version = json.loads((Path(installed['installedPath']) / '.codex-plugin' / 'plugin.json').read_text())['version']
    print('Chat Tree ' + version + ' installed from ' + ('this checkout' if local else 'GitHub') + '.')
    review_hooks()
    print('Restart Codex to load the new version.')


if __name__ == '__main__':
    main()
