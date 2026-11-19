// OFM 管理页的本地侧车代理。
//
// OFM 只把管理界面监听在容器内的 127.0.0.1:18900，且 src/trust.js 要求 Host
// 必须是 loopback、Origin/Referer 必须与 Host 同源。因此本进程必须与
// our-free-model 共享网络命名空间（见 compose.yaml 的 network_mode），
// 对外只暴露在 lyph_private 内网，不发布宿主机端口。
//
// 职责只有三件事：
//   1. 用 settings.json 里的 managementKey 换一个 OFM 管理会话（服务端持有）；
//   2. 给每个请求注入该会话 Cookie，浏览器不需要再输入 OFM 令牌；
//   3. 把浏览器的 https Origin/Referer 改写成 OFM 认可的 loopback 形式。
//
// 不记录请求 URL、Cookie、Authorization 或 settings.json 内容。

import http from 'node:http'
import fs from 'node:fs'

const PORT = Number(process.env.OFM_PROXY_PORT || 18905)
const UPSTREAM_HOST = '127.0.0.1'
const UPSTREAM_PORT = 18900
const UPSTREAM_AUTHORITY = `${UPSTREAM_HOST}:${UPSTREAM_PORT}`
const SETTINGS_PATH = process.env.OFM_SETTINGS_PATH || '/data/settings.json'
const COOKIE_NAME = 'ofm_management_18900'
const SESSION_PATH = '/api/management/session'
const SESSION_TTL_MS = 7 * 60 * 60 * 1000
const MAX_BODY_BYTES = 32 * 1024 * 1024
const UPSTREAM_TIMEOUT_MS = 10 * 60 * 1000

// 只允许 Nginx（lyph_private 上的固定地址）和本机健康检查访问。
const ALLOWED_PEERS = new Set(
  (process.env.OFM_PROXY_ALLOW || '172.30.91.254,127.0.0.1')
    .split(',').map(value => value.trim()).filter(Boolean),
)

let session = null
let pending = null
let exitScheduled = false

const log = (...parts) => console.log(new Date().toISOString(), ...parts)

function readManagementKey() {
  const settings = JSON.parse(fs.readFileSync(SETTINGS_PATH, 'utf8'))
  const key = typeof settings.managementKey === 'string' && settings.managementKey
    ? settings.managementKey
    : settings.forwardKey
  if (typeof key !== 'string' || key.length === 0) {
    throw new Error('settings.json has no managementKey/forwardKey')
  }
  return { key, privileged: key === settings.managementKey }
}

function createSession(key) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ key })
    const request = http.request({
      host: UPSTREAM_HOST,
      port: UPSTREAM_PORT,
      method: 'POST',
      path: SESSION_PATH,
      agent: false,
      headers: {
        host: UPSTREAM_AUTHORITY,
        'content-type': 'application/json',
        'content-length': Buffer.byteLength(body),
        connection: 'close',
      },
    }, response => {
      const chunks = []
      response.on('data', chunk => chunks.push(chunk))
      response.on('end', () => {
        if (response.statusCode !== 200) {
          reject(new Error(`OFM session request failed with ${response.statusCode}`))
          return
        }
        const header = response.headers['set-cookie']?.[0] || ''
        const match = new RegExp(`^${COOKIE_NAME}=([^;]+)`).exec(header)
        if (!match) {
          reject(new Error('OFM session response has no management cookie'))
          return
        }
        resolve(match[1])
      })
    })
    request.setTimeout(15000, () => request.destroy(new Error('OFM session request timed out')))
    request.on('error', reject)
    request.end(body)
  })
}

async function ensureSession(force = false) {
  const { key, privileged } = readManagementKey()
  if (!force && session && session.key === key && Date.now() < session.until) return session.id
  if (pending) return pending
  const task = (async () => {
    const id = await createSession(key)
    session = { id, key, until: Date.now() + SESSION_TTL_MS }
    log(privileged ? 'OFM 管理会话已刷新（特权）' : 'OFM 管理会话已刷新（仅 forwardKey，部分页面会 403）')
    return id
  })()
  pending = task
  try {
    return await task
  } finally {
    if (pending === task) pending = null
  }
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    request.on('data', chunk => {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        reject(Object.assign(new Error('request body too large'), { statusCode: 413 }))
        request.destroy()
        return
      }
      chunks.push(chunk)
    })
    request.on('end', () => resolve(Buffer.concat(chunks)))
    request.on('error', reject)
  })
}

