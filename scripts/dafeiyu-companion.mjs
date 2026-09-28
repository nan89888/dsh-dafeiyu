#!/usr/bin/env node

// Codex-bound BigFish supervisor. The supervisor can run at login, but the
// pet and app-server only exist while ChatGPT.app is running.
import { spawn, execFileSync } from 'node:child_process'
import { appendFileSync, existsSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createInterface } from 'node:readline'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const pythonLauncher = resolve(root, 'scripts/macos-python-helper.sh')
const pythonHelper = resolve(root, 'runtime/helper.py')
const projectPython = resolve(root, '.build/python-env/bin/python')
const codex = process.env.DAFEIYU_CODEX_BIN || '/Applications/ChatGPT.app/Contents/Resources/codex'
const stateDir = resolve(process.env.DAFEIYU_STATE_DIR || resolve(homedir(), '.dsh/dafeiyu'))
const logPath = resolve(stateDir, 'companion.log')
const threadPath = resolve(stateDir, 'codex-thread.json')
const pluginEventPath = resolve(stateDir, 'plugin-events.jsonl')
const externalMarkerPath = resolve(stateDir, 'external-supervisor')
const chatGptExecutable = '/Applications/ChatGPT.app/Contents/MacOS/ChatGPT'

mkdirSync(stateDir, { recursive: true })
writeFileSync(externalMarkerPath, `${process.pid}\n`, { mode: 0o600 })
const log = (message) => {
  const line = `${new Date().toISOString()} ${message}\n`
  process.stdout.write(line)
  try { appendFileSync(logPath, line) } catch {}
}

if (process.platform !== 'darwin') process.exit(2)
const helperCommand = existsSync(pythonLauncher)
  ? { command: pythonLauncher, args: [], label: 'PySide6 macOS helper' }
  : { command: existsSync(projectPython) ? projectPython : (process.env.DAFEIYU_PYTHON || 'python3'), args: [pythonHelper], label: 'Python/PySide6 helper' }
if (!existsSync(pythonLauncher) && !existsSync(pythonHelper)) process.exit(2)

let pet
let appServer
let stopping = false
let threadId
let nextRpcId = 1
let requestQueue = Promise.resolve()
let appServerReady
let activeTurn
let selectedModel = process.env.DAFEIYU_MODEL || ''
let reasoningEffort = process.env.DAFEIYU_REASONING || ''
let petDisabledForSession = false
let chatGptProcessId
let petSessionProcessId
let pluginEventOffset = 0
const pending = new Map()
const now = () => Date.now()
const petMessage = (kind, payload = {}) => ({ protocolVersion: 1, kind, timestamp: now(), ...payload })
const sendPet = (kind, payload = {}) => {
  if (pet?.stdin?.writable) pet.stdin.write(`${JSON.stringify(petMessage(kind, payload))}\n`)
}

function pollPluginEvents() {
  if (!existsSync(pluginEventPath)) return
  try {
    const data = readFileSync(pluginEventPath, 'utf8')
    if (data.length > 2_000_000) {
      writeFileSync(pluginEventPath, data.slice(-500_000), { mode: 0o600 })
      pluginEventOffset = 0
      return
    }
    if (pluginEventOffset > data.length) pluginEventOffset = 0
    const chunk = data.slice(pluginEventOffset)
    pluginEventOffset = data.length
    for (const line of chunk.split('\n')) {
      if (!line.trim()) continue
      try {
        const event = JSON.parse(line)
        if (event?.protocolVersion === 1 && typeof event.kind === 'string'
          && pet?.stdin?.writable && !['user_input', 'settings'].includes(event.kind)) {
          pet.stdin.write(`${JSON.stringify(event)}\n`)
        }
      } catch {
        // Ignore an incomplete bridge line; the next state snapshot repairs it.
      }
    }
  } catch (error) {
    log(`桌宠状态桥接读取失败: ${error.message}`)
  }
}

function loadThreadId() {
  try {
    const value = JSON.parse(readFileSync(threadPath, 'utf8'))
    return typeof value?.threadId === 'string' && value.threadId ? value.threadId : undefined
  } catch {
    return undefined
  }
}

