# Проверка контракта ВкусВилла

Дата публичных запросов — 18 ноября 2026 года.

| Уверенность | Что проверено |
| --- | --- |
| `confirmed-by-live-response` | Обе публичные точки RFC 9728 вернули HTTP 200 и JSON. `resource` равен `https://mcp.vkusvill.ru/mcp`. В `authorization_servers` указан `https://oauth.vkusvill.ru/`. В `scopes_supported` ресурса есть `purchase_history.read`. [Metadata ресурса по пути](https://mcp.vkusvill.ru/.well-known/oauth-protected-resource/mcp), [metadata в корне](https://mcp.vkusvill.ru/.well-known/oauth-protected-resource). |
| `confirmed-by-live-response` | RFC 8414 вернул HTTP 200. Issuer совпадает с RFC 9728. Объявлены `/oauth2/auth`, `/oauth2/token`, `/oauth2/revoke`, `authorization_code`, `refresh_token`, `S256`, `client_secret_post` и `offline_access`. Также объявлен endpoint Device Authorization. Разрешение этого grant для персонального клиента не проверялось. [OAuth metadata](https://oauth.vkusvill.ru/.well-known/oauth-authorization-server). |
| `confirmed-by-live-response` | POST `tools/call` для `vkusvill_orders_history` с `page: 1` без токена вернул HTTP 401. `WWW-Authenticate` указал Bearer, `purchase_history.read` и официальный URL metadata. Тело сообщило об отсутствии JWT. |
| `confirmed-by-code` | CLI отправляет один POST JSON-RPC напрямую, с `Accept: application/json, text/event-stream`. Он различает ошибку JSON-RPC и ошибку MCP, читает JSON из `result.content[0].text`, снимает `data` и умеет читать SSE. CLI обрабатывает `401` обновлением токена, а `403` и `insufficient_scope` — как недостаток прав. [src/api.js](https://github.com/vkustech/vkusvill-cli/blob/e49df83b43429c32cb5c9c6bbd6154757f72bb1a/src/api.js). |
| `confirmed-by-code` | CLI читает `items` или `orders`, `id_general` или `id`, `status_name`, `created_at`, `service`, `price`, `products`. Для даты он учитывает Unix timestamp в секундах и миллисекундах. Если `meta.has_more` отсутствует, CLI ищет следующую страницу при 10 записях. По официальному описанию история охватывает 30 дней и выдаёт 10 записей на страницу. [src/cli.js](https://github.com/vkustech/vkusvill-cli/blob/e49df83b43429c32cb5c9c6bbd6154757f72bb1a/src/cli.js), [src/views/profile.js](https://github.com/vkustech/vkusvill-cli/blob/e49df83b43429c32cb5c9c6bbd6154757f72bb1a/src/views/profile.js), [описание MCP](https://mcp.vkusvill.ru/mcp). |
| `confirmed-by-code` | CLI получает metadata ресурса в корне, сверяет issuer и origin endpoints, просит `offline_access`, отправляет секрет в форме токена и использует PKCE S256. В комментариях CLI зафиксированы ротация при каждом refresh и отзыв цепочки при повторном использовании. В релизах MCP добавление refresh token указано в октябре 2026. [src/auth.js](https://github.com/vkustech/vkusvill-cli/blob/e49df83b43429c32cb5c9c6bbd6154757f72bb1a/src/auth.js), [релизы MCP](https://github.com/vkustech/vkusvill-mcp-server-manifest/blob/85a8f06c218a1f744c3c92ea60e268c101a43801/README.md). |
| `confirmed-by-code` | Для варианта «У моего сервиса нет публичного URI» CLI документирует `http://localhost:3000/callback` и приём полного URL из браузера. Публичная статья ВкусВилла описывает ввод redirect URI своей платформы и режим без публичного адреса. Открытие страницы регистрации без сеанса ведёт к форме входа. [Руководство CLI](https://github.com/vkustech/vkusvill-cli/blob/e49df83b43429c32cb5c9c6bbd6154757f72bb1a/README.en.md), [статья ВкусВилла](https://habr.com/ru/companies/vkusvill/articles/1094732/), [регистрация](https://vkusvill.ru/oauth/registration/). |
| `assumption` | Для VkusVill OAuth ожидается возвращаемый scope либо стандартное сохранение запрошенного scope при его отсутствии, срок жизни `expires_in` и новый refresh token при каждой ротации. При явном HTTP 429 от token endpoint запрос считается отклонённым. При таймауте, HTTP 503, неоднозначном ответе или отсутствии нового refresh token повтор прежнего запрещён. Эти варианты покрыты локально. |
| `confirmed-by-live-response` | 19 ноября 2026 года пользователь сообщил об успешном ручном OAuth и предоставил состояния HA после чтения истории с реального аккаунта. Отображались номер, дата создания и исходный статус `Успешно завершен`. Время успешного опроса обновлялось при прежнем статусе. Источник — состояния HA, переданные пользователем. |
| `blocked` | Не проверены приём `https://my.home-assistant.io/redirect/oauth` для собственного клиента, ротация токена на реальном аккаунте, статусы активной доставки и задержка их обновления. |

## Решение по OAuth

- **BLOCKED для полностью автоматического callback My Home Assistant.** Документация HA описывает встроенный OAuth flow и `application_credentials`. Публичные примеры ВкусВилла показывают HTTPS callback для отдельных платформ. Для собственного клиента HA точный URI не проверен через действующую регистрацию. Серверное объявление Device Authorization не подтверждает право персонального клиента на этот grant. [OAuth в HA](https://contributors.home-assistant.io/core/integrations/oauth2/), [статья ВкусВилла о регистрации](https://habr.com/ru/companies/vkusvill/articles/1094732/).
- **GO для ручного PKCE.** Пользователь подтвердил первый вход через loopback с реальным аккаунтом. Используется возврат на loopback из официального CLI. HA хранит verifier и state в памяти пять минут, принимает полный URL с совпадающим state, извлекает code и обменивает его один раз. Интеграция не открывает этот URL, не слушает локальный порт и не принимает готовые токены из чужих программ. Секрет передаётся только после проверки metadata на `https://oauth.vkusvill.ru/oauth2/token`, bearer отправляется только на точный MCP endpoint. HTTP-редиректы отключены.
- При refresh используется блокировка на запись. Новый access token, refresh token и поколение гранта записываются одним обновлением ConfigEntry. Перед отправкой Core атомарно записывает и проверяет журнал с **несекретным** ID поколения. Если процесс завершится после потребления refresh token, но до сохранения новой пары, следующий Core попросит войти заново. Ошибочные ответы и диагностический экспорт не содержат значения OAuth или исходное тело.
- Для каждого рабочего аккаунта используется персональная регистрация ВкусВилла и согласие пользователя. Для перехода на встроенный callback нужно проверить точный URL в форме регистрации, полный браузерный цикл и ротацию с отдельным тестовым OAuth-клиентом.

## Решение по заказам

- **GO для отображения текста `status_name`.** Пользователь увидел исходный текст одной записи истории. Переходы статуса активного заказа нужно сопоставить с приложением ВкусВилла. Парсер принимает ISO 8601 в дополнение к timestamp из CLI. При отсутствии часового пояса в ISO для сортировки используется UTC.
- **BLOCKED для маппинга стадий.** Ни публичное описание инструмента, ни просмотренные исходники CLI не перечисляют значения поля. Наблюдение `Успешно завершен` подтверждает точное значение текста, а для его привязки к стадии нужна сверка с типом и исходом заказа. Таблица соответствий пуста. Проверки активного, доставленного и отменённого заказа используют вымышленные маркеры в памяти теста.
- Выбор по времени и остановка на короткой странице при отсутствии `has_more` являются правилами интеграции. Порядок страниц и полнота активных заказов в истории внешним сервисом не подтверждены. Отсутствие заказа в возвращённом окне не создаёт событие «доставлен».
- Время доставки или отмены в ответе MCP пока не подтверждено. Окно карточки начинается с первого опроса, обнаружившего подтверждённый конечный статус ранее активного заказа. Для восстановления окна HA сохраняет только срок и криптографический отпечаток заказа, привязанный к OAuth-гранту. Текст после перезапуска берётся из нового ответа MCP.
- При `429` и `503` клиент хранит дату следующего разрешённого запроса в текущем процессе HA, в том числе при перезагрузке записи. Пауза равна как минимум 60 секундам. Если срок превышает час, координатор может проверить его повторно без отправки запроса. Ответы ограничены 4 МиБ для MCP и 256 КиБ для OAuth, а один опрос — 60 секундами и восемью страницами.

Приведённые ниже публичные запросы выполнялись без учётных данных и без перехода по редиректам.

```sh
curl -i https://mcp.vkusvill.ru/.well-known/oauth-protected-resource/mcp
curl -i https://mcp.vkusvill.ru/.well-known/oauth-protected-resource
curl -i https://oauth.vkusvill.ru/.well-known/oauth-authorization-server
curl -i https://mcp.vkusvill.ru/mcp \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"vkusvill_orders_history","arguments":{"page":1}}}'
```

Документация HA по [config flow](https://contributors.home-assistant.io/core/integrations/config-flow/), [координаторам](https://contributors.home-assistant.io/core/integrations/fetching-data/), [качеству интеграций](https://contributors.home-assistant.io/core/quality-scale/), [диагностике](https://contributors.home-assistant.io/core/integrations/diagnostics/), [сущностям](https://contributors.home-assistant.io/core/entities/entity-class/) и [тестированию](https://contributors.home-assistant.io/core/development/testing/). Выбранная тестовая версия Core — `2026.10.4`.

## Локальная проверка

Python 3.14.10, Home Assistant Core 2026.10.4 и Ruff 0.15.12. Команды запускались из корня проекта в подготовленном виртуальном окружении.

| Команда | Результат |
| --- | --- |
| `python -m pytest -q` | 53 теста прошли. Конфигурация, параметры, reauth, выгрузка и повторная загрузка проверены на реальном Core с подставным HTTP-транспортом. |
| `python -m ruff check .` | Проверка пройдена. |
| `python -m ruff format --check .` | Проверены 18 Python-файлов. |
| `python -m compileall -q custom_components tests` | Компиляция пройдена. |

Дополнительный запуск `HttpClient` с реальным `aiohttp.ClientSession` подтвердил, что текущий код принимает обе публичные OAuth metadata и классифицирует анонимный ответ MCP как HTTP 401. Тесты обмена кода, refresh и истории заказов использовали синтетические ответы.

Сценарий проверки следующих статусов и времени заказа описан в [чеклисте ручной проверки](INTEGRATION_CHECKLIST.md).