function upstreamHeaders(request, sessionId, bodyLength) {
  const headers = {}
  for (const [name, value] of Object.entries(request.headers)) {
    if ([
      'host', 'connection', 'keep-alive', 'proxy-connection', 'transfer-encoding',
      'upgrade', 'te', 'trailer', 'cookie', 'authorization', 'origin', 'referer',
      'content-length',
    ].includes(name)) continue
    if (value !== undefined) headers[name] = value
  }
  headers.host = UPSTREAM_AUTHORITY
  headers.cookie = `${COOKIE_NAME}=${sessionId}`
  if (request.headers.origin) headers.origin = `http://${UPSTREAM_AUTHORITY}`
  if (request.headers.referer) headers.referer = `http://${UPSTREAM_AUTHORITY}/`
  if (bodyLength > 0 || !['GET', 'HEAD'].includes(request.method || 'GET')) {
    headers['content-length'] = String(bodyLength)
  }
  headers.connection = 'close'
  return headers
}

function sendUpstream(method, path, headers, body) {
  return new Promise((resolve, reject) => {
    const request = http.request({
      host: UPSTREAM_HOST,
      port: UPSTREAM_PORT,
      method,
      path,
      headers,
      agent: false,
    }, response => resolve({ request, response }))
    request.setTimeout(UPSTREAM_TIMEOUT_MS, () => request.destroy(new Error('OFM upstream timed out')))
    request.on('error', reject)
    request.end(body.length ? body : undefined)
  })
}

function scheduleExit(reason) {
  if (exitScheduled) return
  exitScheduled = true
  log('OFM 上游不可达，退出以便重新加入容器网络命名空间:', reason)
  setTimeout(() => process.exit(1), 1500).unref()
}

function sendJson(response, status, payload) {
  const body = JSON.stringify(payload)
  response.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(body),
    'cache-control': 'no-store',
  })
  response.end(body)
}

async function handle(request, response) {
  const peer = String(request.socket.remoteAddress || '').replace(/^::ffff:/, '')
  if (!ALLOWED_PEERS.has(peer)) {
    sendJson(response, 403, { error: 'forbidden' })
    return
  }
  const pathname = (request.url || '/').split('?')[0]
  if (pathname === '/healthz') {
    try {
      await ensureSession()
      response.writeHead(200, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' })
      response.end('ok')
    } catch (error) {
      response.writeHead(503, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' })
      response.end('unavailable')
    }
    return
  }

  let body
  try {
    body = await readBody(request)
  } catch (error) {
    sendJson(response, error.statusCode || 400, { error: '请求内容无效' })
    return
  }

  for (let attempt = 0; attempt < 2; attempt += 1) {
    let sessionId
    try {
      sessionId = await ensureSession(attempt > 0)
    } catch (error) {
      log('OFM 管理会话创建失败:', error.message)
      sendJson(response, 503, { error: '模型工作台暂时不可用' })
      return
    }

    let upstream
    try {
      upstream = await sendUpstream(
        request.method || 'GET',
        request.url || '/',
        upstreamHeaders(request, sessionId, body.length),
        body,
      )
    } catch (error) {
      log('OFM 上游请求失败:', error.code || error.message)
      if (['ECONNREFUSED', 'EHOSTUNREACH', 'ENETUNREACH'].includes(error.code)) scheduleExit(error.code)
      if (!response.headersSent) sendJson(response, 502, { error: '模型工作台暂时不可用' })
      else response.destroy()
      return
    }

    const { response: upstreamResponse } = upstream
    if (upstreamResponse.statusCode === 401 && attempt === 0) {
      upstreamResponse.resume()
      await new Promise(resolve => upstreamResponse.on('end', resolve))
      session = null
      log('OFM 管理会话已失效，重新登录')
      continue
    }

    const headers = {}
    for (const [name, value] of Object.entries(upstreamResponse.headers)) {
      if (['connection', 'keep-alive', 'transfer-encoding', 'set-cookie'].includes(name)) continue
      headers[name] = value
    }
    response.writeHead(upstreamResponse.statusCode || 502, headers)
    upstreamResponse.pipe(response)
    upstreamResponse.on('error', () => response.destroy())
    request.on('aborted', () => upstreamResponse.destroy())
    return
  }
}

const server = http.createServer((request, response) => {
  handle(request, response).catch(error => {
    log('OFM 代理内部错误:', error.code || error.message)
    if (!response.headersSent) sendJson(response, 500, { error: '模型工作台暂时不可用' })
    else response.destroy()
  })
})
server.requestTimeout = 0
server.headersTimeout = 60000
server.keepAliveTimeout = 65000
server.listen(PORT, '0.0.0.0', () => log(`OFM 管理侧车已监听 :${PORT}`))
