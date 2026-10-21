/**
 * Harness2 adaptation sweeps, driven from DeepSeek Harness.
 *
 * `harness2_improve` starts one `harness2-dsh run` in the background and returns the
 * run directory immediately; `harness2_result` reports whether it finished and hands
 * back the adopted harness; `harness2_cancel` stops it. A sweep is several headless
 * DSH sessions, so it never blocks the calling turn.
 *
 * Run state lives in the run directory (`pid`, `exit_code`, `summary.json`), so a
 * sweep started before a DSH restart can still be polled or cancelled after it.
 * Relative paths resolve against the calling session's workspace.
 *
 * @module dsh-plugin-harness2
 */

import { spawn, execFile } from 'node:child_process'
import {
  accessSync,
  closeSync,
  constants,
  existsSync,
  openSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { mkdir } from 'node:fs/promises'
import { homedir } from 'node:os'
import { promisify } from 'node:util'
import path from 'node:path'

export const name = 'harness2-tools'
export const inject = ['tools']

const execFileAsync = promisify(execFile)

const textBlocks = (text) => [{ type: 'text', text }]

// Tool parameters stay inside DSH's enforced JSON Schema subset: object-level
// `required` arrays, no numeric range keywords.
const IMPROVE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['harness', 'tasks'],
  properties: {
    harness: {
      type: 'string',
      description:
        'Directory of the base harness to refine: it must contain AGENTS.md. Relative paths resolve against the workspace.',
    },
    tasks: {
      type: 'array',
      items: { type: 'string' },
      description:
        'Task directories, each holding task.md plus any source files the work reads. Several tasks run in order as one stream: later tasks are refined with the harnesses and lessons adopted on earlier ones.',
    },
    k: {
      type: 'integer',
      description: 'Task-level recursion budget, a positive integer (default 1). Each step costs several DSH runs per task.',
    },
    mode: {
      type: 'string',
      enum: ['parallel', 'sequential', 'both'],
      description: 'Recursion mode (default parallel).',
    },
    out: {
      type: 'string',
      description: 'Output root for this run. Defaults to a timestamped directory under harness2-runs/ in the workspace.',
    },
    dry_run: {
      type: 'boolean',
      description: 'Return the plan without running any model session.',
    },
  },
}

const RUN_DIR_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['run_dir'],
  properties: {
    run_dir: {
      type: 'string',
      description: 'The run directory returned by harness2_improve.',
    },
  },
}

const IMPROVE_OUTPUT = {
  type: 'object',
  properties: {
    state: { type: 'string' },
    run_dir: { type: 'string' },
    pid: { type: 'number' },
    plan: {},
    message: { type: 'string' },
  },
}

const RESULT_OUTPUT = {
  type: 'object',
  properties: {
    state: { type: 'string' },
    run_dir: { type: 'string' },
    exit_code: {},
    adopted: {},
    summary: {},
    log_tail: { type: 'string' },
  },
}

const CANCEL_OUTPUT = {
  type: 'object',
  properties: {
    state: { type: 'string' },
    run_dir: { type: 'string' },
    message: { type: 'string' },
  },
}

function readText(file) {
  try {
    return readFileSync(file, 'utf8')
  } catch {
    return undefined
  }
}

function readJson(file) {
  const raw = readText(file)
  if (raw === undefined) return undefined
  try {
    return JSON.parse(raw)
  } catch {
    return undefined
  }
}

function readInt(file) {
  const raw = readText(file)
  const value = raw === undefined ? NaN : Number.parseInt(raw.trim(), 10)
  return Number.isInteger(value) ? value : undefined
}

function logTail(runDir, limit = 4000) {
  const log = readText(path.join(runDir, 'run.log')) || ''
  return log.length > limit ? log.slice(-limit) : log
}

function alive(pid) {
  if (!pid) return false
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    return error.code === 'EPERM'
  }
}

