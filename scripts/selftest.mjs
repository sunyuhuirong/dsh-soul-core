#!/usr/bin/env node
/**
 * dsh-soul-core 自测：断言「身份不可变 / 目标每轮可读 / 契约可核验 / 行可挂载」。
 *
 * 这是一层**不调用模型**的确定性验证：
 *   1. 盘上身份契约与 soul-core 内核认定的 contentHash 一致（治理闸门必须通过）；
 *   2. 身份渲染是纯函数 —— 同一份契约渲染两次逐字节相同（长会话里不可能自己漂移）；
 *   3. 缺字段的身份契约必须响亮失败，不允许静默降级成「没有自我核心的 agent」；
 *   4. 目标渲染（身份+目标注入用的同一个渲染器）随文件内容变化，并对缺文件给出显式占位；
 *   5. DSH profile 组合结果里确实挂着 soul-core 行，且 inject 同时包含 systemPrompt 与 tools。
 *
 * 用法（在仓库根目录）：
 *   node dsh-soul-core-plugin/scripts/selftest.mjs
 *
 * 需要 DSH_HOME 指向一个已把本插件加入 bundles 的 profile，否则第 5 项跳过。
 * 模型侧的长会话行为验证见 README（那一层需要真实凭据）。
 */

import { readFileSync, writeFileSync, mkdtempSync, rmSync, existsSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const PLUGIN_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '..')
// 内核已捆绑进插件包（soul-core/ 子目录），不再依赖外层同级的 soul-core。
const KERNEL_DIR = join(PLUGIN_DIR, 'soul-core')

