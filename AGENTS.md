# AGENTS.md — правила для AI coding-агентов в репозитории VkusVill Home Assistant

## Mission and scope

Разрабатываем **native Home Assistant custom integration** для Home Assistant OS. Только официальный `https://mcp.vkusvill.ru/mcp`, получение заказов и их статусов; без оформления заказов, scraping, мобильного reverse engineering и неофициальных endpoints. Весь runtime — Python внутри Home Assistant Core. Никаких обязательных Node.js, CLI, cron, отдельного контейнера/add-on и YAML-настройки.

Продуктовая цель: надёжно предоставить HA *то, что реально известно* о текущем заказе. Не обещать «сборка → у курьера → доставлен», пока это не подтверждено значениями API. Не использовать догадки при статусах, неизвестных или отсутствующих полях.

## Preflight: inspect before edit

1. Посмотри структуру репозитория, существующие файлы, тесты, конфигурацию CI и git status. Не удаляй чужую работу.
2. Изучи на текущий момент документацию Home Assistant developers по config flow, OAuth/application credentials, coordinators, diagnostics, entity naming/unique_id и tests.
3. Изучи исходники https://github.com/vkustech/vkusvill-cli : `src/api.js`, `src/auth.js`, `src/cli.js`, `src/views/profile.js`, а также https://github.com/vkustech/vkusvill-mcp-server-manifest .
4. В `docs/API_FINDINGS.md` фиксируй источники фактов и степень уверенности: `confirmed-by-code`, `confirmed-by-live-response`, `assumption`, `blocked`.
5. Если нет сетевого доступа, явно запиши это и работай с проверяемыми контрактными фикстурами. Не объявляй live-совместимость доказанной.

## Known API facts (verify against upstream)

- MCP endpoint: `https://mcp.vkusvill.ru/mcp`.
- JSON-RPC: `method=tools/call`, `params.name=vkusvill_orders_history`, `params.arguments={"page": 1}`.
- OAuth: authorization code + PKCE S256, permission/scope `purchase_history.read`; refresh tokens rotate and must never refresh concurrently.
- Observed by upstream CLI: response may contain `items` or `orders`; fields `id_general`/`id`, `status_name`, `created_at`, `service`, `price`, `products`, `meta.has_more`.
- Result may be wrapped inside `result.content[].text` and inner `{data: ...}`; JSON-RPC `error` and MCP tool-level failures must be handled distinctly; server may use SSE.
- Upstream CLI covers about 30 days of orders, pages of around 10; don't infer guarantees on sorting/status timing.
- OAuth callback support for HA is **unverified**. The CLI's default `http://localhost:3000/callback` is not a valid assumption for HAOS. Verify provider redirect registration and HA OAuth design before claiming end-to-end setup.

## Core architecture

- Domain: `vkusvill`. Code in `custom_components/vkusvill/`.
- Use one API client and one `DataUpdateCoordinator` per `ConfigEntry`.
- Use HA-managed `aiohttp` client session, native async APIs, properly bounded timeouts; no blocking I/O in event loop.
- Prefer supported HA OAuth `application_credentials` and built-in OAuth helpers **only if compatible with VkusVill's authorization server and allowed redirect**. Do not fake OAuth support or hardcode an invalid callback.
- Centralize refresh behind `asyncio.Lock` **per entry** and update token state atomically. Preserve rotated refresh token. Support reauth and ensure reload/unload lifecycle is sound.
- Define a typed model for parsed orders/statuses and transport exceptions. Transport parsing must be separate from status normalization and entity exposure.
- Use a `DataUpdateCoordinator` with default 120s polling; configurable within a documented bounded range (>=60s). On 429/503 obey `Retry-After`; do not retry indefinitely or trigger concurrent polling storms.
- Make state transitions deterministic. Unknown/unobserved != false. API outage != no active orders.
- Stable entity `unique_id`, translations, icon/device classes when semantically correct, and proper `available` handling. Avoid uncontrolled entity proliferation due to changing order IDs.
- Provide diagnostics with redaction and meaningful but safe logging.

## Data semantics — NON-NEGOTIABLE

