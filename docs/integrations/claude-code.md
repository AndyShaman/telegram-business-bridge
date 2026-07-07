# Claude Code

```bash
claude mcp add tg-business --scope user -- uv run --directory /path/to/tg-business-bridge tg-business-bridge-mcp
```

Демон должен работать (docker/systemd). MCP-сервер читает ту же data/-папку:
задай `BRIDGE_DATA_DIR` в окружении MCP-сервера, если data не в CWD.
Скопируй AGENT_GUIDE.md в свой CLAUDE.md или попроси агента прочитать ресурс guide://agent.