function saveThreadId(value) {
  try {
    writeFileSync(threadPath, `${JSON.stringify({ threadId: value }, null, 2)}\n`, { mode: 0o600 })
  } catch (error) {
    log(`Codex thread 保存失败: ${error.message}`)
  }
}

function currentChatGptProcessId() {
  try {
    const output = execFileSync('pgrep', ['-f', chatGptExecutable], { encoding: 'utf8' })
    const processId = Number.parseInt(output.trim().split(/\s+/)[0], 10)
    return Number.isSafeInteger(processId) && processId > 0 ? processId : undefined
  } catch {
    return undefined
  }
}

function codexIsRunning() {
  return currentChatGptProcessId() !== undefined
}

function rpc(method, params) {
  return new Promise((resolve, reject) => {
    if (!appServer?.stdin?.writable) return reject(new Error('Codex app-server 未连接'))
    const id = nextRpcId++
    pending.set(id, { resolve, reject })
    appServer.stdin.write(`${JSON.stringify({ id, method, params })}\n`)
  })
}

function notify(method, params) {
  if (appServer?.stdin?.writable) appServer.stdin.write(`${JSON.stringify({ method, params })}\n`)
}

function stopAppServer() {
  for (const reject of pending.values()) reject(new Error('Codex 已退出'))
  pending.clear()
  appServer?.kill('SIGTERM')
  appServer = undefined
  appServerReady = undefined
  if (activeTurn) {
    activeTurn.reject(new Error('Codex 已退出'))
    activeTurn = undefined
  }
}

function stopPet(reason = 'chatgpt-exited') {
  if (!pet) return
  const child = pet
  // Detach the old helper immediately.  On a ChatGPT relaunch the new
  // session must be allowed to spawn its own window without waiting for the
  // previous Qt process to finish its graceful shutdown timer.
  pet = undefined
  petSessionProcessId = undefined
  if (child.stdin?.writable) {
    child.stdin.write(`${JSON.stringify(petMessage('shutdown', { reason }))}\n`)
  }
  setTimeout(() => { if (!child.killed) child.kill('SIGTERM') }, 1200).unref?.()
}

function handleCodexNotification(event) {
  const method = event.method
  const params = event.params || {}
  if (method === 'item/agentMessage/delta') {
    if (activeTurn && params.threadId === activeTurn.threadId
      && (!activeTurn.turnId || params.turnId === activeTurn.turnId)) {
      activeTurn.reply += String(params.delta || '')
    }
  } else if (method === 'item/completed') {
    const item = params.item
    if (activeTurn && params.threadId === activeTurn.threadId
      && (!activeTurn.turnId || params.turnId === activeTurn.turnId) && item?.type === 'agentMessage') {
      const text = typeof item.text === 'string' ? item.text : ''
      if (text) activeTurn.reply = text
    }
  } else if (method === 'turn/started') {
    if (activeTurn && params.turn?.id) activeTurn.turnId = params.turn.id
    sendPet('state', { state: 'WORKING', message: 'Codex 正在工作', detail: 'Codex · 处理中' })
  } else if (method === 'turn/completed') {
    if (!activeTurn || params.threadId !== activeTurn.threadId
      || (activeTurn.turnId && params.turn?.id !== activeTurn.turnId)) return
    const turn = activeTurn
    activeTurn = undefined
    if (params.turn?.status === 'completed' && turn.reply.trim()) {
      sendPet('reply', { message: turn.reply, detail: 'Codex 回复完成' })
      log(`Codex 回复已发送到桌宠 (${turn.reply.length} 字符)`)
      turn.resolve()
    } else {
      const detail = params.turn?.error?.message || `turn 状态: ${params.turn?.status || 'unknown'}`
      sendPet('reply_error', { message: 'Codex 没有生成可显示的回复', detail })
      log(`Codex 回复为空或未完成: ${detail}`)
      turn.reject(new Error(detail))
    }
  }
}

