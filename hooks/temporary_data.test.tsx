import { expect, test } from 'claude-code/testing'
import type { TestBody } from 'claude-code/testing'

type TestOn = Parameters<TestBody>[1]

const BAND = {
  plugin: 'engineering-playbook',
  component: 'AbovePrompt',
  props: {
    hasSurvey: false,
    isCollapsed: false,
    isWorking: true,
    maxRows: 30,
    bodyColumns: 100,
    scroll: { offset: 0, bodyRows: 30 },
    view: {},
  },
} as const

const UI = {
  tmp_waiting: 'Auto mode is waiting for your answer:',
  tmp_hint: 'Click, or ctrl+x tab, the arrows and Enter. The rest of the work goes on.',
  tmp_approve: 'Approve',
  tmp_decline: 'Decline',
  tmp_title: 'Temporary data',
  tmp_help: 'Deletions of this project you have not run yet.',
  tmp_close: 'Close',
  tmp_empty: 'Nothing kept for this project.',
  tmp_yes: 'Yes, remove',
  tmp_no: 'No, keep it',
}

const ITEM = {
  id: 'a1b2c3d4e5f6',
  title: 'git worktree app',
  detail: '/work/app',
  origin: 'asked by Claude on 2026-11-21 20:00',
  text: 'deletes files and directories recursively: rm -rf build (asked by Claude on 2026-11-21 20:00)',
  confirm: 'Remove it? This runs, in /work/app:',
  command: 'rm -rf build',
}

const WAITING = {
  ui: UI,
  pending: [
    { id: 3, text: '#3 deletes files and directories recursively (queued 2 min ago)', command: 'rm -rf build', cut: '' },
  ],
  more: '',
  open: '',
  items: [],
}

const KEPT = { ui: UI, pending: [], more: '', open: 'Temporary data (1)', items: [ITEM] }
const EMPTY = { ui: UI, pending: [], more: '', open: '', items: [] }

const exited = (exitCode: number, stdout: string, stderr = '') => ({
  value: { exitCode, stdout, stderr, isStdoutTruncated: false, isStderrTruncated: false },
})

const typed = (command: string) =>
  ({
    command,
    args: '',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: false, columns: 120 },
  }) as const

const sent = (text: string) => ({ text, wait: false, origin: { kind: 'composer' } }) as const

