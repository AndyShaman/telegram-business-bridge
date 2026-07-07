# iva

iva (https://github.com/smixs/iva) поддерживает MCP только по URL, а не через
запуск команды (stdio). Поэтому MCP-сервер нужно поднять отдельным процессом
в режиме streamable-http.

## 1. Запуск MCP-сервера в режиме streamable-http

```bash
BRIDGE_MCP_TRANSPORT=streamable-http uv run --directory /path/to/tg-business-bridge tg-business-bridge-mcp
```

(или пропиши `BRIDGE_MCP_TRANSPORT=streamable-http` в `.env` рядом с демоном).
По умолчанию сервер слушает `http://127.0.0.1:8765/mcp`; хост и порт
переопределяются через `BRIDGE_MCP_HOST` / `BRIDGE_MCP_PORT`.

Демон (bridge) и MCP-сервер — два отдельных процесса, но должны работать на
одной машине и указывать на один и тот же `BRIDGE_DATA_DIR`.

## 2. Подключение в iva

Создай `agent/connections/tg-business.ts` (имя соединения iva берёт из имени
файла):

```ts
import { defineMcpClientConnection } from "eve/connections";

export default defineMcpClientConnection({
  url: "http://localhost:8765/mcp",
  description: "Личные Telegram-сообщения владельца (Business API bridge)",
});
```

Формат соответствует `docs/extending.md` и `agent/connections/README.md` в
репозитории iva; при обновлении iva сверься с ними.

Инструменты появятся как `connection__tg-business__search_messages` и т.д.
Рекомендация: попроси iva после разбора переписки сохранять важные факты в свой
vault (write_card) — мост хранит только сырую историю.
