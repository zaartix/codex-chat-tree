# Working on Chat Tree

- One code base serves two hosts. Shared code lives in `plugins/chat-tree/`: `server.py` (MCP tools), `store.py` (graph and operations), `outline.py` (Markdown checklist), `hooks/guard.py` (prompt hook).
- Host adapters: `runtime.py` (Codex app-server) and `claude.py` (Claude Desktop records). `CHAT_TREE_HOST=claude` selects Claude; without it the process serves Codex.
- The Codex plugin is `plugins/chat-tree/` (`.codex-plugin/plugin.json`, `.mcp.json`, `hooks/hooks.json`, `assets/tree.html` panel, `skills/chat-tree/SKILL.md`).
- The Claude plugin is the repository root (`.claude-plugin/`) with its own files in `claude/` (`mcp.json`, `hooks.json`, `skills/chat-tree/SKILL.md`). Claude loads default folders (`hooks/`, `skills/`, `.mcp.json`, …) from the plugin root, so none may exist at the repository root.
- Run `python3 -m unittest discover -s tests` after every change. The plugin must keep working on Python 3.9 with the standard library only.
- Design the Codex panel with `python3 preview.py`; it uses isolated data and never touches real chats.
- Try a change in Codex with `python3 install.py` from this checkout, then restart Codex. In Claude, `python3 install_claude.py` from this checkout runs the plugin from this folder; new sessions load changes.
- Release with `python3 release.py X.Y.Z`. It sets the same version in both manifests; the Codex version also names the panel resource.
- Do not keep compatibility with older data or flows. When a change breaks saved data, bump `SCHEMA_VERSION` in `store.py`; older databases then fail with a clear error instead of being migrated.
- Keep the MCP and hook configurations free of machine paths. Changing the Codex `hooks.json` makes every Codex user trust the hooks again.
- Built-in texts are English; task data keeps its original language.
- `work/` is local scratch space and is not published.