function startAppServer() {
  if (appServer || stopping || !codexIsRunning()) return
  appServer = spawn(codex, ['app-server', '--stdio'], { cwd: root, env: process.env, stdio: ['pipe', 'pipe', 'pipe'] })
  appServer.stdout.setEncoding('utf8')
  createInterface({ input: appServer.stdout }).on('line', (line) => {
    if (!line.trim()) return
    let event; try { event = JSON.parse(line) } catch { return }
    if (event.id !== undefined && pending.has(event.id)) {
      const { resolve, reject } = pending.get(event.id); pending.delete(event.id)
      if (event.error) reject(new Error(event.error.message || 'Codex app-server error')); else resolve(event.result)
    } else handleCodexNotification(event)
  })
  appServer.stderr.setEncoding('utf8')
  appServer.stderr.on('data', (chunk) => { if (chunk.trim()) log(`app-server: ${chunk.trim()}`) })
  appServer.once('error', (error) => log(`app-server error: ${error.message}`))
  appServer.once('exit', () => {
    appServer = undefined
    appServerReady = undefined
    if (activeTurn) {
      activeTurn.reject(new Error('Codex app-server 已退出'))
      activeTurn = undefined
    }
    if (!stopping && codexIsRunning()) setTimeout(startAppServer, 1000).unref?.()
  })
  appServerReady = rpc('initialize', { clientInfo: { name: 'dafeiyu', title: 'DSH 大肥鱼', version: '0.1.0' } })
    .then(() => notify('initialized')).then(() => log('Codex app-server connected'))
    .catch((error) => {
      log(`Codex app-server initialize failed: ${error.message}`)
      throw error
    })
  // A failed initialization is reported when the user submits a message;
  // attach a handler now so a transient startup failure is not an unhandled
  // rejection that kills the lifecycle supervisor.
  appServerReady.catch(() => {})
}

async function submitToCodex(text) {
  if (!appServer) startAppServer()
  if (!appServer) throw new Error('Codex 桌面端尚未启动')
  await appServerReady
  if (!threadId) threadId = loadThreadId()
  if (threadId) {
    try {
      await rpc('thread/resume', { threadId })
    } catch (error) {
      log(`Codex 旧线程无法恢复，将创建新线程: ${error.message}`)
      threadId = undefined
    }
  }
  if (!threadId) {
    const result = await rpc('thread/start', { cwd: root, ephemeral: false, sandbox: 'read-only', threadSource: 'app' })
    threadId = result?.thread?.id
    if (!threadId) throw new Error('Codex 没有返回 thread id')
    saveThreadId(threadId)
  }
  sendPet('state', { state: 'THINKING', message: '正在交给 Codex 思考', detail: 'Codex · 新消息' })
  const completion = new Promise((resolve, reject) => {
    activeTurn = { threadId, turnId: undefined, reply: '', resolve, reject }
  })
  try {
    const turnParams = { threadId, input: [{ type: 'text', text }] }
    if (selectedModel) turnParams.model = selectedModel
    if (reasoningEffort) turnParams.effort = reasoningEffort
    const result = await rpc('turn/start', turnParams)
    const turnId = result?.turn?.id
    if (!turnId) throw new Error('Codex 没有返回 turn id')
    if (activeTurn) activeTurn.turnId = turnId
    await completion
  } catch (error) {
    if (activeTurn) {
      activeTurn = undefined
      throw error
    }
    throw error
  }
}

function handlePetEvent(event) {
  if (event.kind === 'closed') {
    // A user-selected close is intentional for the current ChatGPT session.
    // Keep the supervisor alive, but do not recreate the helper until a new
    // ChatGPT process/session is observed.
    petDisabledForSession = true
    log('用户已关闭大肥鱼，本次 ChatGPT 会话保持关闭')
    return
  }
  if (event.kind === 'settings') {
    if (typeof event.model === 'string') selectedModel = event.model
    if (typeof event.reasoningEffort === 'string') reasoningEffort = event.reasoningEffort
    log(`桌宠设置已更新: model=${selectedModel || 'default'}, effort=${reasoningEffort || 'default'}`)
    return
  }
  if (event.kind !== 'user_input' || typeof event.text !== 'string' || !event.text.trim()) return
  const text = event.text.trim()
  log(`收到桌宠输入 (${text.length} 字符)`)
  requestQueue = requestQueue.catch(() => {}).then(() => submitToCodex(text)).catch((error) => {
    sendPet('reply_error', { message: 'Codex 没有回复', detail: error.message }); log(`Codex turn failed: ${error.message}`)
  })
}

