// Claude Code only: the /playbook-config settings menu and gate toasts.
//
// Interface, never rules. Every value is read and written through
// scripts/playbook_config.py (`show --json`, `set`), the same script the
// playbook-config skill uses in the other harnesses, so validation and the
// list of settings live in one place. The skill still answers when the model
// is asked in words.
//
// The menu is a numbered band above the prompt, not a pane: a pane needs the
// keyboard, which the terminal (VS Code's included) did not hand it, so the
// arrows went to the prompt. Here every entry is a number: the band's digit
// hotkey, and the same digit typed and sent while the menu is open, which this
// module answers itself (`prompt.submit`, dropped) without reaching the model.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { ConfigSetting, ConfigSnapshot, MenuScreen, PanelNotice } from './config_panel_state'

const COMMAND = 'playbook-config'
const EXIT = '0'
const DIGIT_RE = /^\s*(\d)\s*$/
// A gate's denial starts with its tag, sometimes behind the engine's own
// prefix; a command whose output merely mentions a tag is not a denial.
const GATE_TAG_RE = /^(?:PreToolUse:\S+ hook error: )?\[playbook:([\w-]+)\]/

// Change the tag when a stored value's fields change: an earlier version's
// value is then read as the initial one instead of a wrong shape.
const snapshot = atom({ plugin: 'engineering-playbook', key: 'snapshot' } as const, null, {
  shape: 'snapshot-1',
})
const notice = atom({ plugin: 'engineering-playbook', key: 'notice' } as const, null, {
  shape: 'notice-1',
})
const filterReport = atom({ plugin: 'engineering-playbook', key: 'filterReport' } as const, null, {
  shape: 'filter-report-1',
})
const screen = atom({ plugin: 'engineering-playbook', key: 'screen' } as const, null, {
  shape: 'screen-1',
})

// Status and Learn need the model: the session's configuration block lives in
// its context, and Learn is a skill that reads transcripts and writes CLAUDE.md.
// The menu hands those to the model in words; the skill takes it from there.
const ASK_STATUS = 'Show the engineering playbook session status (playbook-config, Status).'
const ASK_LEARN = 'Run the engineering-learn skill for this project (playbook-config, Learn).'

// What a numbered entry does, as data: the module may hand `$` only to named
// functions, so one switch (`run`) turns an action into calls.
type Action =
  | { kind: 'open'; key: string }
  | { kind: 'set'; key: string; value: string }
  | { kind: 'back' }
  | { kind: 'status' }
  | { kind: 'learn' }
  | { kind: 'diff' }
  | { kind: 'exit' }

type Entry = { digit: string; label: string; action: Action }

const MAIN: MenuScreen = { kind: 'main' }

function configScript($: EngineInterface): string {
  return `${$.plugin.root}/scripts/playbook_config.py`
}

function isSnapshot(value: unknown): value is ConfigSnapshot {
  return (
    typeof value === 'object' &&
    value !== null &&
    Array.isArray((value as { settings?: unknown }).settings)
  )
}

async function refresh($: EngineInterface): Promise<void> {
  const ran = await $.process.run(['python3', configScript($), 'show', '--json'])
  if (ran.exitCode !== 0) {
    await update($, notice, () => ({ text: ran.stderr.trim() || 'could not read the settings', isError: true }))
    return
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(ran.stdout)
  } catch {
    parsed = null
  }
  if (!isSnapshot(parsed)) {
    await update($, notice, () => ({ text: 'the settings script answered something unreadable', isError: true }))
    return
  }
  await update($, snapshot, () => parsed)
  const report = await $.process.run(['python3', `${$.plugin.root}/hooks/output_filter.py`, '--report'])
  await update($, filterReport, () => (report.exitCode === 0 ? report.stdout.trim() || null : null))
}

async function apply($: EngineInterface, key: string, value: string): Promise<void> {
  const ran = await $.process.run(['python3', configScript($), 'set', key, value])
  const said: PanelNotice =
    ran.exitCode === 0
      ? { text: ran.stdout.trim(), isError: false }
      : { text: ran.stderr.trim() || `could not set ${key}`, isError: true }
  await update($, notice, () => said)
  // The second line says when the change takes effect: the part worth a toast.
  const effect = said.text.split('\n').pop()?.trim()
  if (effect) {
    $.ui.toast(effect)
  }
  await refresh($)
  await update($, screen, () => MAIN)
}

async function close($: EngineInterface): Promise<void> {
  await update($, screen, () => null)
}

async function askModel($: EngineInterface, text: string): Promise<void> {
  await close($)
  await $.prompt.submit({ text })
}

function settingLine(setting: ConfigSetting): string {
  const label = setting.choices.find(choice => choice.value === setting.value)?.label ?? setting.value
  const marker = setting.value === setting.default ? '' : ` (default ${setting.default})`

  return `${setting.label}: ${label}${marker}`
}

// The numbered entries of the screen shown: one place decides what each digit
// does, for the band's buttons and for a digit typed and sent alike.
function entries(current: MenuScreen, settings: ConfigSetting[]): Entry[] {
  const back: Entry = { digit: EXIT, label: 'Back', action: { kind: 'back' } }
  if (current.kind === 'setting') {
    const setting = settings.find(one => one.key === current.key)
    if (!setting) {
      return [back]
    }
    const picks: Entry[] = setting.choices.slice(0, 9).map((choice, index) => ({
      digit: String(index + 1),
      label: choice.value === setting.value ? `${choice.label} (current)` : choice.label,
      action: { kind: 'set', key: setting.key, value: choice.value },
    }))

    return [...picks, back]
  }
  const rows: Entry[] = settings.slice(0, 6).map((setting, index) => ({
    digit: String(index + 1),
    label: settingLine(setting),
    action: { kind: 'open', key: setting.key },
  }))
  const after = rows.length + 1

  return [
    ...rows,
    { digit: String(after), label: 'Status', action: { kind: 'status' } },
    { digit: String(after + 1), label: 'Learn', action: { kind: 'learn' } },
    { digit: String(after + 2), label: 'Diff panel', action: { kind: 'diff' } },
    { digit: EXIT, label: 'Exit', action: { kind: 'exit' } },
  ]
}