let failures = 0
/** @param {string} label @param {boolean} ok @param {string} [detail] */
function check(label, ok, detail = '') {
  process.stdout.write(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail === '' ? '' : `  — ${detail}`}\n`)
  if (!ok) failures += 1
}

// ── 1. 契约可核验：盘上 spec 必须通过 soul-core 内核的治理闸门 ──────────────────
const identityFile = join(PLUGIN_DIR, 'soul-core', 'soul', 'specs', 'ip-analyst.soul.json')
let contentHash = ''
try {
  const out = execFileSync(
    'python3',
    [
      '-c',
      [
        'import json,sys',
        `sys.path.insert(0, ${JSON.stringify(KERNEL_DIR)})`,
        'from soulcore.contract import load_soul',
        `s = load_soul(${JSON.stringify(identityFile)})`,
        'print(s.content_hash)',
      ].join('\n'),
    ],
    { encoding: 'utf8' },
  ).trim()
  contentHash = out
  check('身份契约通过 soul-core 治理闸门（load_soul）', out.startsWith('sha256:'), out)
} catch (error) {
  check('身份契约通过 soul-core 治理闸门（load_soul）', false, String(error.message ?? error).split('\n')[0])
}

// ── 2. 身份渲染是纯函数：同一契约两次渲染逐字节相同 ─────────────────────────────
// 自测必须**无副作用**：插件默认把记忆写在 <插件>/soul/memory，
// 若不隔离，一次自测就会往仓库里留下运行时数据。
// 仅当调用方没有显式指定数据目录时，才把 HOME 钉到临时目录（并负责删除）。
const isolatedHome = mkdtempSync(join(tmpdir(), 'soulcore-selftest-home-'))
const runsInIsolatedHome = (process.env.DSH_SOUL_CORE_HOME ?? '').trim() === ''
if (runsInIsolatedHome) process.env.DSH_SOUL_CORE_HOME = isolatedHome

const mod = await import(`file://${join(PLUGIN_DIR, 'index.js')}`)
check('模块导出 apply 与 inject', typeof mod.apply === 'function' && Array.isArray(mod.inject))
check('inject 同时声明 systemPrompt 与 tools', mod.inject.includes('systemPrompt') && mod.inject.includes('tools'), mod.inject.join(','))

const specText = readFileSync(identityFile, 'utf8')
const spec = JSON.parse(specText)
const renderOnce = await captureIdentityText(mod)
const renderTwice = await captureIdentityText(mod)
check(
  '身份文本在同一进程内逐字节稳定（不可被会话内容改写）',
  renderOnce === renderTwice && renderOnce.includes(spec.id) && renderOnce.includes(contentHash),
  `${renderOnce.length} bytes`,
)
check('身份文本包含全部 locked 核心主张', spec.identityCore.coreClaims.every((c) => renderOnce.includes(c.id)))
check('身份文本包含全部红线', spec.identityCore.redLines.every((r) => renderOnce.includes(r.id)))

// ── 3. 缺字段的身份契约必须响亮失败 ──────────────────────────────────────────
const scratch = mkdtempSync(join(tmpdir(), 'soul-core-selftest-'))
try {
  const broken = join(scratch, 'broken.json')
  writeFileSync(broken, JSON.stringify({ id: 'x', revision: 1 }))
  let threw = false
  try {
    await captureIdentityText(mod, broken)
  } catch {
    threw = true
  }
  check('缺 selfModel 的身份契约响亮失败（不静默降级）', threw)
} finally {
  rmSync(scratch, { recursive: true, force: true })
}

// ── 4. 目标渲染跟随文件内容 ─────────────────────────────────────────────────
const goalTmp = mkdtempSync(join(tmpdir(), 'soul-core-goal-'))
try {
  const goalFile = join(goalTmp, 'goal.json')
  writeFileSync(goalFile, JSON.stringify({ objective: '目标A', status: 'in_progress' }))
  const a = await captureGoalText(mod, goalFile)
  writeFileSync(goalFile, JSON.stringify({ objective: '目标B', status: 'done' }))
  const b = await captureGoalText(mod, goalFile)
  rmSync(goalFile)
  const c = await captureGoalText(mod, goalFile)
  check('目标文本随文件变化（每轮重新读取）', a.includes('目标A') && b.includes('目标B') && a !== b)
  check('目标文件缺失时给出显式占位而非静默为空', c.includes('未设定') && c.length > 0)
} finally {
  rmSync(goalTmp, { recursive: true, force: true })
}

// ── 5. profile 组合结果里挂着 soul-core 行 ───────────────────────────────────
const dshHome = process.env.DSH_HOME ?? ''
const profile = process.env.SOULCORE_TEST_PROFILE ?? 'soulcore'
if (dshHome === '') {
  process.stdout.write('SKIP  profile 组合检查（未设置 DSH_HOME）\n')
} else {
  try {
    const dump = execFileSync('dsh', ['--profile', profile, '--dump-config'], { encoding: 'utf8', env: process.env })
    check('profile 组合结果包含 soul-core 行', /^- id: soul-core$/m.test(dump))
    check('soul-core 行声明了 systemPrompt 与 tools 的 inject', /inject:\n(?:\s+-\s+\S+\n)*\s+-\s+systemPrompt\n(?:\s+-\s+\S+\n)*\s+-\s+tools/m.test(dump))
  } catch (error) {
    check(`profile 组合检查（DSH_HOME=${dshHome}, profile=${profile}）`, false, String(error.message ?? error).split('\n')[0])
  }
}

// ── 6. 记忆桥：真内核、真落盘、可跨进程召回 ────────────────────────────────
const bridgePath = join(PLUGIN_DIR, 'scripts', 'soulcore_bridge.py')
if (!existsSync(bridgePath)) {
  check('记忆桥存在', false, bridgePath)
} else {
  const home = mkdtempSync(join(tmpdir(), 'soulcore-bridge-'))
  try {
    const run = (request) =>
      JSON.parse(
        execFileSync('python3', [bridgePath, '--home', home, '--kernel', KERNEL_DIR], {
          input: JSON.stringify(request),
          encoding: 'utf8',
        }),
      )
    const selftest = run({ command: 'selftest' })
    check('记忆桥可调用（数据目录可写）', selftest.ok === true, JSON.stringify(selftest.data ?? selftest.error))
    check(
      '记忆桥加载的是真实身份契约',
      selftest.data?.content_hash === contentHash,
      selftest.data?.content_hash,
    )

    const first = run({ command: 'remember', name: '张三', role: '客户', evidence: '客户张三提到申报截止日' })
    check('首次记住一个人落为待定（无为）', first.data?.person?.state === 'emerging', first.data?.note)
    const second = run({ command: 'remember', name: '张三', evidence: '张三确认了截止日' })
    check('第二次相遇转正为 active', second.data?.person?.state === 'active' && second.data?.person?.encounters === 2)
    check('同一人不产生第二个档案', second.data?.pid === first.data?.pid)

    const event = run({ command: 'event', summary: '张三提交了优先权文件', people: ['张三'] })
    check('事件挂到人上', event.data?.event?.participants?.[0] === first.data?.pid)

    const status = run({ command: 'status' })
    check('账本状态可读（阴阳配比）', typeof status.data?.yin_yang?.kept === 'number', JSON.stringify(status.data?.yin_yang))

    const refresh = run({ command: 'refresh', limit: 5 })
    const injected = readFileSync(join(home, 'recall.md'), 'utf8')
    check('refresh 写出可注入的简报', refresh.data?.wrote > 0 && injected.includes('张三'))
    check('简报带阴阳账目', injected.includes('账目'))

    // 新进程重新读盘 —— 记忆必须比进程活得久
    const again = run({ command: 'recall', query: '张三', k: 3 })
    check('跨进程仍可召回（记忆已落盘）', again.data?.hits?.length >= 1, JSON.stringify(again.data?.hits))

    const forget = run({ command: 'forget', ref: first.data.pid, reason: '自测' })
    check('显式遗忘是归档不是删除', forget.data?.forgotten === true)
    const afterForget = run({ command: 'status' })
    check(
      '遗忘后仍可在册（可达性下降，非抹除）',
      afterForget.data?.yin_yang?.shed >= 1,
      JSON.stringify(afterForget.data?.yin_yang),
    )
  } catch (error) {
    check('记忆桥端到端', false, String(error.message ?? error).split('\n')[0])
  } finally {
    rmSync(home, { recursive: true, force: true })
  }
}

// 清理自测用的隔离数据目录（仅当它是本脚本自己创建的时候）
if (runsInIsolatedHome) rmSync(isolatedHome, { recursive: true, force: true })

process.stdout.write(failures === 0 ? '\n全部通过。\n' : `\n${failures} 项失败。\n`)
process.exitCode = failures === 0 ? 0 : 1

/**
 * 用一个假 ctx 捕获插件注册的身份段落文本。
 * @param {any} plugin @param {string} [identityOverride]
 * @returns {Promise<string>}
 */
async function captureIdentityText(plugin, identityOverride) {
  let captured = ''
  const fakeCtx = fakeContext({
    section: (s) => {
      if (s.name === 'soul-core:identity') captured = s.text
      return () => {}
    },
    context: () => () => {},
    register: () => () => {},
  })
  plugin.apply(fakeCtx, identityOverride === undefined ? {} : { identityFile: identityOverride })
  return captured
}

/**
 * 用一个假 ctx 捕获插件注册的目标上下文文本（provider 形式，注册后立即求值一次）。
 * @param {any} plugin @param {string} goalFile
 * @returns {Promise<string>}
 */
async function captureGoalText(plugin, goalFile) {
  let captured = ''
  const fakeCtx = fakeContext({
    section: () => () => {},
    // 必须按注册名挑：插件还注册了 soul-core:memory，抓到它就是空串
    context: (c) => {
      if (c.name === 'soul-core:goal') {
        captured = typeof c.text === 'function' ? c.text({}) : c.text
      }
      return () => {}
    },
    register: () => () => {},
  })
  plugin.apply(fakeCtx, { goalFile })
  return captured
}

/**
 * 最小假 ctx：只提供插件真正用到的形状（effect / systemPrompt / tools）。
 * @param {{ section: (s: any) => any, context: (c: any) => any, register: (t: any) => any }} registries
 */
function fakeContext(registries) {
  return {
    effect: (factory) => factory(),
    systemPrompt: {
      section: registries.section,
      context: registries.context,
    },
    tools: {
      register: registries.register,
    },
  }
}