- Always preserve raw `status_name` in a dedicated user-visible sensor (unless missing); normalized status is separate.
- Normalized states: `assembling`, `with_courier`, `delivered`, `cancelled`, `other`, `unknown`. Map only **verified** status values/ids from empirical data or authoritative docs. Keep mapping table easy to extend and test.
- Never infer `delivered` because the order disappeared from response. Never infer `with_courier` from estimated time or creation age.
- If active/terminal classification is not known, avoid false binary `off`; mark relevant entity unknown/unavailable instead.
- Do not rely solely on first page or assume latest array element is active. Implement bounded paging and stable selection logic; document multiple-active-order behavior.
- Avoid raw order payload, customer contact details, address, product lines, and personal identifiers in state attributes/logging/diagnostics; order number as entity state only when expressly necessary.
- Stop polling/re-auth storms on unauthorized/permission errors; initiate supported HA reauth rather than deleting tokens silently.

## Security and privacy

- Bearer/access/refresh tokens and client_secret are secrets: never print, log, expose in state attributes, diagnostics, traces, examples or git history. No credential fixtures in repo.
- Tokens only sent to official HTTPS VkusVill hosts discovered/validated through OAuth metadata or the exact MCP endpoint. Reject cross-host redirects, insecure schemes and unexpected discovered endpoints.
- Check standard RFC 8414 / RFC 9728 metadata parsing; validate origin/host and protocol. Never blindly trust URLs from arbitrary API responses.
- Set request timeouts, body size caps, error string caps, pagination caps. Sanitize provider-supplied text before rendering/logging.
- No disk caching of personalized order responses. HA credential store/config entry is allowed for OAuth secrets via supported HA facilities; don't invent custom plaintext files.
- Diagnostics export must be scrubbed. If unsure a field is private, omit it.
- Respect 429/503 and `Retry-After`. Do not bypass the provider's usage controls.

## Expected project layout

```text
custom_components/vkusvill/
  __init__.py
  manifest.json
  const.py
  api.py
  coordinator.py
  config_flow.py
  sensor.py
  binary_sensor.py
  strings.json
  translations/ru.json
  translations/en.json
  diagnostics.py
  application_credentials.py   # only if verified/appropriate
README.md
AGENTS.md
docs/API_FINDINGS.md
tests/
.github/workflows/            # checks, where appropriate
```

You may refactor structure if it clearly improves compatibility with current HA APIs. `README.md` must document HAOS install path `/config/custom_components/vkusvill/`, setup, OAuth registration requirements, known limitations, troubleshooting and examples.

## Testing & quality gates

- Write deterministic fixture-based tests for wrapped/flat JSON, malformed payload, `isError`, pagination, active/terminal/unknown statuses, empty response, multiple orders, errors 401/403/429/503, `Retry-After`, refresh rotation/race, timeout, SSE, logging redaction, unavailable entities, setup/reload/unload/reauth.
- Add mock OAuth discovery + token refresh test. If a real OAuth callback is unverified, model the flow as unverified; tests cannot substitute for live provider acceptance.
- Use `pytest`, `ruff`, Home Assistant test fixtures/libraries pinned to a supported HA Core release. Keep any optional typing/static check passing.
- Run commands available in environment and record exact commands/results. Never claim tests passed unless actually run.
- Keep third-party Python dependencies minimal and manifest compatible with HAOS.
- Keep code type-annotated, small, reviewable and well-named. Prefer standard HA patterns over custom frameworks.

## Change protocol

For each meaningful change:
1. Inspect impacted files and confirm expected behavior.
2. Make smallest coherent code change.
3. Add/update tests and docs at the same time.
4. Run relevant checks, fix failures, do not hide failures.
5. Summarize implementation and any unsupported assumptions.

Do not delete or rewrite unrelated project files. Do not request real account passwords/tokens. Do not deploy into user's HAOS without explicit user request and access. Do not claim real delivery stage verification without an authorized live test.

## Definition of done

- Installable `custom_components/vkusvill`, UI config flow, appropriate auth/reauth path, robust coordinator, safe sensors, translations and diagnostics.
- Explicit go/no-go assessment for OAuth callback and delivery stage fidelity; blockers visible in `docs/API_FINDINGS.md` and README.
- All achievable local tests/linters run and outcomes reported.
- Final report includes installation instructions, entities, example HA automation, verification status (mock/live), and known limitations.
