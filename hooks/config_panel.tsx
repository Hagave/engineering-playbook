// Claude Code only: the /playbook-config settings menu and gate toasts.
//
// Interface, never rules. Every value is read and written through
// scripts/playbook_config.py (`show --json`, `set`), the same script the
// playbook-config skill uses in the other harnesses, so validation and the
// list of settings live in one place. The skill still answers when the model
// is asked in words.
//
// Every string the person reads comes from that script too (`ui`, the labels),
// in the language the `language` setting resolves to: the model's "reply in
// the user's language" never reaches text this module draws.
//
// The menu is a numbered band above the prompt, not a pane: a pane needs the
// keyboard, which the terminal (VS Code's included) did not hand it, so the
// arrows went to the prompt. Here every entry is a number: the band's digit
// hotkey, and the same digit typed and sent while the menu is open, which this
// module answers itself (`prompt.submit`, dropped) without reaching the model.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { ConfigSetting, ConfigSnapshot, MenuScreen, PanelNotice, PanelText } from './config_panel_state'
import { panelPrompt, registerTemporaryData } from './temporary_data'

const COMMAND = 'playbook-config'
const EXIT = '0'
const DIGIT_RE = /^\s*(\d)\s*$/
const ENGLISH = 'en'
// Setting rows on the main screen (a group of settings is one row): with
// Status, Learn, red-lab-playbook and Diff panel after them, five fill the
// digits 1-9.
const MAX_SETTING_ROWS = 5
// A gate's denial starts with its tag, sometimes behind the engine's own
// prefix; a command whose output merely mentions a tag is not a denial.
const GATE_TAG_RE = /^(?:PreToolUse:\S+ hook error: )?\[playbook:([\w-]+)\]/

// Change the tag when a stored value's fields change: an earlier version's
// value is then read as the initial one instead of a wrong shape.
const snapshot = atom({ plugin: 'engineering-playbook', key: 'snapshot' } as const, null, {
  shape: 'snapshot-4',
})
const notice = atom({ plugin: 'engineering-playbook', key: 'notice' } as const, null, {
  shape: 'notice-1',
})
const filterReport = atom({ plugin: 'engineering-playbook', key: 'filterReport' } as const, null, {
  shape: 'filter-report-1',
})
// temporary_data.tsx reads the same value: change the shape in both files.
const screen = atom({ plugin: 'engineering-playbook', key: 'screen' } as const, null, {
  shape: 'screen-3',
})

// Status, Learn and the red-lab-playbook steps need the model: the session's
// configuration block lives in its context, Learn is a skill that reads
// transcripts and writes CLAUDE.md, and a lab step asks which service, port or
// endpoint to scan and runs for minutes with its output in view (the setup
// alone downloads about 1.2 GB, past this module's ten-minute process limit).
// The menu hands those to the model in words; the skill takes it from there.
// The words are for the model, so they stay English; `askText` adds the
// language to answer in.
const ASK_STATUS = 'Show the engineering playbook session status (playbook-config, Status).'
const ASK_LEARN = 'Run the engineering-learn skill for this project (playbook-config, Learn).'

// A typed value cannot be a button: the model asks for the number and runs `set`.
function askCustom(key: string): string {
  return `Set the engineering playbook setting ${key} to a custom value (playbook-config, Settings): ask me for the value, then apply it.`
}

type LabStep = 'setup' | 'passive' | 'active' | 'sqlmap' | 'uninstall'

const LAB_STEPS: { step: LabStep; text: keyof PanelText }[] = [
  { step: 'setup', text: 'lab_setup' },
  { step: 'passive', text: 'lab_passive' },
  { step: 'active', text: 'lab_active' },
  { step: 'sqlmap', text: 'lab_sqlmap' },
  { step: 'uninstall', text: 'lab_uninstall' },
]

function askLab(step: LabStep): string {
  return `Run the red-lab-playbook step "${step}" for this project (playbook-config, red-lab-playbook).`
}

// What a numbered entry does, as data: the module may hand `$` only to named
// functions, so one switch (`run`) turns an action into calls.
type Action =
  | { kind: 'open'; key: string }
  | { kind: 'group'; group: string }
  | { kind: 'set'; key: string; value: string }
  | { kind: 'lab' }
  | { kind: 'back' }
  | { kind: 'ask'; text: string }
  | { kind: 'diff' }
  | { kind: 'exit' }

type Entry = { digit: string; label: string; action: Action }

const MAIN: MenuScreen = { kind: 'main' }
const LAB: MenuScreen = { kind: 'lab' }

function configScript($: EngineInterface): string {
  return `${$.plugin.root}/scripts/playbook_config.py`
}

function isSnapshot(value: unknown): value is ConfigSnapshot {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const fields = value as { settings?: unknown; groups?: unknown; ui?: unknown; language_name?: unknown }

  return (
    Array.isArray(fields.settings) &&
    Array.isArray(fields.groups) &&
    typeof fields.ui === 'object' &&
    fields.ui !== null &&
    typeof fields.language_name === 'string'
  )
}

