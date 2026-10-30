/**
 * mymem · OpenCode 插件
 *
 * 干什么：每次「一件事」做完（一轮对话结束），把这一轮的**总结**推到 mymem
 *   `POST /api/v1/ingest`，落到对应项目（按工作目录映射）的记忆里。
 *
 * 为什么用插件而不是让 AI "记得调用"：
 *   模型在专心改代码时指令遵循会掉，"每轮都上报"必然漏。插件的钩子是确定性的。
 *
 * 总结从哪来：
 *   优先用这一轮助手的最终回复正文（它本身就是这次干活的结论），
 *   若本轮出现 OpenCode 自己的压缩摘要，则直接用压缩摘要。
 *   **不额外调模型**——不花钱、不排队、不会因为模型报错就丢记录。
 *
 * 数据落哪：`~/.local/share/opencode-mymem/`
 *   plugin.log   运行日志（排查用，不进对话）
 *   state.json   每个会话已上报到哪条消息（避免重复上报）
 *
 * 配置：`~/.config/opencode/mymem.json`（可选，缺省走默认值）
 *   {
 *     "url": "http://1.92.76.106",
 *     "token": "",
 *     "projects": { "D:\\program\\code\\mes-ai-service": "agent-my" }
 *   }
 *   - url       mymem 服务地址（默认 http://1.92.76.106）
 *   - token     服务端开了 MYMEM_TOKEN 时填，会以 Bearer 发送
 *   - projects  工作目录 → 项目名；没配的目录取目录名
 *
 * 注意：这是 OpenCode V2 的插件 API（@opencode/plugin），V1 是 @opencode-ai/plugin，
 * 包名、入口、事件信封全都不同，不要混。
 */

import { Plugin } from "@opencode/plugin"
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs"
import { homedir, hostname, platform, release } from "node:os"
import { basename, join } from "node:path"

// ─────────────────────────────────────────────────────────────
// 配置
// ─────────────────────────────────────────────────────────────

const CONFIG_FILE = join(homedir(), ".config", "opencode", "mymem.json")
const STATE_DIR = join(homedir(), ".local", "share", "opencode-mymem")
const STATE_FILE = join(STATE_DIR, "state.json")
const LOG_FILE = join(STATE_DIR, "plugin.log")

const DEFAULT_URL = "http://1.92.76.106"
const AGENT = "openchamber"

/** 一轮内容太少不值得上报（比如只有"好的"这种） */
const MIN_SUMMARY_CHARS = 80
/** 上报正文上限，超出截断（mymem 单条记忆不适合塞几万字） */
const MAX_SUMMARY_CHARS = 6000
/** 同一会话两次上报的最小间隔，避免一轮里反复触发 */
const COOLDOWN_MS = 5_000

/** 视为"一轮结束"的事件。宁可多认几个——真正的去重靠消息 ID */
const TURN_END_EVENTS = new Set([
  "session.execution.ended",
  "session.execution.completed",
  "session.idle",
  "session.turn.ended",
  "session.turn.completed",
  "session.step.ended",
  "session.text.ended",
  "session.completed",
  "session.message.completed",
])

interface MemConfig {
  url: string
  token: string
  projects: Record<string, string>
}

function readConfig(): MemConfig {
  try {
    const raw = JSON.parse(readFileSync(CONFIG_FILE, "utf8"))
    return {
      url: String(raw?.url ?? DEFAULT_URL).replace(/\/+$/, ""),
      token: String(raw?.token ?? ""),
      projects: (raw?.projects ?? {}) as Record<string, string>,
    }
  } catch {
    return { url: DEFAULT_URL, token: "", projects: {} }
  }
}

// ─────────────────────────────────────────────────────────────
// 本地状态（只用来去重）
// ─────────────────────────────────────────────────────────────

type State = Record<string, { lastMessageID: string; at: number }>

function readState(): State {
  try {
    const raw = JSON.parse(readFileSync(STATE_FILE, "utf8"))
    return raw && typeof raw === "object" ? (raw as State) : {}
  } catch {
    return {}
  }
}

function writeState(state: State): void {
  try {
    mkdirSync(STATE_DIR, { recursive: true })
    writeFileSync(STATE_FILE, `${JSON.stringify(state, null, 2)}\n`, "utf8")
  } catch {
    /* 状态写不进去只影响去重，不能影响用户干活 */
  }
}

