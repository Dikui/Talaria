import { readFileSync } from 'node:fs'

import { createBridge, renderSnapshot, SNAPSHOT_OUTPUT_SCHEMA } from './bridge.js'
import { createDeskGuard } from './guard.js'

const toolSchema = JSON.parse(readFileSync(new URL('./get_snapshot.tool.json', import.meta.url), 'utf8'))

export const name = '@talaria/dsh-plugin'
export const inject = ['tools', 'sessions', 'webServer']

/**
 * talaria-desk Profile 的宿主插件：
 * - 注册只读 get_snapshot 工具，经回环 HTTP + Bearer 调用 Python 守护进程；
 * - 挂载单调执行守卫，拦截非白名单工具与 fork 历史写操作重放。
 */
export function apply(ctx, config = {}) {
  const bridge = createBridge({
    credentialFile: config.credentialFile,
    baseUrl: config.baseUrl,
    token: config.token,
  })

  ctx.tools.register({
    name: toolSchema.name,
    description: `${toolSchema.description} 只读；返回的 snapshot_id 必须用于后续所有读取。`,
    parameters: toolSchema.inputSchema,
    output: {
      schema: SNAPSHOT_OUTPUT_SCHEMA,
      render: renderSnapshot,
    },
    async execute(args, exec) {
      return bridge.createSnapshot(args, exec.signal)
    },
  })

  const getSession = (exec) => exec.agent?.session ?? (exec.agent ? ctx.sessions.get(exec.agent.id) : undefined)
  ctx.tools.guard(createDeskGuard({ getSession }))

  // 浏览器嵌入所需的回环守护进程地址与 Bearer token 只经同源 bootstrap 下发。
  ctx.effect(() =>
    ctx.webServer.register({
      kind: 'exact',
      path: '/talaria/bootstrap',
      handler(_request, response) {
        const credentialFile = process.env.TALARIA_DSH_BRIDGE
        try {
          const descriptor = JSON.parse(readFileSync(credentialFile, 'utf8'))
          response.writeHead(200, {
            'content-type': 'application/json; charset=utf-8',
            'cache-control': 'no-store',
          })
          response.end(
            JSON.stringify({
              base_url: descriptor.base_url,
              token: descriptor.token,
            }),
          )
        } catch (error) {
          response.writeHead(503, { 'content-type': 'application/json; charset=utf-8' })
          response.end(
            JSON.stringify({
              code: 'TALARIA_BOOTSTRAP_UNAVAILABLE',
              message: `无法读取 Talaria 桥接凭据：${error.message}`,
            }),
          )
        }
      },
    }),
  )
}