// The engine around the module: a session, its folder, a toast and the rows
// appended for the model. `views` answers `show` in turn, the last one again.
function engine(on: TestOn, views: object[], answer: object): { calls: string[][]; notes: string[] } {
  const calls: string[][] = []
  const notes: string[] = []
  let shown = 0
  on('session.id', () => ({ value: 'sess-1' }))
  on('session.cwd', () => ({ value: '/work/app' }))
  on('ui.toast', () => ({ value: undefined }))
  on('command.register', () => ({ value: { command: 'temporary-data' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.append', async (_, e, next) => {
    const block = e.message.content[0]
    const text: unknown = block !== undefined && block.type === 'text' ? block.text : ''
    notes.push(typeof text === 'string' ? text : '')

    return await next(e)
  })
  on('process.run', (_, e) => {
    calls.push([...e.argv])
    if (e.argv.includes('show')) {
      const view = views[Math.min(shown, views.length - 1)]
      shown += 1

      return exited(0, JSON.stringify(view))
    }

    return exited(0, JSON.stringify(answer))
  })

  return { calls, notes }
}

test('a queued command shows Decline first, focused, and Approve, with no digit hotkey', async ($, on) => {
  engine(on, [WAITING], {})
  await $.session.start({ cwd: '/work/app', surface: 'terminal', isInteractive: true })

  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  const headline = await band.find({ type: 'Text', text: /#3 deletes/ })
  expect(headline?.text).not.toContain('rm -rf build')
  expect(headline?.props.bold).toBe(true)
  expect((await band.find({ type: 'Text', text: 'rm -rf build' }))?.props.dimColor).toBe(true)
  expect((await band.find({ type: 'Box', key: 'queued-3' }))?.props.borderStyle).toBe('round')
  const decline = await band.find({ type: 'Button', key: 'decline-3' })
  const approve = await band.find({ type: 'Button', key: 'approve-3' })
  expect(decline?.props.autoFocus).toBe(true)
  expect(decline?.props.hotkey).toBeUndefined()
  expect(approve?.props.hotkey).toBeUndefined()
  expect((await band.find({ type: 'Text', text: 'Decline' }))?.props.color).toBe('red')
  expect((await band.find({ type: 'Text', text: 'Approve' }))?.props.color).toBe('green')
  await band.unmount()
})

test('Approve runs the script for that number and tells the model', async ($, on) => {
  const answer = { text: '#3 ran.', note: '[playbook:consent] The user approved #3', is_error: false }
  const { calls, notes } = engine(on, [WAITING, EMPTY], answer)
  await $.session.start({ cwd: '/work/app', surface: 'terminal', isInteractive: true })

  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await band.press({ key: 'approve-3' })
  await band.unmount()

  const ran = calls.find(argv => argv.includes('approve'))
  expect(ran?.slice(-7)).toEqual(['approve', '--session', 'sess-1', '--project', '/work/app', '--id', '3'])
  expect(notes).toEqual(['[playbook:consent] The user approved #3'])
  const after = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await after.find({ type: 'Button', key: 'approve-3' })).toBeUndefined()
  await after.unmount()
})

test('Decline runs the script for that number', async ($, on) => {
  const answer = { text: '#3 kept in Temporary data.', note: '[playbook:consent] The user declined #3', is_error: false }
  const { calls, notes } = engine(on, [WAITING, KEPT], answer)
  await $.session.start({ cwd: '/work/app', surface: 'terminal', isInteractive: true })

  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await band.press({ key: 'decline-3' })
  await band.unmount()

  expect(calls.some(argv => argv.includes('decline') && argv.includes('3'))).toBe(true)
  expect(notes).toEqual(['[playbook:consent] The user declined #3'])
  const after = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await after.find({ type: 'Button', key: 'open' }))?.props.label).toBe('Temporary data (1)')
  await after.unmount()
})

test('the panel says what it is, puts Close first, and removing takes a second press', async ($, on) => {
  const answer = { text: 'Removed.', note: '', is_error: false }
  const { calls, notes } = engine(on, [KEPT, KEPT, EMPTY], answer)
  await $.command.run(typed('temporary-data'))

  const list = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await list.find({ type: 'Text', text: /Deletions of this project/ }))?.text).toBeTruthy()
  expect((await list.find({ type: 'Text', text: 'Temporary data' }))?.props.bold).toBe(true)
  expect((await list.find({ type: 'Box', key: `card-${ITEM.id}` }))?.props.borderStyle).toBe('round')
  expect((await list.find({ type: 'Button', key: `item-${ITEM.id}` }))?.props.label).toBe(ITEM.title)
  expect((await list.find({ type: 'Text', text: ITEM.detail }))?.props.dimColor).toBe(true)
  expect((await list.find({ type: 'Text', text: ITEM.origin }))?.props.dimColor).toBe(true)
  expect(await list.find({ type: 'Text', text: /rm -rf build/ })).toBeUndefined()
  const closeButton = await list.find({ type: 'Button', key: 'close' })
  expect(closeButton?.props.hotkey).toBe('0')
  expect(closeButton?.props.autoFocus).toBe(true)
  await list.press({ key: `item-${ITEM.id}` })
  await list.unmount()
  expect(calls.some(argv => argv.includes('remove'))).toBe(false)

  const confirm = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await confirm.find({ type: 'Text', text: /This runs/ }))?.text).toBe(ITEM.confirm)
  expect((await confirm.find({ type: 'Button', key: 'confirm-no' }))?.props.autoFocus).toBe(true)
  await confirm.press({ key: 'confirm-yes' })
  await confirm.unmount()

  const ran = calls.find(argv => argv.includes('remove'))
  expect(ran?.slice(-5)).toEqual(['remove', '--project', '/work/app', '--item', ITEM.id])
  expect(notes).toEqual([])
})

test('digits sent while the panel is open never reach the model', async ($, on) => {
  const toModel: string[] = []
  engine(on, [KEPT], { text: 'Removed.', note: '', is_error: false })
  on('prompt.submit', (_, e) => {
    toModel.push(e.text)

    return { text: e.text }
  })
  await $.command.run(typed('temporary-data'))

  expect((await $.prompt.submit(sent('1'))).drop).toBeTruthy()
  expect((await $.prompt.submit(sent('0'))).drop).toBeTruthy()
  expect((await $.prompt.submit(sent('0'))).drop).toBeTruthy()
  await $.prompt.submit(sent('1'))
  expect(toModel).toEqual(['1'])
})

test('with nothing waiting or kept the band gives its row back', async ($, on) => {
  engine(on, [EMPTY], {})
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)

    return <Text>engine</Text>
  })
  await $.session.start({ cwd: '/work/app', surface: 'terminal', isInteractive: true })

  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await band.find({ type: 'Text' }))?.text).toBe('engine')
  await band.unmount()
})

test('a queued deny refreshes the band', async ($, on) => {
  const { calls } = engine(on, [EMPTY, WAITING], {})
  on('tool.call', () => ({ deny: "[playbook:safety] It is queued for the user's approval as #3." }))
  await $.session.start({ cwd: '/work/app', surface: 'terminal', isInteractive: true })
  await $.tool.call({ tool: 'Bash', command: 'rm -rf build' })

  expect(calls.filter(argv => argv.includes('show')).length).toBe(2)
  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await band.find({ type: 'Button', key: 'approve-3' })).toBeTruthy()
  await band.unmount()
})
