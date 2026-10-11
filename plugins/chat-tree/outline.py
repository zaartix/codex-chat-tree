"""Markdown checklist of a tree view, for hosts that cannot show the panel."""

STATES = {'working': 'working', 'service': 'working', 'running': 'working'}
OPERATIONS = {'start': 'Starting', 'finish': 'Completing', 'delete': 'Deleting', 'rebuild': 'Rebuilding'}


def _title(node, link):
    number = node.get('number')
    text = ('**' + number + '** ' if number else '') + node['title']
    return '[' + text + '](' + link(node['chat_id']) + ')' if node['chat_id'] else text


def _line(node, link, current_id):
    done = node['state'] in ('done', 'closed')
    marks = []
    if node['state'] == 'closed':
        marks.append('closed')
    if node['stale'] and not done:
        marks.append('context changed')
    state = (node.get('runtime') or {}).get('state')
    if not done and state in STATES:
        marks.append(STATES[state])
    if node['locked_by']:
        marks.append('reserved')
    if node['id'] == current_id:
        marks.append('**this chat**')
    return '[' + ('x' if done else ' ') + '] ' + _title(node, link) + (' · ' + ' · '.join(marks) if marks else '')


def outline(view, link):
    """The checklist the panel would show: the whole tree in its root chat, otherwise the selected item with its
    ancestors as a breadcrumb and its own nested checklist. Chat links open the item chats."""
    if not view['nodes']:
        lines = ['No Chat Tree is linked to this chat.']
        if view['roots']:
            lines += ['', 'Trees in other chats:']
            lines += ['- ' + _title({**r, 'number': None}, link) + ' · ' + str(r['done']) + '/' + str(r['items']) + ' done'
                      for r in view['roots']]
        return '\n'.join(lines)
    nodes = {n['id']: n for n in view['nodes']}
    children = {}
    for n in view['nodes']:
        children.setdefault(n['parent_id'], []).append(n)
    focus = nodes[view['focus_id']]
    root = nodes[focus['root_id']]
    chain = [nodes[c['node_id']] for c in view['contexts']]
    lines = []
    if focus['id'] == root['id']:
        items = children.get(root['id'], [])
        done = sum(1 for n in items if n['state'] in ('done', 'closed') and not n['stale'])
        lines.append('### ' + _title(root, link) + ' · ' + str(done) + '/' + str(len(items)) + ' done')
        top, depth = items, 0
    else:
        lines.append(' › '.join(_title(n, link) for n in chain[:-1]))
        lines.append('')
        lines.append('- ' + _line(focus, link, view['current_node_id']))
        top, depth = children.get(focus['id'], []), 1

    def walk(items, level):
        for n in items:
            lines.append('  ' * level + '- ' + _line(n, link, view['current_node_id']))
            walk(children.get(n['id'], []), level + 1)
    walk(top, depth)
    for op in view['operations']:
        target = nodes.get(op['node_id'])
        if target:
            lines += ['', '_' + OPERATIONS.get(op['kind'], op['kind']) + ' ' + ((target.get('number') or '') + ' ' + target['title']).strip()
                      + ': ' + op['stage'] + (' — ' + op['error'] if op.get('error') else '') + '_']
    return '\n'.join(lines)