function stateOf(runDir) {
  const summary = readJson(path.join(runDir, 'summary.json'))
  const exitCode = readInt(path.join(runDir, 'exit_code'))
  if (summary) return { state: exitCode ? 'failed' : 'done', summary, exit_code: exitCode ?? 0 }
  if (exitCode !== undefined) return { state: 'failed', exit_code: exitCode }
  if (alive(readInt(path.join(runDir, 'pid')))) return { state: 'running' }
  if (existsSync(path.join(runDir, 'cancelled'))) return { state: 'cancelled' }
  if (existsSync(path.join(runDir, 'pid'))) return { state: 'failed' }
  return { state: 'unknown' }
}

function describe(value) {
  return textBlocks(JSON.stringify(value, null, 2))
}

function executableFile(file) {
  try {
    accessSync(file, constants.X_OK)
    return true
  } catch {
    return false
  }
}

/** The harness2-dsh entry point: configured, or found on PATH or in ~/.local/bin (uv tool installs). */
function resolveExecutable(configured) {
  if (configured) return configured
  if (process.env.HARNESS2_DSH_EXECUTABLE) return process.env.HARNESS2_DSH_EXECUTABLE
  const dirs = [...(process.env.PATH || '').split(path.delimiter), path.join(homedir(), '.local', 'bin')]
  for (const dir of dirs) {
    const file = dir && path.join(dir, 'harness2-dsh')
    if (file && executableFile(file)) return file
  }
  return undefined
}

/** The directory the calling session works in; relative tool paths resolve against it. */
function workspaceOf(exec) {
  return exec?.agent?.session?.header?.cwd || process.cwd()
}

/** The dsh launcher rollouts should use: this very installation when it can be found. */
function resolveDshBin(configured) {
  if (configured) return configured
  if (process.env.HARNESS2_DSH_BIN) return process.env.HARNESS2_DSH_BIN
  const self = process.argv[1]
  if (self && /(^|[\\/])(dsh|bin\.js)$/.test(self) && executableFile(self)) return self
  return 'dsh'
}

