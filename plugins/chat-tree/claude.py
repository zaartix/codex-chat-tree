"""Claude Desktop (Code tab) as seen from the plugin: read-only access to what Claude itself records.

Chats are Desktop sessions, named by their `local_…` ID. Only the agent can create, archive, rename or delete them
with the native session tools; the server reads the results here and verifies them.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

ID = re.compile(r'local_[0-9a-f-]{36}')


def config_dir():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude')))


def app_dir():
    if os.environ.get('CHAT_TREE_CLAUDE_APP_DIR'):
        return Path(os.environ['CHAT_TREE_CLAUDE_APP_DIR'])
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'Claude'
    if os.name == 'nt':
        return Path(os.environ.get('APPDATA', str(Path.home()))) / 'Claude'
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'Claude'


def link(chat_id):
    # Desktop opens a session from a Markdown link to its ID.
    return '#' + chat_id


class ClaudeDesktop:
    name = 'claude'
    link_to = staticmethod(link)

    def __init__(self, config=None, app=None):
        self.config = Path(config) if config else config_dir()
        self.app = Path(app) if app else app_dir()

    @staticmethod
    def _alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (OSError, ValueError):
            return True
        return True

    def processes(self):
        """Running Claude Code processes as they describe themselves in `sessions/<pid>.json`."""
        found = []
        for path in (self.config / 'sessions').glob('*.json'):
            try:
                info = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(info.get('pid'), int) and self._alive(info['pid']):
                found.append(info)
        return found

    def _process(self, pid):
        try:
            info = json.loads((self.config / 'sessions' / (str(pid) + '.json')).read_text())
        except (OSError, ValueError):
            return None
        return info if info.get('pid') == pid else None

    def current_chat(self):
        """The Desktop session of the Claude process that started this MCP server.

        The process file follows the session after /clear or resume; a headless `claude -p` started inside a session
        has no Desktop session of its own and is never mistaken for it."""
        info = self._process(os.getppid())
        return info.get('hostSessionId') if info else None

    def hook_chat(self, event):
        """The Desktop session a hook event belongs to: the process whose current Claude session is the event's."""
        session = event.get('session_id')
        for info in self.processes():
            if session and info.get('sessionId') == session:
                return info.get('hostSessionId')
        return None

    def live(self, chat_ids):
        """'working' while a session's process is busy, 'idle' when it waits for the user or is not running."""
        status = {info['hostSessionId']: info.get('status') for info in self.processes() if info.get('hostSessionId')}
        return {c: 'idle' if status.get(c, 'idle') == 'idle' else 'working' for c in chat_ids}

    def _record_path(self, chat_id):
        if not ID.fullmatch(chat_id or ''):
            return None
        root = self.app / 'claude-code-sessions'
        account, org = os.environ.get('CLAUDE_CODE_ACCOUNT_UUID'), os.environ.get('CLAUDE_CODE_ORGANIZATION_UUID')
        if account and org and (root / account / org / (chat_id + '.json')).is_file():
            return root / account / org / (chat_id + '.json')
        return next(root.glob('*/*/' + chat_id + '.json'), None)

    def available(self):
        return (self.app / 'claude-code-sessions').is_dir()

    def record(self, chat_id):
        """Desktop's saved session record, or None when the session does not exist."""
        path = self._record_path(chat_id)
        if not path:
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def deleted(self, chat_id):
        """Desktop leaves a `deleted_<uuid>` entry in place of a deleted session's record."""
        return ID.fullmatch(chat_id or '') is not None and \
            next((self.app / 'claude-code-sessions').glob('*/*/deleted_' + chat_id[len('local_'):]), None) is not None

    def inspect(self, chat_ids):
        """Title and archive state of each chat; None for a deleted chat. A chat Desktop has not saved yet is left out."""
        if not self.available():
            return {}
        found = {}
        for chat_id in chat_ids:
            record = self.record(chat_id)
            if record:
                found[chat_id] = {'title': record.get('title') or '', 'archived': bool(record.get('isArchived'))}
            elif self.deleted(chat_id):
                found[chat_id] = None
        return found

    def settings(self, chat_id):
        """The settings an item chat inherits from its parent."""
        record = self.record(chat_id)
        if not record:
            raise RuntimeError('Claude Desktop has no saved session ' + str(chat_id))
        return {'cwd': record.get('cwd'), 'model': record.get('model'), 'effort': record.get('effort'),
                'permission_mode': record.get('permissionMode')}

    def wait_record(self, chat_id, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.record(chat_id):
                return True
            time.sleep(.5)
        return False
