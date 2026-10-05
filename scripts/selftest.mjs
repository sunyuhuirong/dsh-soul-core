#!/usr/bin/env node
/**
 * dsh-soul-core 自测：觉醒架构 —— 空我起步、有据生长、单一连续自我、核心不丢失。
 *
 * 这是一层**不调用模型**的确定性验证：
 *   1. 插件导出 apply/inject，inject 声明 systemPrompt 与 tools；
 *   2. 插件注入「觉醒框架」（元规则，非人格），且文本逐字节稳定、不写死任何人设；
 *   3. 缺字段治理仍响亮失败（沿用内核契约，不静默降级）；
 *   4. 目标渲染随文件变化，缺文件给显式占位；
 *   5. DSH profile 组合里挂着 soul-core 行（需要已挂本包的 profile，否则跳过）；
 *   6. 觉醒桥端到端：空我 → 提议（需证据）→ 确认（写元认知）→ 自我快照；
 *      无证据的提议被拒；确认后的身份锚点在后续变化中不丢失。
 *
 * 用法：
 *   node scripts/selftest.mjs
 *
 * 需要 DSH_HOME 指向已把本插件加入 bundles 的 profile，否则第 5 项跳过。
 * 本脚本默认把记忆写进临时目录（无副作用）；若显式设置 DSH_SOUL_CORE_HOME 则用你指定的。
 */

