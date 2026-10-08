# Working on Chat Tree

- Plugin code lives in `plugins/chat-tree/`: `server.py` (MCP tools), `store.py` (graph and operations), `runtime.py` (Codex app-server), `hooks/guard.py`, `assets/tree.html` (panel), `skills/chat-tree/SKILL.md`.
- Run `python3 -m unittest discover -s tests` after every change. The plugin must keep working on Python 3.9 with the standard library only.
- Design the panel with `python3 preview.py`; it uses isolated data and never touches real chats.
- Try a change in Codex with `python3 install.py` from this checkout, then restart Codex.
- Release with `python3 release.py X.Y.Z`. The version in `plugin.json` is the only version; it also names the panel resource.
- Do not keep compatibility with older data or flows. When a change breaks saved data, bump `SCHEMA_VERSION` in `store.py`; older databases then fail with a clear error instead of being migrated.
- Keep `.mcp.json` and `hooks/hooks.json` free of machine paths. Changing `hooks.json` makes every user trust the hooks again.
- Built-in texts are English; task data keeps its original language.
- `work/` is local scratch space and is not published.