function startPet(sessionProcessId) {
  if (pet || petDisabledForSession || stopping || !codexIsRunning()) return
  petSessionProcessId = sessionProcessId
  const helperEnv = {
    ...process.env,
    DSH_DAFEIYU_WEBUI_URL: 'http://127.0.0.1:3080/',
    DSH_DAFEIYU_BUBBLE_MODE: 'always',
  }
  // Do not inject a topmost override on every launch.  The helper persists
  // the user's ordinary/topmost choice in layout.json; forcing `topmost` here
  // made “普通窗口层级” revert on the next ChatGPT restart.
  if (process.env.DSH_DAFEIYU_WINDOW_LEVEL) {
    helperEnv.DSH_DAFEIYU_WINDOW_LEVEL = process.env.DSH_DAFEIYU_WINDOW_LEVEL
  }
  pet = spawn(helperCommand.command, helperCommand.args, {
    cwd: root,
    argv0: 'DSH',
    env: helperEnv,
    stdio: ['pipe', 'pipe', 'pipe'],
  })
  const child = pet
  pet.stdout.setEncoding('utf8')
  createInterface({ input: pet.stdout }).on('line', (line) => {
    try {
      const event = JSON.parse(line)
      if (event.kind === 'ready') {
        sendPet('hello', { state: 'IDLE', host: 'codex', message: '大肥鱼已连接 Codex' })
        sendPet('state', { state: 'IDLE', message: '我在桌面上等你呢', detail: 'Codex · 输入框已连接' })
        log(`BigFish helper ready (${helperCommand.label})`)
      } else handlePetEvent(event)
    } catch {}
  })
  pet.stderr.setEncoding('utf8')
  pet.stderr.on('data', (chunk) => { if (chunk.trim()) log(`helper: ${chunk.trim()}`) })
  pet.once('error', (error) => log(`helper error: ${error.message}`))
  pet.once('exit', (code, signal) => {
    if (pet === child) pet = undefined
    // A helper from the preceding ChatGPT process may finish shutting down
    // after the next process has started. It must not suppress the new
    // session's pet.
    if (petSessionProcessId === sessionProcessId && chatGptProcessId === sessionProcessId) {
      petDisabledForSession = true
      log(`大肥鱼已关闭，本次 ChatGPT 会话不再自动重启 (code=${String(code)}, signal=${String(signal)})`)
    } else {
      log(`上一会话的大肥鱼已关闭 (code=${String(code)}, signal=${String(signal)})`)
    }
  })
}

function pollLifecycle() {
  if (stopping) return
  const observedProcessId = currentChatGptProcessId()
  if (observedProcessId !== undefined) {
    if (chatGptProcessId !== observedProcessId) {
      const previousProcessId = chatGptProcessId
      chatGptProcessId = observedProcessId
      petDisabledForSession = false
      if (previousProcessId !== undefined) {
        log(`检测到新的 ChatGPT 会话 (${previousProcessId} -> ${observedProcessId})，重新显示大肥鱼`)
        stopAppServer()
        stopPet('chatgpt-session-replaced')
      }
    }
    startPet(observedProcessId)
    startAppServer()
  } else {
    if (pet || appServer) { log('ChatGPT.app 已退出，关闭大肥鱼和 Codex 连接'); stopAppServer(); stopPet() }
    chatGptProcessId = undefined
    petDisabledForSession = false
  }
}

function shutdown() {
  if (stopping) return
  stopping = true
  stopAppServer()
  stopPet()
  try { unlinkSync(externalMarkerPath) } catch {}
}
process.once('SIGINT', shutdown)
process.once('SIGTERM', shutdown)
pollLifecycle()
setInterval(pollLifecycle, 1000).unref()
setInterval(pollPluginEvents, 250).unref()
log('大肥鱼生命周期监督器已启动，等待 ChatGPT.app')