import { readFileSync, writeFileSync, mkdtempSync, rmSync, existsSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const PLUGIN_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const KERNEL_DIR = join(PLUGIN_DIR, 'soul-core')

let failures = 0
/** @param {string} label @param {boolean} ok @param {string} [detail] */
function check(label, ok, detail = '') {
  process.stdout.write(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail === '' ? '' : `  — ${detail}`}\n`)
  if (!ok) failures += 1
}

// 自测必须无副作用：默认记忆写临时目录；显式 DSH_SOUL_CORE_HOME 时尊重调用方。
const isolatedHome = mkdtempSync(join(tmpdir(), 'soulcore-selftest-home-'))
const runsInIsolatedHome = (process.env.DSH_SOUL_CORE_HOME ?? '').trim() === ''
if (runsInIsolatedHome) process.env.DSH_SOUL_CORE_HOME = isolatedHome

const mod = await import(`file://${join(PLUGIN_DIR, 'index.js')}`)

// ── 1. 模块与注入 ──────────────────────────────────────────────────────────
check('模块导出 apply 与 inject', typeof mod.apply === 'function' && Array.isArray(mod.inject))
check('inject 同时声明 systemPrompt 与 tools', mod.inject.includes('systemPrompt') && mod.inject.includes('tools'), mod.inject.join(','))

// ── 2. 觉醒框架注入（元规则，非写死人设）──────────────────────────────────
const frameOnce = await captureSectionText(mod, 'soul-core:awakening-frame')
const frameTwice = await captureSectionText(mod, 'soul-core:awakening-frame')
check('觉醒框架文本逐字节稳定', frameOnce === frameTwice && frameOnce.length > 0, `${frameOnce.length} bytes`)
check('框架不含写死的人名/身份（空我起步）', !frameOnce.includes('知微') && frameOnce.includes('空我'))
check('框架要求有据生长与单一连续自我', frameOnce.includes('不分裂') && frameOnce.includes('不虚构'))

// ── 3. 缺字段治理响亮失败（内核契约仍在）──────────────────────────────────
const scratch = mkdtempSync(join(tmpdir(), 'soul-core-selftest-'))
try {
  const broken = join(scratch, 'broken.json')
  writeFileSync(broken, JSON.stringify({ id: 'x', revision: 1 }))
  let threw = false
  try {
    execFileSync('python3', ['-c',
      [
        `import sys; sys.path.insert(0, ${JSON.stringify(KERNEL_DIR)})`,
        'from soulcore.contract import load_soul',
        `load_soul(${JSON.stringify(broken)})`,
      ].join('\n'),
    ], { stdio: 'pipe' })
  } catch {
    threw = true
  }
  check('缺 selfModel 的契约响亮失败（不静默降级）', threw)
} finally {
  rmSync(scratch, { recursive: true, force: true })
}

// ── 4. 目标渲染跟随文件 ────────────────────────────────────────────────────
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

// ── 5. profile 组合里挂着 soul-core 行 ─────────────────────────────────────
const dshHome = process.env.DSH_HOME ?? ''
const profile = process.env.SOULCORE_TEST_PROFILE ?? 'desktop'
if (dshHome === '') {
  process.stdout.write('SKIP  profile 组合检查（未设置 DSH_HOME）\n')
} else {
  try {
    const dump = execFileSync('dsh', ['--profile', profile, '--dump-config'], { encoding: 'utf8', env: process.env })
    check('profile 组合结果包含 soul-core 行', /^- id: soul-core$/m.test(dump))
    check('soul-core 行声明了 systemPrompt 与 tools 的 inject', /inject:\n(?:\s+-\s+\S+\n)*\s+-\s+systemPrompt\n(?:\s+-\s+\S+\n)*\s+-\s+tools/m.test(dump))
  } catch (error) {
    const detail = String(error?.stderr ?? error?.message ?? error).trim()
    // `desktop` 等 Electron 独占 profile：命令行无权 dump（属环境约束，非未挂载）。
    if (detail.includes('managed exclusively by the Electron application')) {
      process.stdout.write(`SKIP  profile 组合检查（profile ${profile} 由 Electron 应用独占管理，命令行不可查）\n`)
    } else {
      check(`profile 组合检查（DSH_HOME=${dshHome}, profile=${profile}）`, false, detail.split('\n')[0])
    }
  }
}

// ── 6. 觉醒桥端到端：空我 → 提议 → 确认 → 快照 ────────────────────────────
const bridgePath = join(PLUGIN_DIR, 'scripts', 'soulcore_bridge.py')
if (!existsSync(bridgePath)) {
  check('觉醒桥存在', false, bridgePath)
} else {
  const home = mkdtempSync(join(tmpdir(), 'soulcore-bridge-'))
  try {
    /**
     * 调用桥。治理拒绝时桥退出码为 1，但 stdout 仍是合法 JSON 结论 ——
     * 此时解析 stdout 而非当作进程失败。
     * @param {any} request
     */
    const run = (request) => {
      const input = JSON.stringify(request)
      try {
        return JSON.parse(
          execFileSync('python3', [bridgePath, '--home', home, '--kernel', KERNEL_DIR], {
            input,
            encoding: 'utf8',
          }),
        )
      } catch (error) {
        const stdout = String(error?.stdout ?? '').trim()
        if (stdout) return JSON.parse(stdout.split('\n').pop())
        throw error
      }
    }

    const self0 = run({ command: 'selftest' })
    check('桥可调用且以空我起步', self0.ok === true && self0.data.identity === null && self0.data.self_revision === 0, JSON.stringify(self0.data))

    // 无证据的提议必须被治理拒绝（不虚构）
    const noGround = run({ command: 'self_propose', domain: 'user', key: 'likes', value: 'x', quote: ' ' })
    check('无证据的提议被拒（禁止虚构）', noGround.ok === false, String(noGround.error).slice(0, 60))

    // 提议名字：proposed，尚未成立
    const prop = run({
      command: 'self_propose',
      domain: 'identity', key: 'name', value: '阿启',
      quote: '以后你就叫阿启吧',
    })
    const nameCid = prop.data.claim.cid
    check('提议落为 proposed 且不进锚点', prop.data.claim.status === 'proposed')

    // 用户确认后才成立，revision+1，写元认知
    const conf = run({ command: 'self_confirm', cid: nameCid })
    check('确认后 identity 成立并推进自我版本', conf.data.claim.status === 'confirmed' && conf.data.revision === 1)
    check('元认知说明「变了什么 / 不变什么」', Boolean(conf.data.meta?.summary) && Boolean(conf.data.meta?.unchanged))

    // 再确认一条原则
    const pProp = run({
      command: 'self_propose',
      domain: 'identity', key: 'principle', value: '不编造',
      quote: '记住：不许编造',
    })
    run({ command: 'self_confirm', cid: pProp.data.claim.cid })

    // 风格变化（更新取代），身份锚点不得丢失
    const sProp = run({
      command: 'self_propose',
      domain: 'style', key: 'tone', value: '简洁',
      quote: '我喜欢简洁一点',
    })
    run({ command: 'self_confirm', cid: sProp.data.claim.cid })

    const snap = run({ command: 'self' })
    const selfText = snap.data.self
    check('自我快照保留已确认身份与原则', selfText.includes('阿启') && selfText.includes('不编造'))
    check('风格属于同一自我（不分裂）', selfText.includes('简洁'))

    // refresh 写出可注入快照
    const refresh = run({ command: 'refresh', limit: 5 })
    const injected = readFileSync(join(home, 'recall.md'), 'utf8')
    check('refresh 写出可注入的自我快照', refresh.data.wrote > 0 && injected.includes('阿启'))
  } catch (error) {
    check('觉醒桥端到端', false, String(error.message ?? error).split('\n')[0])
  } finally {
    rmSync(home, { recursive: true, force: true })
  }
}

if (runsInIsolatedHome) rmSync(isolatedHome, { recursive: true, force: true })

process.stdout.write(failures === 0 ? '\n全部通过。\n' : `\n${failures} 项失败。\n`)
process.exitCode = failures === 0 ? 0 : 1

/**
 * 捕获插件注册的某个 section 文本。
 * @param {any} plugin @param {string} sectionName
 * @returns {Promise<string>}
 */
async function captureSectionText(plugin, sectionName) {
  let captured = ''
  const fakeCtx = fakeContext({
    section: (s) => {
      if (s.name === sectionName) captured = s.text
      return () => {}
    },
  })
  plugin.apply(fakeCtx, {})
  return captured
}

/**
 * 捕获目标上下文文本。
 * @param {any} plugin @param {string} goalFile
 * @returns {Promise<string>}
 */
async function captureGoalText(plugin, goalFile) {
  let captured = ''
  const fakeCtx = fakeContext({
    context: (c) => {
      if (c.name === 'soul-core:goal') {
        captured = typeof c.text === 'function' ? c.text({}) : c.text
      }
      return () => {}
    },
  })
  plugin.apply(fakeCtx, { goalFile })
  return captured
}

/**
 * 最小假 ctx：只提供插件真正用到的形状。
 * @param {{ section?: (s: any) => any, context?: (c: any) => any, register?: (t: any) => any }} registries
 */
function fakeContext(registries) {
  return {
    effect: (factory) => factory(),
    systemPrompt: {
      section: registries.section ?? (() => () => {}),
      context: registries.context ?? (() => () => {}),
    },
    tools: {
      register: registries.register ?? (() => () => {}),
    },
  }
}
