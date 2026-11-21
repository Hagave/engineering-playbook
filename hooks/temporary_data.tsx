// Claude Code only: the button above the chat for a destructive command auto
// mode queued, and the Temporary data panel.
//
// Interface, never rules: every list, decision and string comes from
// scripts/temporary_data.py, which carries out a press under the queue's lock
// (`approve` runs the command, `decline` keeps a deletion in Temporary data).
// The answer in the chat ("pode apagar") still works alongside, through
// hooks/consent_guard.py.
//
// Both sit in the band above the prompt, not in a pane: the terminal (VS Code's
// included) does not hand a pane the keyboard, so its arrows went to the prompt
// (see config_panel.tsx). The band takes clicks, and after ctrl+x tab its arrows
// and Enter. The approval row has no digit hotkey: a bare digit in an empty
// prompt presses a band Button, and a destructive command must never run on the
// first key of a message. The panel, which the person opens, numbers its
// entries like the settings menu, and removing still takes a second press.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type {
  PanelNotice,
  TemporaryAnswer,
  TemporaryItem,
  TemporaryPending,
  TemporaryScreen,
  TemporaryView,
} from './config_panel_state'

const COMMAND = 'temporary-data'
const TITLE = 'Temporary data'
const CLOSE = '0'
const YES = '1'
const MAX_ITEMS = 9
const ACTION_TIMEOUT_MS = 600_000
const DIGIT_RE = /^\s*(\d)\s*$/
// The gate's deny for a command it queued (context_guard._consent_queued_msg).
const QUEUED_MARK = "queued for the user's approval"

type On = Parameters<Register>[0]
type QueuedAction = 'approve' | 'decline'

const view = atom({ plugin: 'engineering-playbook', key: 'tmpView' } as const, null, {
  shape: 'tmp-view-1',
})
const tmpScreen = atom({ plugin: 'engineering-playbook', key: 'tmpScreen' } as const, null, {
  shape: 'tmp-screen-1',
})
const tmpNotice = atom({ plugin: 'engineering-playbook', key: 'tmpNotice' } as const, null, {
  shape: 'tmp-notice-1',
})
// The settings menu's screen, owned by config_panel.tsx (same reference and
// shape there): read only, to yield the band while the menu is open. The state
// library takes an atom declared in the file that uses it.
const menuScreen = atom({ plugin: 'engineering-playbook', key: 'screen' } as const, null, {
  shape: 'screen-3',
})
// The queued command a press is running: its row shows no buttons meanwhile.
const busy = atom({ plugin: 'engineering-playbook', key: 'tmpBusy' } as const, null, {
  shape: 'tmp-busy-1',
})

const LIST: TemporaryScreen = { kind: 'list' }

function script($: EngineInterface): string {
  return `${$.plugin.root}/scripts/temporary_data.py`
}

function isView(value: unknown): value is TemporaryView {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const fields = value as { ui?: unknown; pending?: unknown; items?: unknown }

  return (
    typeof fields.ui === 'object' && fields.ui !== null && Array.isArray(fields.pending) && Array.isArray(fields.items)
  )
}

function isAnswer(value: unknown): value is TemporaryAnswer {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const fields = value as { text?: unknown; note?: unknown; is_error?: unknown }

  return typeof fields.text === 'string' && typeof fields.note === 'string' && typeof fields.is_error === 'boolean'
}

function parsed(stdout: string): unknown {
  try {
    return JSON.parse(stdout)
  } catch {
    return null
  }
}

async function where($: EngineInterface): Promise<{ session: string; project: string }> {
  return { session: await $.session.id(), project: await $.session.cwd() }
}

// Reads the queue and the project's Temporary data; keeps the last view when
// the script fails, so a broken read never hides a waiting command.
async function refresh($: EngineInterface): Promise<void> {
  const { session, project } = await where($)
  const ran = await $.process.run(['python3', script($), 'show', '--session', session, '--project', project])
  const shown = ran.exitCode === 0 ? parsed(ran.stdout) : null
  if (isView(shown)) {
    await update($, view, () => shown)
  }
}