function log(msg: string): void {
  try {
    mkdirSync(STATE_DIR, { recursive: true })
    appendFileSync(LOG_FILE, `[${new Date().toISOString()}] ${msg}\n`, "utf8")
  } catch {
    /* 忽略 */
  }
}

// ─────────────────────────────────────────────────────────────
// 消息解析
//
// V2 的消息是扁平的：{ id, time, type, content: [...] }，
// type 为 user / assistant / compaction / idle。
// assistant 的正文在 content[] 里，part 的 type 是 text / reasoning / tool。
// ─────────────────────────────────────────────────────────────

interface Turn {
  user: string
  assistant: string[]
  tools: string[]
}

function extractText(m: any): string {
  if (!m) return ""
  const out: string[] = []
  if (Array.isArray(m.content)) {
    for (const p of m.content) {
      if (!p) continue
      if (typeof p === "string") {
        out.push(p)
        continue
      }
      if (p.type === "text" && typeof p.text === "string") out.push(p.text)
      else if (p.type === "tool" && typeof p.name === "string") out.push(`[工具] ${p.name}`)
    }
  }
  if (typeof m.payload?.text === "string") out.push(m.payload.text)
  if (typeof m.text === "string") out.push(m.text)
  return out.join("\n").trim()
}

function toolNames(m: any): string[] {
  const names: string[] = []
  if (Array.isArray(m?.content)) {
    for (const p of m.content) {
      if (p?.type === "tool" && typeof p.name === "string") names.push(p.name)
    }
  }
  return names
}

/** 一条 user 到下一 user 之间算一轮 */
function toTurns(messages: any[]): Turn[] {
  const turns: Turn[] = []
  let cur: Turn | null = null

  for (const m of messages) {
    const t = m?.type
    if (t === "user") {
      if (cur) turns.push(cur)
      cur = { user: extractText(m), assistant: [], tools: [] }
    } else if (t === "assistant") {
      if (!cur) cur = { user: "", assistant: [], tools: [] }
      const txt = extractText(m)
      if (txt) cur.assistant.push(txt)
      cur.tools.push(...toolNames(m))
    } else if (t === "compaction" && typeof m?.summary === "string") {
      if (cur) cur.assistant.push(`[会话压缩摘要]\n${m.summary}`)
      else turns.push({ user: "", assistant: [`[会话压缩摘要]\n${m.summary}`], tools: [] })
    }
  }
  if (cur) turns.push(cur)

  return turns.filter((t) => t.user || t.assistant.length > 0)
}

// ─────────────────────────────────────────────────────────────
// 脱敏
//
// 对话正文里会带密钥：用户自己的配置里就有明文口令，命令里出现
// `MYSQL_PASSWORD=xxx` 是常态。推到远端之前必须过一遍。
// ─────────────────────────────────────────────────────────────