// Reads the settings and the panel's strings; false when they could not be read.
async function refresh($: EngineInterface): Promise<boolean> {
  const ran = await $.process.run(['python3', configScript($), 'show', '--json'])
  if (ran.exitCode !== 0) {
    await update($, notice, () => ({ text: ran.stderr.trim() || 'could not read the settings', isError: true }))
    return false
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(ran.stdout)
  } catch {
    parsed = null
  }
  if (!isSnapshot(parsed)) {
    await update($, notice, () => ({ text: 'the settings script answered something unreadable', isError: true }))
    return false
  }
  await update($, snapshot, () => parsed)
  const report = await $.process.run(['python3', `${$.plugin.root}/hooks/output_filter.py`, '--report'])
  await update($, filterReport, () => (report.exitCode === 0 ? report.stdout.trim() || null : null))

  return true
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

// A prompt for the model, told to answer in the person's language: an English
// prompt would otherwise pull the reply into English.
function askText(prompt: string, loaded: ConfigSnapshot): string {
  return loaded.language === ENGLISH ? prompt : `${prompt} Answer in ${loaded.language_name}.`
}

function settingLine(setting: ConfigSetting, ui: PanelText): string {
  const label = setting.choices.find((choice): boolean => choice.value === setting.value)?.label ?? setting.value
  const marker = setting.value === setting.default ? '' : ` (${ui.default} ${setting.default})`

  return `${setting.label}: ${label}${marker}`
}

// The heading and the explanation under it, for the screen shown.
function heading(current: MenuScreen, loaded: ConfigSnapshot): { title: string; help: string | undefined } {
  const { ui } = loaded
  if (current.kind === 'lab') {
    return { title: ui.lab_title, help: ui.lab_help }
  }
  if (current.kind === 'setting') {
    const shown = loaded.settings.find((one): boolean => one.key === current.key)
    if (shown) {
      return { title: shown.label, help: shown.help }
    }
  }
  if (current.kind === 'group') {
    const group = loaded.groups.find((one): boolean => one.key === current.group)
    if (group) {
      return { title: group.label, help: group.help }
    }
  }

  return { title: ui.title, help: undefined }
}

// The main screen's setting rows: a setting of its own, or one row per group,
// placed where the group's first setting comes.
function mainRows(loaded: ConfigSnapshot): { label: string; action: Action }[] {
  const rows: { label: string; action: Action }[] = []
  const placed = new Set<string>()
  for (const setting of loaded.settings) {
    if (setting.group === '') {
      rows.push({ label: settingLine(setting, loaded.ui), action: { kind: 'open', key: setting.key } })
      continue
    }
    const group = loaded.groups.find((one): boolean => one.key === setting.group)
    if (group && !placed.has(group.key)) {
      placed.add(group.key)
      rows.push({ label: group.label, action: { kind: 'group', group: group.key } })
    }
  }

  return rows
}

function noEntry(ui: PanelText, digit: string): string {
  return `${ui.title}: ${ui.no_entry.replace('{digit}', digit)}`
}

// The numbered entries of the screen shown: one place decides what each digit
// does, for the band's buttons and for a digit typed and sent alike.
function entries(current: MenuScreen, loaded: ConfigSnapshot): Entry[] {
  const { ui, settings } = loaded
  const back: Entry = { digit: EXIT, label: ui.back, action: { kind: 'back' } }
  if (current.kind === 'setting') {
    const setting = settings.find((one): boolean => one.key === current.key)
    if (!setting) {
      return [back]
    }
    const picks: Entry[] = setting.choices.slice(0, 9).map(
      (choice, index): Entry => ({
        digit: String(index + 1),
        label: choice.value === setting.value ? `${choice.label} (${ui.current})` : choice.label,
        action: { kind: 'set', key: setting.key, value: choice.value },
      }),
    )
    if (setting.custom) {
      picks.push({
        digit: String(picks.length + 1),
        label: ui.custom,
        action: { kind: 'ask', text: askText(askCustom(setting.key), loaded) },
      })
    }

    return [...picks, back]
  }
  if (current.kind === 'group') {
    const members: Entry[] = settings
      .filter((setting): boolean => setting.group === current.group)
      .slice(0, 9)
      .map(
        (setting, index): Entry => ({
          digit: String(index + 1),
          label: settingLine(setting, ui),
          action: { kind: 'open', key: setting.key },
        }),
      )

    return [...members, back]
  }
  if (current.kind === 'lab') {
    const steps: Entry[] = LAB_STEPS.map(
      ({ step, text }, index): Entry => ({
        digit: String(index + 1),
        label: ui[text],
        action: { kind: 'ask', text: askText(askLab(step), loaded) },
      }),
    )

    return [...steps, back]
  }
  const rows: Entry[] = mainRows(loaded)
    .slice(0, MAX_SETTING_ROWS)
    .map((row, index): Entry => ({ digit: String(index + 1), ...row }))
  const after = rows.length + 1

  return [
    ...rows,
    { digit: String(after), label: ui.status, action: { kind: 'ask', text: askText(ASK_STATUS, loaded) } },
    { digit: String(after + 1), label: ui.learn, action: { kind: 'ask', text: askText(ASK_LEARN, loaded) } },
    { digit: String(after + 2), label: ui.lab, action: { kind: 'lab' } },
    { digit: String(after + 3), label: ui.diff, action: { kind: 'diff' } },
    { digit: EXIT, label: ui.exit, action: { kind: 'exit' } },
  ]
}

async function run($: EngineInterface, action: Action): Promise<void> {
  switch (action.kind) {
    case 'open':
      await update($, screen, (): MenuScreen => ({ kind: 'setting', key: action.key }))
      return
    case 'group':
      await update($, screen, (): MenuScreen => ({ kind: 'group', group: action.group }))
      return
    case 'set':
      await apply($, action.key, action.value)
      return
    case 'lab':
      await update($, screen, () => LAB)
      return
    case 'back':
      await update($, screen, () => MAIN)
      return
    case 'ask':
      await askModel($, action.text)
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
  const loaded = await read($, snapshot)
  if (current === null || loaded === null) {
    return undefined
  }

  return entries(current, loaded).find((one): boolean => one.digit === digit)
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
  // skill, which handles it directly; taking it here dropped the request. When
  // the settings cannot be read the menu has nothing to draw, not even its own
  // words, so the skill's menu runs instead.
  on('command.run', async ($, e, next) => {
    if (e.command !== COMMAND && !e.command.endsWith(`:${COMMAND}`)) {
      return next(e)
    }
    if (e.args.trim() !== '' || e.origin?.kind !== 'composer') {
      return next(e)
    }
    if (!(await refresh($))) {
      return next(e)
    }
    const loaded = await read($, snapshot)
    if (loaded === null) {
      return next(e)
    }
    await update($, notice, () => null)
    await update($, screen, () => MAIN)

    return { text: loaded.ui.opened }
  }).catch(($, e, next) => next(e)) // the menu failed: the skill's menu runs instead

  // While the menu is open, a lone digit sent from the prompt picks its entry
  // and never reaches the model; Status, Learn and a lab step become the prompt
  // itself (a prompt.submit hook may not submit another prompt). Anything else
  // sent closes the menu and goes on as typed, so a later digit meant for the
  // model (an answer to its numbered question) is never taken by a forgotten
  // menu.
  // The Temporary data panel answers the same way while it is open.
  on('prompt.submit', async ($, e, next) => {
    if ((await read($, screen)) === null) {
      const panel = await panelPrompt($, e.text, e.command !== undefined)

      return panel === 'pass' ? await next(e) : panel
    }
    const digit = DIGIT_RE.exec(e.text)?.[1]
    if (digit === undefined || e.command !== undefined) {
      await close($)

      return next(e)
    }
    const loaded = await read($, snapshot)
    const entry = await entryFor($, digit)
    if (!entry || loaded === null) {
      return { drop: loaded === null ? `no entry ${digit}` : noEntry(loaded.ui, digit) }
    }
    if (entry.action.kind === 'ask') {
      await close($)

      return next({ ...e, text: entry.action.text })
    }
    await run($, entry.action)
    const last = await read($, notice)
    const isSet = entry.action.kind === 'set'

    return { drop: (isSet && last?.text.split('\n').pop()) || `${loaded.ui.title}: ${entry.label}` }
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
    const loaded = await read($, snapshot)
    if (current === null || loaded === null || e.props.hasSurvey) {
      return next(e)
    }
    const { Box, Text, Button } = $.ui.resolve(e)
    const last = await read($, notice)
    const report = await read($, filterReport)
    const { ui } = loaded
    const isMain = current.kind === 'main'
    const { title, help } = heading(current, loaded)

    return (
      <Box flexDirection="column">
        <Text bold>
          {title} <Text dimColor>{isMain ? ui.hint_main : ui.hint_sub}</Text>
        </Text>
        {help !== undefined && <Text dimColor>{help}</Text>}
        {entries(current, loaded).map(entry => (
          <Button
            key={`entry-${entry.digit}`}
            plain
            hotkey={entry.digit}
            label={entry.label}
            onPress={() => void choose($, entry.digit)}
          />
        ))}
        {isMain && report !== null && <Text dimColor>{report}</Text>}
        {last !== null && (
          <Text color={last.isError ? 'red' : undefined} dimColor={!last.isError}>
            {last.text}
          </Text>
        )}
      </Box>
    )
  })

  registerTemporaryData(on)
}