async function say($: EngineInterface, text: string, isError: boolean): Promise<void> {
  const said: PanelNotice = { text, isError }
  await update($, tmpNotice, () => said)
  $.ui.toast(text)
}

// Runs one action of the script and tells the person and, where it says
// something, the model.
async function act($: EngineInterface, argv: string[]): Promise<void> {
  const ran = await $.process.run(['python3', script($), ...argv], { timeoutMs: ACTION_TIMEOUT_MS })
  const answer = ran.exitCode === 0 ? parsed(ran.stdout) : null
  if (!isAnswer(answer)) {
    await say($, ran.stderr.trim() || 'temporary data: the script answered something unreadable', true)
    return
  }
  await say($, answer.text, answer.is_error)
  if (answer.note !== '') {
    await $.session.append({ message: { type: 'user', content: [{ type: 'text', text: answer.note }] } })
  }
}

async function answerQueued($: EngineInterface, id: number, action: QueuedAction): Promise<void> {
  if ((await read($, busy)) !== null) {
    return
  }
  await update($, busy, () => id)
  try {
    const { session, project } = await where($)
    await act($, [action, '--session', session, '--project', project, '--id', String(id)])
  } finally {
    await update($, busy, () => null)
    await refresh($)
  }
}

async function removeItem($: EngineInterface, item: TemporaryItem): Promise<void> {
  const { project } = await where($)
  await act($, ['remove', '--project', project, '--item', item.id])
  await refresh($)
  await update($, tmpScreen, () => LIST)
}

async function open($: EngineInterface): Promise<void> {
  await refresh($)
  await update($, tmpNotice, () => null)
  await update($, tmpScreen, () => LIST)
}

async function close($: EngineInterface): Promise<void> {
  await update($, tmpScreen, () => null)
}

// What a digit does on the screen shown: one place for the band's buttons and
// for a digit typed and sent alike. False when the digit names nothing.
async function choose($: EngineInterface, digit: string): Promise<boolean> {
  const current = await read($, tmpScreen)
  const loaded = await read($, view)
  if (current === null || loaded === null) {
    return false
  }
  if (digit === CLOSE) {
    if (current.kind === 'confirm') {
      await update($, tmpScreen, () => LIST)
    } else {
      await close($)
    }
    return true
  }
  if (current.kind === 'confirm') {
    const item = loaded.items.find((one): boolean => one.id === current.item)
    if (digit !== YES || item === undefined) {
      return false
    }
    await removeItem($, item)
    return true
  }
  const item = loaded.items.slice(0, MAX_ITEMS)[Number(digit) - 1]
  if (item === undefined) {
    return false
  }
  const confirm: TemporaryScreen = { kind: 'confirm', item: item.id }
  await update($, tmpScreen, () => confirm)
  return true
}

// The prompt while the panel is open: a lone digit picks its entry and never
// reaches the model (`drop`); anything else closes the panel and goes on as
// typed (`pass`), as the settings menu does. `pass` too while it is closed.
// One `prompt.submit` hook per module: config_panel.tsx calls this from its own.
export async function panelPrompt(
  $: EngineInterface,
  text: string,
  isCommand: boolean,
): Promise<{ drop: string } | 'pass'> {
  if ((await read($, tmpScreen)) === null) {
    return 'pass'
  }
  const digit = DIGIT_RE.exec(text)?.[1]
  if (digit === undefined || isCommand) {
    await close($)
    return 'pass'
  }
  const isDone = await choose($, digit)
  const loaded = await read($, view)

  return { drop: isDone ? (loaded?.ui.tmp_title ?? TITLE) : `${TITLE}: no entry ${digit}` }
}