export function apply(ctx, config = {}) {
  const dshBin = resolveDshBin(config.dshBin)
  const modelArgs = config.provider && config.model ? ['--provider', config.provider, '--model', config.model] : []

  const improve = {
    name: 'harness2_improve',
    description:
      'Start a Harness2 harness-adaptation sweep: the base harness (AGENTS.md, .agents/skills, scripts, sub_agents) is refined against one or more tasks by running real DeepSeek Harness sessions, with Harness2 proposing, probing, and composing the edits. Returns immediately with a run directory; poll it with harness2_result. Costs several model sessions per task and per k.',
    parameters: IMPROVE_SCHEMA,
    output: { schema: IMPROVE_OUTPUT, render: (_args, value) => describe(value) },
    timeoutMs: 120000,
    async execute(args, exec) {
      const executable = resolveExecutable(config.executable)
      if (!executable) {
        throw new Error(
          'harness2-dsh is not installed. Install it with `uv tool install git+https://github.com/alt-coder/dsh-harness2`, or set config.executable in the harness2-tools row.',
        )
      }
      const workspace = workspaceOf(exec)
      const harness = path.resolve(workspace, String(args.harness))
      const tasks = (Array.isArray(args.tasks) ? args.tasks : []).map((t) => path.resolve(workspace, String(t)))
      if (!existsSync(path.join(harness, 'AGENTS.md'))) {
        throw new Error(`${harness} has no AGENTS.md; that file is the harness's always-on text`)
      }
      if (tasks.length === 0) throw new Error('tasks must name at least one task directory')
      for (const task of tasks) {
        if (!existsSync(path.join(task, 'task.md'))) {
          throw new Error(`${task} has no task.md; the task prompt lives there`)
        }
      }
      const k = Number.isInteger(args.k) && args.k > 0 ? args.k : 1
      const mode = args.mode || 'parallel'
      const outRoot = config.defaultOutDir ? path.resolve(workspace, config.defaultOutDir) : path.join(workspace, 'harness2-runs')
      const runDir =
        args.out && String(args.out).trim()
          ? path.resolve(workspace, String(args.out))
          : path.join(outRoot, `${path.basename(tasks[0])}-${Date.now()}`)
      if (alive(readInt(path.join(runDir, 'pid')))) {
        throw new Error(`${runDir} already has a running sweep; poll it or cancel it first`)
      }
      await mkdir(runDir, { recursive: true })

      const argv = ['run', '--harness', harness, '--out', runDir, '--k', String(k), '--mode', mode, ...modelArgs]
      for (const task of tasks) argv.push('--task', task)
      const env = { ...process.env, HARNESS2_DSH_BIN: dshBin }
      if (args.dry_run === true) {
        argv.push('--dry-run')
        const { stdout } = await execFileAsync(executable, argv, { timeout: 60000, env, signal: exec?.signal })
        return { state: 'planned', run_dir: runDir, plan: JSON.parse(stdout) }
      }

      // Clear markers from an earlier sweep in the same directory; the shell wrapper
      // below records the new exit code, which survives a DSH restart.
      for (const stale of ['exit_code', 'cancelled', 'summary.json']) {
        rmSync(path.join(runDir, stale), { force: true })
      }
      const fd = openSync(path.join(runDir, 'run.log'), 'a')
      let child
      try {
        child = spawn(
          '/bin/sh',
          ['-c', '"$0" "$@"; code=$?; echo "$code" > exit_code; exit "$code"', executable, ...argv],
          { cwd: runDir, env, detached: true, stdio: ['ignore', fd, fd] },
        )
        child.on('error', () => {
          try {
            writeFileSync(path.join(runDir, 'exit_code'), '127\n')
          } catch {}
        })
        child.unref()
      } finally {
        closeSync(fd)
      }
      if (child.pid) writeFileSync(path.join(runDir, 'pid'), `${child.pid}\n`)
      return {
        state: 'running',
        run_dir: runDir,
        pid: child.pid ?? 0,
        message: `Sweep is running. Poll harness2_result with run_dir=${runDir}; the log is ${path.join(runDir, 'run.log')}.`,
      }
    },
  }

  const result = {
    name: 'harness2_result',
    description:
      'Report one Harness2 sweep started by harness2_improve: running, done (with the adopted harness directory per task and mode, plus the summary), failed or cancelled (with the end of its log).',
    parameters: RUN_DIR_SCHEMA,
    output: { schema: RESULT_OUTPUT, render: (_args, value) => describe(value) },
    async execute(args, exec) {
      const runDir = path.resolve(workspaceOf(exec), String(args.run_dir))
      const status = stateOf(runDir)
      const value = { state: status.state, run_dir: runDir }
      if (status.exit_code !== undefined) value.exit_code = status.exit_code
      if (status.summary) {
        value.summary = status.summary
        value.adopted = status.summary.adopted || {}
      }
      if (status.state !== 'done') value.log_tail = logTail(runDir)
      return value
    },
  }

  const cancel = {
    name: 'harness2_cancel',
    description:
      'Stop a running Harness2 sweep started by harness2_improve, including the DSH sessions it launched. Rollouts already finished stay in the run directory.',
    parameters: RUN_DIR_SCHEMA,
    output: { schema: CANCEL_OUTPUT, render: (_args, value) => describe(value) },
    async execute(args, exec) {
      const runDir = path.resolve(workspaceOf(exec), String(args.run_dir))
      const pid = readInt(path.join(runDir, 'pid'))
      if (!alive(pid)) {
        return { state: stateOf(runDir).state, run_dir: runDir, message: 'No sweep is running there.' }
      }
      writeFileSync(path.join(runDir, 'cancelled'), `${new Date().toISOString()}\n`)
      // The sweep was spawned detached, so its pid leads a process group that also
      // holds harness2-dsh and every headless dsh session it started.
      try {
        process.kill(-pid, 'SIGTERM')
      } catch {
        process.kill(pid, 'SIGTERM')
      }
      return { state: 'cancelled', run_dir: runDir, message: `Sent SIGTERM to sweep process group ${pid}.` }
    },
  }

  ctx.effect(() => ctx.tools.register(improve))
  ctx.effect(() => ctx.tools.register(result))
  ctx.effect(() => ctx.tools.register(cancel))
}
