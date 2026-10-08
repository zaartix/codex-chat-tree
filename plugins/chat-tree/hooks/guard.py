#!/usr/bin/env python3
"""Trusted local hook: serialize user prompts against branch operations."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store import Store, TreeError, data_path, LANGUAGE_INSTRUCTION


def main():
    if not data_path().exists():
        print('{}')
        return
    event = json.load(sys.stdin)
    # Codex supplies the parent session_id to internal subagent hooks.
    # Their lifecycle must not overwrite the linked main chat's lifecycle.
    if event.get('agent_id'):
        print('{}')
        return
    try:
        store = Store()
    except TreeError as error:
        sys.exit('Chat Tree: ' + str(error))
    try:
        node = store.hook(event.get('session_id'), event.get('hook_event_name'), event.get('turn_id'), event.get('prompt'))
    except Exception:
        if event.get('hook_event_name') == 'UserPromptSubmit':
            raise
        print('{}')
        print('Chat Tree: failed to record stop state', file=sys.stderr)
        return
    if not node or event.get('hook_event_name') != 'UserPromptSubmit':
        print('{}')
        return
    view = store.view(event['session_id'])
    children = [{'title': n['title'], 'summary': n['summary'], 'stale': bool(n['stale']), 'state': n['state']}
                for n in view['nodes'] if n['parent_id'] == node['id'] and n['summary']]
    context = {'node_id': node['id'], 'root_id': node['root_id'], 'title': node['title'],
               'context_chain': view['contexts'], 'direct_child_results': children,
               'saved_decisions': next(n for n in view['nodes'] if n['id']==node['id'])['results'], 'stale': bool(node['stale'])}
    first = ('This is the first message in this branch chat: call tree_panel once so the user sees the branch, then handle the message. '
             if node.get('first_prompt') else '')
    if node.get('reopened'):
        first += ('This item was completed and its chat archived; this message reopened it. Tell the user once that the item '
                  'is open again and will be completed again when they ask. ')
    pending = store.pending_archives(node['root_id'], except_chat=event['session_id'])
    if pending:
        first += ('Apply these Chat Tree chat changes with native tools before anything else, without mentioning them unless one '
                  'fails: archive -> set_thread_archived archived=true, unarchive -> archived=false. ' + json.dumps(pending) + ' ')
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit',
           'additionalContext': 'Chat Tree: current context of the linked branch. Change the tree or complete a branch only at the explicit request of the user. Use the chat-tree skill for operations. ' + first + LANGUAGE_INSTRUCTION + '\n' + json.dumps(context, ensure_ascii=False)}}))


if __name__ == '__main__':
    try:
        main()
    except TreeError as error:
        print(json.dumps({'decision': 'block', 'reason': str(error)}, ensure_ascii=False))
    except Exception as error:
        print('Chat Tree guard: ' + str(error), file=sys.stderr)
        sys.exit(2)
