# Push Rules

`safe-push` 的受限豁免。只覆盖已人工核对为假的测试令牌、桥接配置字段和 token 变量标识；真实凭据（AWS/GitHub/私钥等）仍由扫描器的固定规则拦截。

```safe-push-allow
docs/agents/push-rules.md  ^.*$
core/src/talaria/workbench_api/app.py  token = settings\.token or secrets\.token_urlsafe\(32\)
core/src/talaria/workbench_api/service_descriptor.py  token = payload\.get\("token"\)
core/src/talaria/workbench_api/routes/analysis.py  (evidence_token =|anchor_token =)
core/src/talaria/cli/main.py  token = secrets\.token_urlsafe\(32\)
dsh/packages/talaria-desk-ui/src/lib/api.ts  (const token = (readSessionToken|apiToken)\(\);|apiToken = config\.token \?\? null;|const token = apiToken \?\? readSessionToken\(\);)
dsh/packages/talaria-dsh-plugin/bridge.js  token: config\.token
dsh/packages/talaria-dsh-plugin/client/index.tsx  token: payload\.token
dsh/packages/talaria-dsh-plugin/index.js  token: (config|descriptor)\.token
dsh/packages/talaria-dsh-plugin/test/**  token: '(test|file|secret)-token'
tests/**  (TOKEN = "(test|scanner|briefing|m1-acceptance)-token"|token="(test|secret)-token"|token = (client\.cookies\.get\("talaria_token"\)|descriptor\["token"\]|_wait_for\()|token: 'test-token')
tests/cli/test_dsh_host.py  (token=(super-secret|recorded)|\?token=(super-secret|recorded|probe-secret))
docs/research/news-feed-data-sources.md  https://data\.coindesk\.com/blogs/changes-to-coindesk-data-indices-api-free-tier-access
tests/fixtures/m2/fake_dsh_host.py  (bridge_url, token = _load_bridge\(\)|\?token=recorded)
```
