# Push Rules

`safe-push` 的受限豁免。只覆盖已人工核对为假的测试令牌、桥接配置字段和 token 变量标识；真实凭据（AWS/GitHub/私钥等）仍由扫描器的固定规则拦截。

```safe-push-allow
docs/agents/push-rules.md  ^.*$
core/src/talaria/workbench_api/app.py  token = settings\.token or secrets\.token_urlsafe\(32\)
core/src/talaria/workbench_api/service_descriptor.py  token = payload\.get\("token"\)
core/src/talaria/workbench_api/routes/analysis.py  (evidence_token =|anchor_token =)
core/src/talaria/cli/main.py  token = secrets\.token_urlsafe\(32\)
dsh/packages/talaria-desk-ui/src/lib/api.ts  ((const|let) token = (readSessionToken|apiToken)\(\);|apiToken = config\.token \?\? null;|const token = apiToken \?\? readSessionToken\(\);|token = response\.ok \? readSessionToken\(\) : "";)
dsh/packages/talaria-dsh-plugin/bridge.js  token: config\.token
dsh/packages/talaria-dsh-plugin/client/index.tsx  token: payload\.token
dsh/packages/talaria-dsh-plugin/index.js  token: (config|descriptor)\.token
dsh/packages/talaria-dsh-plugin/test/**  token: '(test|file|secret)-token'
tests/**  (TOKEN = "(test|scanner|briefing|m1-acceptance)-token"|token="(test|secret)-token"|token = (client\.cookies\.get\("talaria_token"\)|descriptor\["token"\]|_wait_for\()|token: 'test-token')
tests/cli/test_dsh_host.py  (token=(super-secret|recorded)|\?token=(super-secret|recorded|probe-secret))
tests/fixtures/m2/fake_dsh_host.py  (bridge_url, token = _load_bridge\(\)|\?token=recorded)
core/src/talaria/service_endpoint.py  token = payload\.get\("token"\)
core/src/talaria/workbench_api/routes/strategy_ws.py  scheme, _, token = websocket\.headers\.get\("authorization", ""\)\.partition\(" "\)
tests/cli/test_cli.py  "token": descriptor\.token
tests/cli/test_service.py  "HTTP_PROXY": "http://test-user:(p&ss|secret)@proxy\.example:8080
tests/cli/test_trade_backfill.py  (token="local-token"|token=settings\.token)
tests/workbench_api/test_paper_api.py  token="trader-session"
engine/crates/talaria-server/src/routes/analysis.rs  let ev_token = evidence_class\.trim\(\)\.to_uppercase\(\);
engine/crates/talaria-server/src/server.rs  token: token\.clone\(\),
workbench/packages/talaria-desk-ui/src/features/feed/deskSocketHub.ts  const token = getApiToken\(\);
workbench/packages/talaria-desk-ui/src/lib/api.ts  (apiToken = config\.token \?\? null;|const token = getApiToken\(\);|const metaToken = readMetaContent\("talaria-token"\);|configureApi\(\{ baseUrl: (conn\.base_url|metaBaseUrl \|\| apiBaseUrl), token: (conn\.token|metaMatch\[1\]) \}\);|token: (injected|data)\.token,|let token = readSessionToken\(\);|token = readSessionToken\(\);)
workbench/packages/talaria-desk-ui/src/chart/velaIndicators.ts  token: instanceId,
workbench/src-tauri/src/service.rs  (token: desc\.token\.clone\(\),|"token": info\.token,|"token": "secret-token-123",)
dsh/packages/talaria-dsh-plugin/bridge.js  (const explicitToken = config\.token \|\| process\.env\.TALARIA_SERVICE_TOKEN|token: explicitToken,|token: parsed\.token \})
dsh/packages/talaria-dsh-plugin/test/bridge.test.mjs  token: '(rust|desc)-token'
engine/crates/talaria-packs/src/us_equity/stream.rs  "secret": self\.api_secret,
```
