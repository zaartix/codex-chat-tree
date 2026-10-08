"""Task graph, revisioned changes and durable operation journal."""
import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def identifier():
    return str(uuid.uuid4())


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


LANGUAGE_INSTRUCTION = ('Keep the task working language. Preserve the original language of titles, descriptions, '
                        'contexts, decisions and summaries. Write user-facing replies in the language of the source '
                        'conversation or task context; these English service instructions do not change that language.')


def dispatch_prompt(op):
    return ('The user approved this Chat Tree operation in the panel: '+op['kind']+'. Use the chat-tree skill. '
            'Perform only the approved operation without implementing the task. Start with tree_job; at requested, call tree_step claim. '
            'Do not create another chat at creating. Do not change other branches. '+LANGUAGE_INSTRUCTION+'\n'+
            encode({k:op[k] for k in ('id','token','kind','node_id')}))


class TreeError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details

    def result(self):
        return {'error': {'code': self.code, 'message': str(self), **self.details}}


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def text(value, name, maximum=64000, required=False):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise TreeError('invalid_input', f'Invalid field: {name}')
    return value.strip()


SCHEMA_VERSION = 1


def data_path():
    if os.environ.get('CHAT_TREE_DB'):
        return Path(os.environ['CHAT_TREE_DB']).expanduser()
    # Stable across plugin versions and cache removal.
    return Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'chat-tree' / 'tree.sqlite3'


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else data_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version != SCHEMA_VERSION and (version or db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone()):
                raise TreeError('schema', f'Chat Tree data at {self.path} uses schema {version}; this version needs schema {SCHEMA_VERSION}. '
                                          'Older data is not migrated: move the file away to start with an empty tree.')
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS nodes(
                  id TEXT PRIMARY KEY, root_id TEXT NOT NULL, parent_id TEXT REFERENCES nodes(id),
                  title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', context TEXT NOT NULL DEFAULT '',
                  common_context TEXT NOT NULL DEFAULT '', chat_id TEXT UNIQUE, cwd TEXT NOT NULL DEFAULT '',
                  project_id TEXT, state TEXT NOT NULL DEFAULT 'todo', stale INTEGER NOT NULL DEFAULT 0,
                  position INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
                  summary TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS nodes_root ON nodes(root_id);
                CREATE INDEX IF NOT EXISTS nodes_parent ON nodes(parent_id);
                CREATE TABLE IF NOT EXISTS proposals(
                  id TEXT PRIMARY KEY, root_id TEXT, revision INTEGER, payload TEXT NOT NULL,
                  state TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL, owner_chat TEXT);
                CREATE TABLE IF NOT EXISTS operations(
                  id TEXT PRIMARY KEY, root_id TEXT NOT NULL, node_id TEXT NOT NULL, kind TEXT NOT NULL,
                  stage TEXT NOT NULL, payload TEXT NOT NULL, token TEXT NOT NULL, error TEXT,
                  created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS summaries(
                  operation_id TEXT NOT NULL, node_id TEXT NOT NULL, summary TEXT NOT NULL,
                  chat_id TEXT, turn_id TEXT, created REAL NOT NULL,
                  PRIMARY KEY(operation_id,node_id));
                CREATE TABLE IF NOT EXISTS runtime(
                  chat_id TEXT PRIMARY KEY, state TEXT NOT NULL, turn_id TEXT, prompt TEXT,
                  hook_seen INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS locks(node_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS results(operation_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL REFERENCES nodes(id),
                  source_title TEXT NOT NULL, summary TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS audit(
                  id INTEGER PRIMARY KEY, action TEXT NOT NULL, evidence TEXT NOT NULL,
                  payload TEXT NOT NULL, created REAL NOT NULL);
            ''')
            db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, factory=Connection)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def node(self, db, node_id):
        row = db.execute('SELECT * FROM nodes WHERE id=?', (node_id,)).fetchone()
        if row is None:
            raise TreeError('not_found', 'Branch not found')
        return dict(row)

    def subtree(self, db, node_id):
        self.node(db, node_id)
        rows = db.execute('''WITH RECURSIVE branch(id,depth) AS (
            SELECT id,0 FROM nodes WHERE id=? UNION ALL
            SELECT n.id,b.depth+1 FROM nodes n JOIN branch b ON n.parent_id=b.id)
            SELECT n.*,b.depth FROM nodes n JOIN branch b ON n.id=b.id
            ORDER BY b.depth DESC,n.position,n.created''', (node_id,)).fetchall()
        return [dict(r) for r in rows]

    def context(self, db, node_id):
        chain, cursor = [], self.node(db, node_id)
        while cursor:
            chain.append(cursor)
            cursor = self.node(db, cursor['parent_id']) if cursor['parent_id'] else None
        chain.reverse()
        return [{'node_id': n['id'], 'title': n['title'], 'description':n['description'], 'context': n['context'],
                 'chat_id':n['chat_id'], 'chat_url':'codex://threads/'+n['chat_id'] if n['chat_id'] else None,
                 'common_context': n['common_context'], 'revision': n['revision']} for n in chain]

    def view(self, chat_id=None, focus_id=None, root_id=None):
        with self.connect() as db:
            roots = [dict(r) for r in db.execute('''SELECT r.*,
                (SELECT COUNT(*) FROM nodes c WHERE c.parent_id=r.id) AS items,
                (SELECT COUNT(*) FROM nodes c WHERE c.parent_id=r.id AND c.state='done' AND c.stale=0) AS done
                FROM nodes r WHERE r.parent_id IS NULL ORDER BY r.updated DESC''')]
            current = db.execute('SELECT id,root_id FROM nodes WHERE chat_id=?', (chat_id,)).fetchone() if chat_id else None
            # An explicit tree choice wins over the chat's own tree, so linked chats can browse other trees.
            if focus_id:
                selected = self.node(db, focus_id)
            elif root_id:
                selected = self.node(db, root_id)
            elif current:
                selected = self.node(db, current['id'])
            else:
                proposals = [dict(r) for r in db.execute("SELECT * FROM proposals WHERE root_id IS NULL AND owner_chat=? AND state='pending' ORDER BY created",(chat_id,))]
                for p in proposals:
                    p['payload'] = json.loads(p['payload'])
                return {'roots': roots, 'nodes': [], 'current_node_id': None, 'current_root_id': None, 'focus_id': None,
                        'proposals': proposals, 'operations': [], 'revision': None}
            root = self.node(db, selected['root_id'])
            nodes = [dict(r) for r in db.execute('SELECT * FROM nodes WHERE root_id=? ORDER BY position,created', (root['id'],))]
            runtime = {r['chat_id']: dict(r) for r in db.execute('SELECT chat_id,state,hook_seen,updated FROM runtime')}
            for n in nodes:
                n['results'] = [dict(r) for r in db.execute('SELECT * FROM results WHERE parent_id=? ORDER BY created', (n['id'],))]
                n['runtime'] = runtime.get(n['chat_id'], {'state': 'unknown', 'hook_seen': 0}) if n['chat_id'] else None
                lock = db.execute('SELECT operation_id FROM locks WHERE node_id=?', (n['id'],)).fetchone()
                n['locked_by'] = lock['operation_id'] if lock else None
            proposals = [dict(r) for r in db.execute("SELECT * FROM proposals WHERE root_id=? AND state='pending' ORDER BY created", (root['id'],))]
            for p in proposals:
                p['payload'] = json.loads(p['payload'])
            ops = [self.operation(db, r['id']) for r in db.execute("SELECT id FROM operations WHERE root_id=? AND stage NOT IN ('done','cancelled') ORDER BY created", (root['id'],))]
            return {'roots': roots, 'root': root, 'nodes': nodes,
                    'current_node_id': current['id'] if current else None,
                    'current_root_id': current['root_id'] if current else None, 'focus_id': selected['id'],
                    'revision': root['revision'], 'contexts': self.context(db, selected['id']),
                    'proposals': proposals, 'operations': ops}

    def operation(self, db, operation_id):
        row = db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
        if not row:
            raise TreeError('not_found', 'Operation not found')
        result = dict(row)
        result['payload'] = json.loads(result['payload'])
        result['dispatch_prompt'] = dispatch_prompt(result)
        result['summaries'] = {r['node_id']: dict(r) for r in db.execute('SELECT * FROM summaries WHERE operation_id=?', (operation_id,))}
        return result

    def _touch(self, db, node):
        db.execute('UPDATE nodes SET revision=revision+1,updated=? WHERE id IN (?,?)', (time.time(), node['id'], node['root_id']))

    def _unlocked(self, db, ids):
        for node_id in ids:
            row = db.execute('SELECT operation_id FROM locks WHERE node_id=?', (node_id,)).fetchone()
            if row:
                raise TreeError('locked', 'Another operation has reserved this branch', operation_id=row['operation_id'])

    def _idle(self, db, nodes, actor_chat=None):
        blocked = []
        for n in nodes:
            if not n['chat_id'] or n['chat_id'] == actor_chat:
                continue
            state = db.execute('SELECT * FROM runtime WHERE chat_id=?', (n['chat_id'],)).fetchone()
            if not state or not state['hook_seen'] or state['state'] != 'idle':
                blocked.append({'title': n['title'], 'chat_id': n['chat_id'],
                                'state': state['state'] if state else 'unknown',
                                'url': 'codex://threads/' + n['chat_id']})
        if blocked:
            raise TreeError('busy', 'Operation blocked: an agent is working or the chat state has not been verified by the plugin hooks', chats=blocked)

    def propose(self, change, actor_chat=None):
        if change.get('action')=='create' and not actor_chat:
            raise TreeError('no_chat','A proposal must belong to a specific chat')
        with self.transaction() as db:
            node_id = change.get('node_id') or change.get('parent_id')
            node = self.node(db, node_id) if node_id else None
            root = self.node(db, node['root_id']) if node else None
            proposal_id = identifier()
            db.execute('INSERT INTO proposals(id,root_id,revision,payload,created,owner_chat) VALUES(?,?,?,?,?,?)',
                       (proposal_id, root['id'] if root else None, root['revision'] if root else None, encode(change), time.time(),actor_chat))
            return {'proposal_id': proposal_id, 'state': 'pending', 'change': change}

    def dismiss(self, proposal_id):
        with self.transaction() as db:
            row=db.execute("SELECT state FROM proposals WHERE id=?",(proposal_id,)).fetchone()
            if not row or row['state']!='pending':
                raise TreeError('invalid_proposal','The proposal has already been handled')
            db.execute("UPDATE proposals SET state='dismissed' WHERE id=?",(proposal_id,))
            return {'dismissed':True}

    def _insert(self, db, parent, item):
        if not isinstance(item, dict) or set(item) - {'title','description','context'}:
            raise TreeError('invalid_input', 'An item may contain only title, description and individual context')
        node_id, now = identifier(), time.time()
        title = text(item.get('title'), 'title', 240, True)
        position = db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM nodes WHERE parent_id=?', (parent['id'],)).fetchone()[0]
        db.execute('''INSERT INTO nodes(id,root_id,parent_id,title,description,context,cwd,project_id,position,created,updated)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                   (node_id, parent['root_id'], parent['id'], title, text(item.get('description', ''), 'description'),
                    text(item.get('context', ''), 'context'), parent['cwd'], parent['project_id'], position, now, now))
        return node_id

    def apply(self, change, evidence, actor_chat=None, expected_revision=None, proposal_id=None):
        if not evidence:
            raise TreeError('invalid_input', 'The change source is missing from the audit context')
        with self.transaction() as db:
            if proposal_id:
                p = db.execute('SELECT * FROM proposals WHERE id=?', (proposal_id,)).fetchone()
                if not p or p['state'] != 'pending':
                    raise TreeError('invalid_proposal', 'The proposal has already been handled')
                change = json.loads(p['payload'])
                if change.get('action')=='create' and p['owner_chat']!=actor_chat:
                    raise TreeError('denied','Create this tree in the chat where it was proposed')
                expected_revision = p['revision']
            action = change.get('action')
            if action not in ('create','add','edit','move','start','finish','reopen','delete','rebuild'):
                raise TreeError('invalid_action','Unknown action')
            node_id = change.get('node_id') or change.get('parent_id')
            node = self.node(db, node_id) if node_id else None
            if action!='create' and not node:
                raise TreeError('invalid_input','Select a branch')
            if node:
                root = self.node(db, node['root_id'])
                if expected_revision is not None and root['revision'] != expected_revision:
                    raise TreeError('conflict', 'The tree has changed. Refresh the proposal', revision=root['revision'])
                if change.get('action') not in ('start','finish','delete','rebuild'):
                    self._unlocked(db, [node['id']])
            now = time.time()
            idle_actor = None if evidence.get('source')=='user_interface' else actor_chat
            result = {}
            if action == 'create':
                if not actor_chat:
                    raise TreeError('no_chat', 'Open the panel in the main task chat')
                if db.execute('SELECT 1 FROM nodes WHERE chat_id=?', (actor_chat,)).fetchone():
                    raise TreeError('already_linked', 'This chat already has a tree')
                root_id = identifier()
                cwd = text(change.get('cwd', ''), 'cwd', 4096)
                if cwd and not Path(cwd).is_absolute():
                    raise TreeError('invalid_input', 'The working directory must be an absolute path')
                items = change.get('items', [])
                if not isinstance(items, list) or len(items) > 100:
                    raise TreeError('invalid_input', 'A checklist may contain up to 100 items')
                db.execute('''INSERT INTO nodes(id,root_id,title,description,context,common_context,chat_id,cwd,project_id,created,updated)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                           (root_id, root_id, text(change.get('title'), 'title', 240, True), text(change.get('description', ''), 'description'),
                            text(change.get('context', ''), 'context'), text(change.get('common_context', ''), 'common_context'), actor_chat,
                            cwd, change.get('project_id'), now, now))
                node = self.node(db, root_id)
                result = {'node_id': root_id, 'item_ids': [self._insert(db, node, i) for i in items]}
            elif action == 'add':
                items = change.get('items')
                if not isinstance(items, list) or not items or len(items) > 100:
                    raise TreeError('invalid_input', 'Add between 1 and 100 items')
                result = {'node_id': node['id'], 'item_ids': [self._insert(db, node, i) for i in items]}
                self._touch(db, node)
            elif action == 'edit':
                allowed = ('title', 'description', 'context', 'common_context')
                values = {k: text(change[k], k, 240 if k == 'title' else 64000, k == 'title') for k in allowed if k in change}
                if not values:
                    raise TreeError('invalid_input', 'No changes provided')
                branch = self.subtree(db, node['id'])
                self._unlocked(db, [n['id'] for n in branch])
                context_changed = any(k in values and values[k] != node[k] for k in ('context', 'common_context','description'))
                if context_changed:
                    self._idle(db, branch, idle_actor)
                    own_context_changed = any(k in values and values[k] != node[k] for k in ('context','description'))
                    for child in branch:
                        if child['depth'] >= (1 if own_context_changed else 2):
                            db.execute('UPDATE nodes SET stale=1,updated=? WHERE id=?', (now, child['id']))
                db.execute('UPDATE nodes SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=?', (*values.values(), node['id']))
                self._touch(db, node)
                result = {'node_id': node['id'], 'context_changed': context_changed}
            elif action == 'move':
                parent = self.node(db, change.get('destination_id'))
                branch = self.subtree(db, node['id'])
                if not node['parent_id'] or parent['root_id'] != node['root_id'] or parent['id'] in {n['id'] for n in branch}:
                    raise TreeError('invalid_move', 'The branch cannot be moved to this location')
                # Existing chats were forked from the old parent; moving them would break strict inheritance.
                linked = [{'title': n['title'], 'chat_id': n['chat_id'], 'url': 'codex://threads/'+n['chat_id']} for n in branch if n['chat_id']]
                if linked:
                    raise TreeError('started_branch', 'Only branches without chats can be moved; started chats keep their parent settings. Rebuild the branch instead', chats=linked)
                self._unlocked(db, [n['id'] for n in branch] + [parent['id']])
                position = db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM nodes WHERE parent_id=?', (parent['id'],)).fetchone()[0]
                db.execute('UPDATE nodes SET parent_id=?,position=?,stale=1 WHERE id=?', (parent['id'], position, node['id']))
                for child in branch:
                    db.execute('UPDATE nodes SET stale=1,cwd=?,project_id=?,updated=? WHERE id=?', (parent['cwd'], parent['project_id'], now, child['id']))
                self._touch(db, node)
                result = {'node_id': node['id']}
            elif action == 'reopen':
                db.execute("UPDATE nodes SET state='todo' WHERE id=?",(node['id'],))
                self._touch(db,node)
                result={'node_id':node['id']}
            elif action in ('start', 'finish', 'delete', 'rebuild'):
                if not node:
                    raise TreeError('invalid_input', 'Select a branch')
                if action in ('delete','rebuild') and not node['parent_id']:
                    raise TreeError('root_operation', 'Select an item from the root checklist')
                if action == 'finish' and not node['chat_id']:
                    raise TreeError('not_started', 'Open this branch chat first')
                if action == 'start' and node['chat_id']:
                    if proposal_id:
                        db.execute("UPDATE proposals SET state='approved' WHERE id=?", (proposal_id,))
                    return {'node_id': node['id'], 'chat_id': node['chat_id'], 'url': 'codex://threads/'+node['chat_id']}
                existing = db.execute("SELECT id FROM operations WHERE node_id=? AND kind=? AND stage NOT IN ('done','cancelled')", (node['id'], action)).fetchone()
                if existing:
                    if proposal_id:
                        db.execute("UPDATE proposals SET state='approved' WHERE id=?", (proposal_id,))
                    return {'operation': self.operation(db, existing['id']), 'resumed': True}
                if action!='start':
                    for row in db.execute("SELECT id,payload,kind FROM operations WHERE stage NOT IN ('done','cancelled') AND kind!='start'"):
                        if json.loads(row['payload'])['actor_chat']==actor_chat:
                            raise TreeError('coordinator_busy','Another tree operation is already running in this chat',operation_id=row['id'])
                if action in ('delete', 'rebuild') and any(n['chat_id'] == actor_chat for n in self.subtree(db, node['id'])):
                    raise TreeError('current_chat', 'Start deletion or rebuilding from the parent chat')
                branch = self.subtree(db, node['id'])
                self._unlocked(db, [n['id'] for n in branch])
                if action != 'start':
                    self._idle(db, branch, idle_actor)
                    if evidence.get('source')=='user_interface':
                        initiator=db.execute('SELECT * FROM nodes WHERE chat_id=?',(actor_chat,)).fetchone()
                        if initiator:
                            self._idle(db,[dict(initiator)])
                parent = self.node(db,node['parent_id']) if node['parent_id'] else None
                if action == 'start' and (not parent or not parent['chat_id']):
                    raise TreeError('parent_not_started','Open the immediate parent chat first to inherit its settings',node_id=parent['id'] if parent else node['id'])
                if action in ('finish','rebuild') and parent and not parent['chat_id']:
                    raise TreeError('parent_not_started','Create the immediate parent chat first to deliver the summary',node_id=parent['id'])
                delivery_parent = parent if action in ('finish','rebuild') and parent and parent['chat_id'] != actor_chat else None
                if delivery_parent:
                    self._unlocked(db,[delivery_parent['id']])
                    self._idle(db,[delivery_parent],actor_chat)
                operation_id, token = identifier(), secrets.token_urlsafe(32)
                payload = {'nodes': branch, 'context': self.context(db, node['id']), 'completed': [],
                           'actor_chat': actor_chat, 'replacement_ids': [], 'delivered': False}
                actor_node = db.execute('SELECT id FROM nodes WHERE chat_id=?',(actor_chat,)).fetchone()
                if actor_node:
                    prompt = dispatch_prompt({'id':operation_id,'token':token,'kind':action,'node_id':node['id']})
                    payload['service_permits'] = {hashlib.sha256(prompt.encode()).hexdigest(): {'node_id':actor_node['id'],'purpose':'coordinate'}}
                if action == 'rebuild':
                    parent = self.node(db, node['parent_id']) if node['parent_id'] else node
                    payload['parent_id'] = parent['id']
                db.execute('INSERT INTO operations(id,root_id,node_id,kind,stage,payload,token,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                           (operation_id, node['root_id'], node['id'], action, 'requested', encode(payload), token, now, now))
                for n in branch:
                    db.execute('INSERT INTO locks(node_id,operation_id) VALUES(?,?)', (n['id'], operation_id))
                if delivery_parent:
                    db.execute('INSERT INTO locks VALUES(?,?)',(delivery_parent['id'],operation_id))
                result = {'operation': self.operation(db, operation_id)}
            else:
                raise TreeError('invalid_action', 'Unknown action')
            if proposal_id:
                db.execute("UPDATE proposals SET state='approved' WHERE id=?", (proposal_id,))
            db.execute('INSERT INTO audit(action,evidence,payload,created) VALUES(?,?,?,?)', (action, encode(evidence), encode(change), now))
            return result

    def reserve_creation(self, operation_id, token, node_id, actor_chat):
        with self.transaction() as db:
            op = self.operation(db, operation_id)
            allowed = [op['node_id']] if op['kind'] == 'start' else op['payload']['replacement_ids'] if op['kind'] == 'rebuild' else []
            if not secrets.compare_digest(op['token'], token) or actor_chat != op['payload']['actor_chat'] or node_id not in allowed or op['stage'] not in ('creating','preparing'):
                raise TreeError('denied', 'Chat creation is not allowed by this operation')
            node = self.node(db, node_id)
            creations = op['payload'].setdefault('creations', {})
            if not node['parent_id'] or not self.node(db, node['parent_id'])['chat_id']:
                raise TreeError('parent_not_started', 'Open the immediate parent chat first to inherit its settings')
            if node_id in creations or node['chat_id']:
                raise TreeError('already_creating', 'Creation has already started. Inspect the saved UUID before continuing; do not create a duplicate chat', chat_id=node['chat_id'])
            creations[node_id] = {'started': time.time()}
            db.execute('UPDATE operations SET payload=? WHERE id=?', (encode(op['payload']), operation_id))
            return node

    def created(self, operation_id, token, node_id, chat_id):
        with self.transaction() as db:
            op = self.operation(db, operation_id)
            if not secrets.compare_digest(op['token'], token) or node_id not in op['payload'].get('creations', {}):
                raise TreeError('denied', 'Chat creation has not been reserved')
            node = self.node(db, node_id)
            if node['chat_id'] and node['chat_id'] != chat_id:
                raise TreeError('conflict', 'The UUID has already been saved')
            op['payload']['creations'][node_id]['chat_id'] = chat_id
            db.execute('UPDATE nodes SET chat_id=? WHERE id=?', (chat_id, node_id))
            db.execute('UPDATE operations SET payload=? WHERE id=?', (encode(op['payload']), operation_id))
            db.execute('INSERT OR IGNORE INTO locks VALUES(?,?)', (node_id, operation_id))

    def job(self, operation_id, token):
        with self.connect() as db:
            op = self.operation(db, operation_id)
            if not secrets.compare_digest(op['token'], token):
                raise TreeError('denied', 'Operation access denied')
            return op

    @staticmethod
    def service_permit(op, node_id, prompt, purpose=None):
        permit=op['payload'].get('service_permits',{}).get(hashlib.sha256(prompt.encode()).hexdigest())
        return bool(permit and permit['node_id']==node_id and (purpose is None or permit['purpose']==purpose))

    def check_service(self, operation_id, token, node_id, prompt, purpose):
        op=self.job(operation_id,token)
        if op['stage'] in ('done','cancelled') or not self.service_permit(op,node_id,prompt,purpose):
            raise TreeError('invalid_service','The request does not match the exact service prompt for this operation')

    def service_prompt(self, operation_id, token, node_id, purpose='summary'):
        with self.transaction() as db:
            op = self.operation(db, operation_id)
            if not secrets.compare_digest(op['token'], token):
                raise TreeError('denied', 'Operation access denied')
            if op['stage'] in ('done','cancelled'):
                raise TreeError('closed','The operation is already closed')
            node = self.node(db, node_id)
            saved = op['payload'].get('service_messages',{}).get(purpose+':'+node_id)
            if saved:
                return {'prompt':saved,'node_id':node_id,'chat_id':node['chat_id'],'cwd':node['cwd'],'project_id':node['project_id']}
            if purpose == 'summary':
                if op['kind'] not in ('finish', 'rebuild') or node_id not in {n['id'] for n in op['payload']['nodes']}:
                    raise TreeError('invalid_step', 'This summary is not part of the operation')
                children = [n for n in op['payload']['nodes'] if n['parent_id'] == node_id]
                if any(n['chat_id'] and n['id'] not in op['summaries'] for n in children):
                    raise TreeError('order', 'Collect summaries from the immediate children first')
                instruction = 'Collect a fresh summary for an explicitly requested branch completion or rebuild. Read your context and the immediate child summaries below. Write one paragraph covering completed work, validation, decisions and unresolved issues. Do not continue implementation or change the tree. Call tree_summary with the operation data and this paragraph. Return the same paragraph.'
                payload = {'children': [{'title': n['title'], 'summary': op['summaries'].get(n['id'], {}).get('summary', 'Work on this item has not started.')} for n in children]}
            elif purpose == 'delivery':
                selected = self.node(db, op['node_id'])
                if selected['parent_id'] != node_id or op['node_id'] not in op['summaries']:
                    raise TreeError('invalid_step', 'Deliver the summary only to the immediate parent')
                instruction = 'Receive the result of a completed or rebuilt immediate child branch. Keep this result in your context. Do not continue implementation, change the tree or complete your own branch. Reply with one line acknowledging the result.'
                payload = {'child': selected['title'], 'summary': op['summaries'][selected['id']]['summary'], 'parent_result': op['payload'].get('parent_result', '')}
            else:
                raise TreeError('invalid_step', 'Unknown service request')
            marker = f'CHAT_TREE_SERVICE {operation_id} {token}'
            prompt = marker + '\n' + instruction + ' ' + LANGUAGE_INSTRUCTION + '\n' + encode({'operation_id': operation_id, 'token': token, 'node_id': node_id, **payload})
            op['payload'].setdefault('service_messages',{})[purpose+':'+node_id]=prompt
            permits = op['payload'].setdefault('service_permits', {})
            permits[hashlib.sha256(prompt.encode()).hexdigest()] = {'node_id': node_id, 'purpose': purpose}
            db.execute('UPDATE operations SET payload=?,updated=? WHERE id=?', (encode(op['payload']), time.time(), operation_id))
            return {'prompt': prompt, 'node_id': node_id, 'chat_id': node['chat_id'], 'cwd': node['cwd'], 'project_id': node['project_id']}

    def bind(self, operation_id, token, chat_id, node_id=None):
        with self.transaction() as db:
            op = self.operation(db, operation_id)
            if not secrets.compare_digest(op['token'], token):
                raise TreeError('denied', 'Operation access denied')
            target = node_id or op['node_id']
            if op['kind'] not in ('start', 'rebuild') or target not in ([op['node_id']] if op['kind'] == 'start' else op['payload']['replacement_ids']):
                raise TreeError('invalid_binding', 'The chat does not belong to the branch being created')
            n = self.node(db, target)
            if n['chat_id'] and n['chat_id'] != chat_id:
                raise TreeError('conflict', 'This item is already linked to another chat')
            db.execute('UPDATE nodes SET chat_id=?,updated=? WHERE id=?', (chat_id, time.time(), target))
            if op['kind'] == 'start':
                db.execute("UPDATE operations SET stage='done',updated=? WHERE id=?", (time.time(), operation_id))
                db.execute('DELETE FROM locks WHERE operation_id=?', (operation_id,))
            self._touch(db, n)
            return {'node_id': target, 'chat_id': chat_id}

    def record_summary(self, operation_id, token, node_id, summary, chat_id, turn_id):
        summary = text(summary, 'summary', 12000, True)
        with self.transaction() as db:
            op = self.operation(db, operation_id)
            if not secrets.compare_digest(op['token'], token) or op['kind'] not in ('finish', 'rebuild'):
                raise TreeError('denied', 'Summary collection access denied')
            if op['stage'] in ('done','cancelled'):
                raise TreeError('closed', 'Summary collection is already closed')
            node = self.node(db, node_id)
            if node_id not in {n['id'] for n in op['payload']['nodes']} or node['chat_id'] != chat_id:
                raise TreeError('invalid_summary', 'The summary does not belong to the selected branch')
            previous = op['summaries'].get(node_id)
            if previous:
                if previous['summary'] != summary or previous['turn_id'] != turn_id:
                    raise TreeError('conflict', 'A fresh summary has already been saved for this collection')
                return {'recorded': True, 'node_id': node_id}
            children = [n for n in op['payload']['nodes'] if n['parent_id'] == node_id and n['chat_id']]
            if any(n['id'] not in op['summaries'] for n in children):
                raise TreeError('order', 'Collect summaries from the immediate children first')
            db.execute('INSERT OR REPLACE INTO summaries VALUES(?,?,?,?,?,?)', (operation_id, node_id, summary, chat_id, turn_id, time.time()))
            db.execute("UPDATE operations SET stage='collecting',updated=? WHERE id=?", (time.time(), operation_id))
            return {'recorded': True, 'node_id': node_id}

    def advance(self, operation_id, token, step, data=None, actor_chat=None):
        data = data or {}
        with self.transaction() as db:
            op = self.operation(db, operation_id)
            if not secrets.compare_digest(op['token'], token):
                raise TreeError('denied', 'Operation access denied')
            if actor_chat and actor_chat != op['payload']['actor_chat']:
                raise TreeError('denied', 'Only the initiating chat may perform this operation')
            payload, stage = op['payload'], op['stage']
            if stage in ('done', 'cancelled'):
                return op
            if step == 'claim':
                if stage != 'requested':
                    raise TreeError('already_claimed', 'The operation has already started; inspect its state')
                stage = 'creating' if op['kind'] == 'start' else 'collecting' if op['kind'] in ('finish', 'rebuild') else 'deleting'
            elif step == 'prepare_replacement':
                if op['kind'] != 'rebuild' or op['node_id'] not in op['summaries']:
                    raise TreeError('incomplete', 'Collect the complete branch summary first')
                if payload['replacement_ids']:
                    raise TreeError('already_prepared', 'The replacement has already been prepared')
                parent = self.node(db, payload['parent_id'])
                items = data.get('items', [])
                if not isinstance(items, list) or not items or len(items) > 100:
                    raise TreeError('invalid_input', 'Provide replacement items at a single level')
                payload['replacement_ids'] = [self._insert(db, parent, i) for i in items]
                for new_id in payload['replacement_ids']:
                    db.execute('INSERT INTO locks VALUES(?,?)', (new_id, operation_id))
                payload['parent_result'] = text(data.get('parent_result', ''), 'parent_result')
                stage = 'preparing'
            elif step == 'ready_to_delete':
                if op['kind'] not in ('delete', 'rebuild'):
                    raise TreeError('invalid_step', 'Deletion is not part of this operation')
                if op['kind'] == 'rebuild' and (not payload['replacement_ids'] or any(not self.node(db, i)['chat_id'] for i in payload['replacement_ids'])):
                    raise TreeError('incomplete', 'Create and bind all replacement chats before deleting the old chats')
                self._idle(db, payload['nodes'], actor_chat)
                stage = 'deleting'
            elif step == 'deleted':
                node_id = data.get('node_id')
                if stage != 'deleting' or node_id not in {n['id'] for n in payload['nodes']}:
                    raise TreeError('invalid_step', 'Deletion has not been scheduled')
                if node_id not in payload['completed']:
                    payload['completed'].append(node_id)
            elif step == 'delivered':
                if op['kind'] not in ('finish', 'rebuild') or op['node_id'] not in op['summaries']:
                    raise TreeError('incomplete', 'The branch summary paragraph is missing')
                payload['delivered'] = True
                payload['delivery_turn_id'] = data.get('turn_id')
            elif step == 'commit':
                node = self.node(db, op['node_id'])
                if op['kind'] == 'finish':
                    if op['node_id'] not in op['summaries'] or (node['parent_id'] and not payload['delivered']):
                        raise TreeError('incomplete', 'Save the summary and deliver it to the parent first')
                    db.execute("UPDATE nodes SET state='done',summary=?,stale=0 WHERE id=?", (op['summaries'][node['id']]['summary'], node['id']))
                elif op['kind'] in ('delete', 'rebuild'):
                    required = {n['id'] for n in payload['nodes'] if n['chat_id']}
                    if not required.issubset(payload['completed']):
                        raise TreeError('incomplete', 'Some original chats remain; the operation has been saved for resuming')
                    if op['kind'] == 'rebuild' and not payload['delivered']:
                        raise TreeError('incomplete', 'Deliver the saved result to the parent')
                    if op['kind'] == 'rebuild' and payload.get('parent_result'):
                        db.execute('INSERT INTO results VALUES(?,?,?,?,?)', (op['id'],payload['parent_id'],node['title'],payload['parent_result'],time.time()))
                    for old in payload['nodes']:
                        db.execute('DELETE FROM results WHERE parent_id=?', (old['id'],))
                        db.execute('DELETE FROM nodes WHERE id=?', (old['id'],))
                else:
                    raise TreeError('invalid_step', 'Creation completes after the chat is bound')
                if db.execute('SELECT 1 FROM nodes WHERE id=?', (node['root_id'],)).fetchone():
                    db.execute('UPDATE nodes SET revision=revision+1,updated=? WHERE id=?', (time.time(), node['root_id']))
                db.execute('DELETE FROM locks WHERE operation_id=?', (operation_id,))
                stage = 'done'
            elif step == 'cancel':
                if payload['completed'] or payload['replacement_ids'] or payload.get('creations'):
                    raise TreeError('partial', 'Resume an operation that has prepared replacements or deleted chats')
                db.execute('DELETE FROM locks WHERE operation_id=?', (operation_id,))
                stage = 'cancelled'
            elif step == 'error':
                db.execute('UPDATE operations SET error=? WHERE id=?', (text(data.get('message', ''), 'message', 4000), operation_id))
            else:
                raise TreeError('invalid_step', 'Unknown operation step')
            db.execute('UPDATE operations SET stage=?,payload=?,updated=? WHERE id=?', (stage, encode(payload), time.time(), operation_id))
            return self.operation(db, operation_id)

    def observe_turn(self, chat_id, turn_id):
        """Host MCP metadata also covers turns admitted as function-call outputs.

        Codex skips UserPromptSubmit for these inputs. This records activity,
        without claiming that a trusted Stop hook has been observed.
        """
        if not chat_id or not turn_id:
            return
        with self.transaction() as db:
            if not db.execute('SELECT 1 FROM nodes WHERE chat_id=?',(chat_id,)).fetchone():
                return
            row=db.execute('SELECT turn_id,state FROM runtime WHERE chat_id=?',(chat_id,)).fetchone()
            state='service' if row and row['turn_id']==turn_id and row['state']=='service' else 'running'
            db.execute('''INSERT INTO runtime VALUES(?,?,?,NULL,0,?) ON CONFLICT(chat_id) DO UPDATE SET
                          state=excluded.state,turn_id=excluded.turn_id,updated=excluded.updated''',
                       (chat_id,state,turn_id,time.time()))

    def fresh_chat(self, chat_id):
        """A chat created without a turn is idle; trusted hooks track it from its first prompt."""
        with self.transaction() as db:
            db.execute("INSERT INTO runtime VALUES(?,'idle',NULL,NULL,1,?) ON CONFLICT(chat_id) DO NOTHING", (chat_id, time.time()))

    def hook(self, chat_id, event, turn_id=None, prompt=None):
        with self.transaction() as db:
            node = db.execute('SELECT * FROM nodes WHERE chat_id=?', (chat_id,)).fetchone()
            if not node:
                return None
            first_prompt = False
            if event == 'UserPromptSubmit':
                prior = db.execute('SELECT turn_id FROM runtime WHERE chat_id=?', (chat_id,)).fetchone()
                lock = db.execute('SELECT operation_id FROM locks WHERE node_id=?', (node['id'],)).fetchone()
                service = False
                if lock and prompt:
                    operation = self.operation(db, lock['operation_id'])
                    service = self.service_permit(operation,node['id'],prompt)
                if lock and not service:
                    raise TreeError('locked', 'An approved operation has reserved this branch. Finish or cancel it in the panel.', operation_id=lock['operation_id'])
                state = 'service' if service else 'running'
                first_prompt = not service and (prior is None or prior['turn_id'] is None)
                db.execute('''INSERT INTO runtime VALUES(?,?,?,?,1,?) ON CONFLICT(chat_id) DO UPDATE SET
                              state=excluded.state,turn_id=excluded.turn_id,prompt=excluded.prompt,hook_seen=1,updated=excluded.updated''',
                           (chat_id, state, turn_id, prompt, time.time()))
            elif event in ('Stop', 'Interrupt', 'SessionEnd'):
                # Never overwrite a newer active turn with a delayed old Stop.
                row = db.execute('SELECT turn_id,state FROM runtime WHERE chat_id=?', (chat_id,)).fetchone()
                if row and turn_id and row['turn_id'] != turn_id:
                    return None
                db.execute('''INSERT INTO runtime VALUES(?,?,?,NULL,1,?) ON CONFLICT(chat_id) DO UPDATE SET
                              state=excluded.state,prompt=NULL,hook_seen=1,updated=excluded.updated''',
                           (chat_id, 'idle' if event == 'Stop' or (event=='SessionEnd' and row and row['state']=='idle') else 'unknown', turn_id, time.time()))
            return {**dict(node), 'first_prompt': first_prompt}
