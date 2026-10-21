// Plugin tests with a stub harness2-dsh: no dsh, Python, or model needed.
// Set DSH_TOOLS to a @deepseek-ai/dsh-tools lib/index.js (or install the package)
// to also validate every schema with DSH's own enforced JSON Schema subset.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { chmodSync, mkdirSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import * as plugin from '../index.js'

const root = mkdtempSync(path.join(tmpdir(), 'dsh-harness2-test-'))
const stub = path.join(root, 'harness2-dsh')
writeFileSync(
  stub,
  `#!/bin/sh
for a in "$@"; do [ "$a" = "--dry-run" ] && { printf '{"argv":"%s"}' "$*"; exit 0; }; done
echo "argv: $*"
sleep "\${STUB_SLEEP:-1}"
echo '{"adopted":{"t1":{"agg":"/x"}},"done":1,"failed":0}' > summary.json
`,
)
chmodSync(stub, 0o755)
for (const dir of ['h', 't1', 't2']) mkdirSync(path.join(root, dir))
writeFileSync(path.join(root, 'h', 'AGENTS.md'), '# rules\n')
writeFileSync(path.join(root, 't1', 'task.md'), 'do x\n')
writeFileSync(path.join(root, 't2', 'task.md'), 'do y\n')

const exec = { signal: AbortSignal.timeout(30000), agent: { session: { header: { cwd: root } } } }
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

function load(config = { executable: stub, provider: 'deepseek-official', model: 'deepseek-flash' }) {
  const tools = {}
  plugin.apply({ effect: (fn) => fn(), tools: { register: (t) => { tools[t.name] = t; return () => {} } } }, config)
  return tools
}

async function dshTools() {
  try {
    return await import(process.env.DSH_TOOLS || '@deepseek-ai/dsh-tools')
  } catch {
    return undefined
  }
}

test('registers three tools with schemas DSH accepts', async () => {
  const tools = load()
  assert.deepEqual(Object.keys(tools).sort(), ['harness2_cancel', 'harness2_improve', 'harness2_result'])
  const walk = (node, at) => {
    for (const [key, child] of Object.entries(node.properties || {})) {
      assert.notEqual(typeof child.required, 'boolean', `${at}.${key}: required must be an object-level array`)
      walk(child, `${at}.${key}`)
    }
  }
  const validator = await dshTools()
  for (const tool of Object.values(tools)) {
    walk(tool.parameters, tool.name)
    if (validator) {
      validator.assertObjectJsonSchema(tool.parameters)
      validator.assertSupportedJsonSchema(tool.output.schema)
    }
  }
})

test('dry run resolves relative paths against the session workspace', async () => {
  const tools = load()
  const plan = await tools.harness2_improve.execute({ harness: 'h', tasks: ['t1', 't2'], dry_run: true }, exec)
  assert.equal(plan.state, 'planned')
  assert.ok(plan.run_dir.startsWith(path.join(root, 'harness2-runs')))
  assert.match(plan.plan.argv, new RegExp(`--task ${root}/t1 --task ${root}/t2`))
  assert.match(plan.plan.argv, /--provider deepseek-official --model deepseek-flash/)
})

test('a sweep runs, refuses a second start, and reports adopted harnesses after a restart', async () => {
  const tools = load()
  const run = await tools.harness2_improve.execute({ harness: 'h', tasks: ['t1'], out: 'r1' }, exec)
  assert.equal(run.state, 'running')
  assert.equal((await tools.harness2_result.execute({ run_dir: 'r1' }, exec)).state, 'running')
  await assert.rejects(tools.harness2_improve.execute({ harness: 'h', tasks: ['t1'], out: 'r1' }, exec), /already has a running sweep/)
  await sleep(2000)
  const fresh = load() // no in-memory state, as after a dsh restart
  const done = await fresh.harness2_result.execute({ run_dir: 'r1' }, exec)
  assert.equal(done.state, 'done')
  assert.equal(done.exit_code, 0)
  assert.deepEqual(done.adopted, { t1: { agg: '/x' } })
})

test('cancel stops the whole sweep process group', async () => {
  process.env.STUB_SLEEP = '30'
  try {
    const tools = load()
    const run = await tools.harness2_improve.execute({ harness: 'h', tasks: ['t1'], out: 'r2' }, exec)
    await sleep(300)
    assert.equal((await tools.harness2_cancel.execute({ run_dir: 'r2' }, exec)).state, 'cancelled')
    await sleep(500)
    assert.equal((await tools.harness2_result.execute({ run_dir: 'r2' }, exec)).state, 'cancelled')
    assert.throws(() => process.kill(run.pid, 0))
  } finally {
    delete process.env.STUB_SLEEP
  }
})

test('input errors name the missing file', async () => {
  const tools = load()
  await assert.rejects(tools.harness2_improve.execute({ harness: 't1', tasks: ['t1'] }, exec), /no AGENTS\.md/)
  await assert.rejects(tools.harness2_improve.execute({ harness: 'h', tasks: ['h'] }, exec), /no task\.md/)
})

test('without harness2-dsh installed, the call explains how to install it', async () => {
  const saved = { PATH: process.env.PATH, HOME: process.env.HOME, X: process.env.HARNESS2_DSH_EXECUTABLE }
  process.env.PATH = path.join(root, 'empty-path')
  process.env.HOME = root
  delete process.env.HARNESS2_DSH_EXECUTABLE
  try {
    const tools = load({})
    await assert.rejects(
      tools.harness2_improve.execute({ harness: 'h', tasks: ['t1'], dry_run: true }, exec),
      /harness2-dsh is not installed.*uv tool install/,
    )
  } finally {
    process.env.PATH = saved.PATH
    process.env.HOME = saved.HOME
    if (saved.X !== undefined) process.env.HARNESS2_DSH_EXECUTABLE = saved.X
  }
})