async function run($: EngineInterface, action: Action): Promise<void> {
  switch (action.kind) {
    case 'open':
      await update($, screen, (): MenuScreen => ({ kind: 'setting', key: action.key }))
      return
    case 'set':
      await apply($, action.key, action.value)
      return
    case 'back':
      await update($, screen, () => MAIN)
      return
    case 'status':
      await askModel($, ASK_STATUS)
      return
    case 'learn':
      await askModel($, ASK_LEARN)
      return
    case 'diff':
      await $.command.run({ command: 'diff' })
      return
    case 'exit':
      await close($)
      return
  }
}

async function entryFor($: EngineInterface, digit: string): Promise<Entry | undefined> {
  const current = await read($, screen)
  if (current === null) {
    return undefined
  }
  const loaded = await read($, snapshot)

  return entries(current, loaded?.settings ?? []).find(one => one.digit === digit)
}

async function choose($: EngineInterface, digit: string): Promise<void> {
  const entry = await entryFor($, digit)
  if (entry) {
    await run($, entry.action)
  }
}

export const register: Register = on => {
  // A bare /playbook-config the person typed opens the menu instead of running
  // the skill. The skill's command may be namespaced
  // (`engineering-playbook:playbook-config`). With arguments (`/playbook-config
  // prose low`) or from anywhere but the composer it is a request for the
  // skill, which handles it directly; taking it here dropped the request.
  on('command.run', async ($, e, next) => {
    if (e.command !== COMMAND && !e.command.endsWith(`:${COMMAND}`)) {
      return next(e)
    }
    if (e.args.trim() !== '' || e.origin?.kind !== 'composer') {
      return next(e)
    }
    await refresh($)
    await update($, notice, () => null)
    await update($, screen, () => MAIN)

    return { text: 'Playbook settings are above the prompt: type a number and Enter (0 exits).' }
  }).catch(($, e, next) => next(e)) // the menu failed: the skill's menu runs instead

  // While the menu is open, a lone digit sent from the prompt picks its entry
  // and never reaches the model; Status and Learn become the prompt itself (a
  // prompt.submit hook may not submit another prompt). Anything else sent
  // closes the menu and goes on as typed, so a later digit meant for the model
  // (an answer to its numbered question) is never taken by a forgotten menu.
  on('prompt.submit', async ($, e, next) => {
    if ((await read($, screen)) === null) {
      return next(e)
    }
    const digit = DIGIT_RE.exec(e.text)?.[1]
    if (digit === undefined || e.command !== undefined) {
      await close($)

      return next(e)
    }
    const entry = await entryFor($, digit)
    if (!entry) {
      return { drop: `Playbook settings: no entry ${digit} on this screen.` }
    }
    if (entry.action.kind === 'status' || entry.action.kind === 'learn') {
      await close($)

      return next({ ...e, text: entry.action.kind === 'status' ? ASK_STATUS : ASK_LEARN })
    }
    await run($, entry.action)
    const last = await read($, notice)
    const isSet = entry.action.kind === 'set'

    return { drop: (isSet && last?.text.split('\n').pop()) || `Playbook settings: ${entry.label}` }
  }).catch(($, e, next) => next(e)) // never lose a prompt to the menu

  // A gate denial is shown to the person too, not only to the model.
  on('tool.call', async ($, e, next) => {
    const ran = await next(e)
    const said = typeof ran.deny === 'string' ? ran.deny : ran.isError === true ? ran.text ?? '' : ''
    const tag = GATE_TAG_RE.exec(said)
    if (tag) {
      $.ui.toast(`playbook ${tag[1]}: blocked ${e.tool}`)
    }

    return ran
  }).catch(($, e, next) => next(e)) // a toast is never a reason to change the call

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const current = await read($, screen)
    if (current === null || e.props.hasSurvey) {
      return next(e)
    }
    const { Box, Text, Button } = $.ui.resolve(e)
    const loaded = await read($, snapshot)
    const last = await read($, notice)
    const report = await read($, filterReport)
    const settings = loaded?.settings ?? []
    const shown = current.kind === 'setting' ? settings.find(one => one.key === current.key) : undefined

    return (
      <Box flexDirection="column">
        <Text bold>
          {shown ? shown.label : 'Engineering Playbook settings'}{' '}
          <Text dimColor>type a number and Enter, {EXIT} {shown ? 'goes back' : 'exits'}</Text>
        </Text>
        {shown && <Text dimColor>{shown.help}</Text>}
        {entries(current, settings).map(entry => (
          <Button
            key={`entry-${entry.digit}`}
            plain
            hotkey={entry.digit}
            label={entry.label}
            onPress={() => void choose($, entry.digit)}
          />
        ))}
        {!shown && report !== null && <Text dimColor>{report}</Text>}
        {last !== null && (
          <Text color={last.isError ? 'red' : undefined} dimColor={!last.isError}>
            {last.text}
          </Text>
        )}
      </Box>
    )
  })
}