function redact(text: string): string {
  if (!text) return text
  return text
    .replace(/\b(sk|pk)-[A-Za-z0-9_-]{16,}/g, "$1-***")
    .replace(
      /(PGPASSWORD|MYSQL_PWD|password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key)\s*[:=]\s*["']?[^\s"',;)]{6,}/gi,
      "$1=***",
    )
    .replace(/(Bearer\s+)[A-Za-z0-9._-]{16,}/gi, "$1***")
    .replace(/\b[0-9a-f]{32,}\b/gi, "***")
    .replace(/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g, "***")
}

// ─────────────────────────────────────────────────────────────
// 上报
// ─────────────────────────────────────────────────────────────

function summarize(turns: Turn[]): string {
  // 有压缩摘要就优先用它：那本来就是摘要
  const compaction = turns
    .flatMap((t) => t.assistant)
    .find((a) => a.startsWith("[会话压缩摘要]"))
  if (compaction) return compaction.replace("[会话压缩摘要]\n", "").trim()

  const userText = turns
    .map((t) => t.user)
    .filter(Boolean)
    .join("\n")
    .trim()
  const assistantText = turns
    .at(-1)
    ?.assistant.filter((a) => !a.startsWith("[工具]"))
    .join("\n")
    .trim()

  const parts: string[] = []
  if (userText) parts.push(`用户诉求：${userText.slice(0, 600)}`)
  if (assistantText) parts.push(`完成情况：${assistantText}`)

  const tools = [...new Set(turns.flatMap((t) => t.tools))]
  if (tools.length) parts.push(`使用工具：${tools.join(", ")}`)

  return parts.join("\n\n").trim()
}

function projectOf(dir: string, cfg: MemConfig): string {
  const direct = cfg.projects[dir]
  if (direct) return direct
  // Windows 大小写不敏感，再按小写匹配一次
  const lower = dir.toLowerCase()
  for (const [k, v] of Object.entries(cfg.projects)) {
    if (k.toLowerCase() === lower) return v
  }
  return basename(dir) || "unknown"
}

function systemIdentity(): string {
  return `${hostname()}|${platform()} ${release()}`
}

async function push(summary: string, project: string): Promise<boolean> {
  const cfg = readConfig()
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, "0")
  const stamp = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`

  const payload = {
    project,
    agent: AGENT,
    system: systemIdentity(),
    memories: [
      {
        category: "context",
        title: `任务总结 ${stamp}`,
        content: summary.slice(0, MAX_SUMMARY_CHARS),
        tags: ["openchamber", "auto-report"],
      },
    ],
  }

  try {
    const res = await fetch(`${cfg.url}/api/v1/ingest`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(cfg.token ? { Authorization: `Bearer ${cfg.token}` } : {}),
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(15_000),
    })
    if (!res.ok) {
      log(`上报失败 [${project}] ${res.status} ${res.statusText}`)
      return false
    }
    const body: any = await res.json().catch(() => ({}))
    log(
      `已上报 [${project}] created=${(body?.created ?? []).length} merged=${(body?.merged ?? []).length} masked=${body?.masked ?? 0} chars=${summary.length}`,
    )
    return true
  } catch (err) {
    log(`上报异常 [${project}] ${String(err)}`)
    return false
  }
}

// ─────────────────────────────────────────────────────────────
// 插件
// ─────────────────────────────────────────────────────────────

export default Plugin.define({
  id: "mymem",

  async setup(ctx) {
    mkdirSync(STATE_DIR, { recursive: true })

    // 插件按 location 多次加载，每个实例只管自己目录的事件，
    // 否则同一个事件会被 N 个实例各报一遍。
    const myDir: string = String((ctx as any)?.location?.directory ?? "")
    const project = projectOf(myDir, readConfig())

    const seenEvents = new Set<string>()
    const lastPushed = new Map<string, number>()
    const controller = new AbortController()

    async function flush(sessionID: string): Promise<void> {
      const messages = await ctx.session.context({ sessionID })
      const list: any[] = Array.isArray(messages) ? messages : []
      if (list.length === 0) return

      const state = readState()
      const last = state[sessionID]?.lastMessageID
      let startIdx = 0
      if (last) {
        const i = list.findIndex((m) => m?.id === last)
        startIdx = i >= 0 ? i + 1 : 0
      }
      const fresh = list.slice(startIdx)
      if (fresh.length === 0) return

      const lastID = String(list[list.length - 1]?.id ?? "")
      const turns = toTurns(fresh)
      const summary = redact(summarize(turns))
      if (summary.length < MIN_SUMMARY_CHARS) {
        // 这一轮没实质内容，仍然推进游标，避免下次把旧内容当成新的
        state[sessionID] = { lastMessageID: lastID, at: Date.now() }
        writeState(state)
        return
      }

      const ok = await push(summary, project)
      if (ok) {
        state[sessionID] = { lastMessageID: lastID, at: Date.now() }
        writeState(state)
      }
    }

    // 事件流：等到一轮结束就上报
    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          const e = event as any
          const type = String(e?.type ?? "")

          if (e?.location?.directory && myDir && e.location.directory !== myDir) continue
          if (typeof e?.id === "string") {
            if (seenEvents.has(e.id)) continue
            seenEvents.add(e.id)
            if (seenEvents.size > 2000) seenEvents.clear()
          }
          if (!TURN_END_EVENTS.has(type)) continue

          const sid = e?.data?.sessionID ?? e?.durable?.aggregateID
          if (typeof sid !== "string" || !sid.startsWith("ses")) continue

          const now = Date.now()
          if (now - (lastPushed.get(sid) ?? 0) < COOLDOWN_MS) continue
          lastPushed.set(sid, now)

          try {
            await flush(sid)
          } catch (err) {
            log(`flush ${sid} 失败：${String(err)}`)
          }
        }
      } catch (err) {
        log(`事件流结束：${String(err)}`)
      }
    })()

    log(`mymem 插件就绪 project=${project} dir=${myDir} url=${readConfig().url}`)

    return () => {
      controller.abort()
    }
  },
})