export function registerTemporaryData(on: On): void {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: COMMAND, description: 'Show the Temporary data panel of this project' })
    await refresh($)

    return await next(e)
  }).catch(async ($, e, next) => await next(e)) // the panel failed: the session goes on without it

  on('command.run', { command: COMMAND }, async $ => {
    await open($)
    const loaded = await read($, view)

    return { text: loaded?.ui.tmp_title ?? TITLE }
  })

  // A command the gate queued, or one the model ran after a written approval:
  // both change what the band shows.
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const ran = await next(e)
    const said = typeof ran.deny === 'string' ? ran.deny : ''
    const loaded = await read($, view)
    if (said.includes(QUEUED_MARK) || (loaded !== null && loaded.pending.length > 0)) {
      await refresh($)
    }

    return ran
  }).catch(async ($, e, next) => await next(e)) // the band is never a reason to change the call

  on('turn.complete', async ($, e, next) => {
    await refresh($)

    return await next(e)
  }).catch(async ($, e, next) => await next(e))

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const loaded = await read($, view)
    // The settings menu, opened on purpose, keeps the band while it is open.
    const isMenuOpen = (await read($, menuScreen)) !== null
    if (loaded === null || e.props.hasSurvey || isMenuOpen) {
      return await next(e)
    }
    const { Box, Text, Button } = $.ui.resolve(e)
    const { ui } = loaded
    const running = await read($, busy)
    const last = await read($, tmpNotice)
    const notice = last !== null && (
      <Text color={last.isError ? 'red' : undefined} dimColor={!last.isError}>
        {last.text}
      </Text>
    )

    if (loaded.pending.length > 0) {
      return (
        <Box flexDirection="column">
          <Text bold>
            {ui.tmp_waiting} <Text dimColor>{ui.tmp_hint}</Text>
          </Text>
          {loaded.pending.map((entry: TemporaryPending, index: number) => (
            <Box key={`queued-${entry.id}`} flexDirection="column">
              <Text>{entry.text}</Text>
              {entry.cut !== '' && <Text dimColor>{entry.cut}</Text>}
              {running === entry.id ? (
                <Text dimColor>…</Text>
              ) : (
                <Box>
                  <Button
                    key={`decline-${entry.id}`}
                    label={ui.tmp_decline}
                    {...(index === 0 ? { autoFocus: true as const } : {})}
                    onPress={() => void answerQueued($, entry.id, 'decline')}
                  />
                  <Text> </Text>
                  {entry.cut === '' && (
                    <Button
                      key={`approve-${entry.id}`}
                      label={ui.tmp_approve}
                      variant="primary"
                      onPress={() => void answerQueued($, entry.id, 'approve')}
                    />
                  )}
                </Box>
              )}
            </Box>
          ))}
          {loaded.more !== '' && <Text dimColor>{loaded.more}</Text>}
          {notice}
        </Box>
      )
    }

    const current = await read($, tmpScreen)
    if (current !== null && current.kind === 'confirm') {
      const item = loaded.items.find((one): boolean => one.id === current.item)
      if (item !== undefined) {
        return (
          <Box flexDirection="column">
            <Text bold>{ui.tmp_title}</Text>
            <Text>{item.text}</Text>
            <Text dimColor>{item.confirm}</Text>
            <Text>{item.command}</Text>
            <Button key="confirm-no" plain hotkey={CLOSE} label={ui.tmp_no} autoFocus onPress={() => void choose($, CLOSE)} />
            <Button key="confirm-yes" plain hotkey={YES} label={ui.tmp_yes} onPress={() => void choose($, YES)} />
            {notice}
          </Box>
        )
      }
    }
    if (current !== null) {
      return (
        <Box flexDirection="column">
          <Text bold>
            {ui.tmp_title} <Text dimColor>{ui.tmp_help}</Text>
          </Text>
          <Button key="close" plain hotkey={CLOSE} label={ui.tmp_close} autoFocus onPress={() => void choose($, CLOSE)} />
          {loaded.items.length === 0 && <Text dimColor>{ui.tmp_empty}</Text>}
          {loaded.items.slice(0, MAX_ITEMS).map((one: TemporaryItem, index: number) => (
            <Button
              key={`item-${one.id}`}
              plain
              hotkey={String(index + 1)}
              label={one.text}
              onPress={() => void choose($, String(index + 1))}
            />
          ))}
          {notice}
        </Box>
      )
    }

    if (loaded.open !== '') {
      return <Button key="open" label={loaded.open} dimColor onPress={() => void open($)} />
    }

    return await next(e)
  })
}
