import { expect, test } from 'claude-code/testing'

const BAND = {
  plugin: 'engineering-playbook',
  component: 'AbovePrompt',
  props: {
    hasSurvey: false,
    isCollapsed: false,
    isWorking: false,
    maxRows: 30,
    bodyColumns: 100,
    scroll: { offset: 0, bodyRows: 30 },
    view: {},
  },
} as const

const SNAPSHOT = {
  settings: [
    {
      key: 'prose',
      label: 'Prose level',
      description: 'response verbosity',
      help: 'How much explanation a response carries. Applies from the next session.',
      value: 'low',
      default: 'mid',
      choices: [
        { value: 'low', label: 'low' },
        { value: 'mid', label: 'mid' },
        { value: 'high', label: 'high' },
      ],
    },
    {
      key: 'watch',
      label: 'Known-failure warnings',
      description: 'warnings',
      help: 'Before a write or command that touches something that already failed, warn.',
      value: 'off',
      default: 'off',
      choices: [
        { value: 'on', label: 'on' },
        { value: 'off', label: 'off' },
      ],
    },
  ],
}

// What the engine would stamp on a command the person typed.
const typed = (command: string) =>
  ({
    command,
    args: '',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: false, columns: 120 },
  }) as const

const sent = (text: string) => ({ text, wait: false, origin: { kind: 'composer' } }) as const

const exited = (exitCode: number, stdout: string, stderr = '') => ({
  value: { exitCode, stdout, stderr, isStdoutTruncated: false, isStderrTruncated: false },
})

const REPORT = 'Output filter: on, 12 kB saved'
const EFFECT = 'Applies from the next session start or compaction.'

test('/playbook-config shows the numbered menu above the prompt', async ($, on) => {
  on('process.run', (_, e) => exited(0, e.argv.includes('--report') ? REPORT : JSON.stringify(SNAPSHOT)))
  on('command.run', () => ({ text: 'the skill ran' }))

  const ran = await $.command.run(typed('playbook-config'))
  expect(ran.text).toContain('type a number')

  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  const prose = await band.find({ type: 'Button', key: 'entry-1' })
  expect(prose?.props.label).toBe('Prose level: low (default mid)')
  expect(prose?.props.hotkey).toBe('1')
  expect((await band.find({ type: 'Button', key: 'entry-0' }))?.props.label).toBe('Exit')
  expect((await band.find({ type: 'Text', text: /Output filter/ }))?.text).toBe(REPORT)
  await band.unmount()
})

test('the namespaced command opens the menu too', async ($, on) => {
  on('process.run', () => exited(0, JSON.stringify(SNAPSHOT)))
  on('command.run', () => ({ text: 'the skill ran' }))

  const ran = await $.command.run(typed('engineering-playbook:playbook-config'))
  expect(ran.text).toContain('type a number')
})

test('with arguments the request reaches the skill', async ($, on) => {
  on('process.run', () => exited(0, JSON.stringify(SNAPSHOT)))
  on('command.run', () => ({ text: 'the skill ran' }))

  for (const args of ['prose low', 'learn']) {
    const ran = await $.command.run({ ...typed('playbook-config'), args })
    expect(ran.text).toBe('the skill ran')
  }
})

test('another command passes through untouched', async ($, on) => {
  on('command.run', () => ({ text: 'status ran' }))

  const ran = await $.command.run(typed('status'))
  expect(ran.text).toBe('status ran')
})

test('digits sent from the prompt walk the menu and set without reaching the model', async ($, on) => {
  const calls: string[][] = []
  const toModel: string[] = []
  on('process.run', (_, e) => {
    calls.push([...e.argv])

    return exited(0, e.argv.includes('set') ? `[playbook:config] prose = mid\n${EFFECT}` : JSON.stringify(SNAPSHOT))
  })
  on('ui.toast', () => ({ value: undefined }))
  on('prompt.submit', (_, e) => {
    toModel.push(e.text)

    return { text: e.text }
  })
  on('command.run', () => ({ text: 'the skill ran' }))
  await $.command.run(typed('playbook-config'))

  const opened = await $.prompt.submit(sent('1'))
  expect(opened.drop).toBeTruthy()

  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await band.find({ type: 'Text', text: /How much explanation/ }))?.text).toBeTruthy()
  expect((await band.find({ type: 'Button', key: 'entry-1' }))?.props.label).toBe('low (current)')
  expect((await band.find({ type: 'Button', key: 'entry-0' }))?.props.label).toBe('Back')
  await band.unmount()

  const picked = await $.prompt.submit(sent('2'))
  expect(picked.drop).toBe(EFFECT)
  expect(calls.some(argv => argv.slice(-3).join(' ') === 'set prose mid')).toBe(true)
  expect(toModel).toEqual([])
})

test('a band button press does what its digit does', async ($, on) => {
  const calls: string[][] = []
  on('process.run', (_, e) => {
    calls.push([...e.argv])

    return exited(0, e.argv.includes('set') ? `[playbook:config] watch = on\n${EFFECT}` : JSON.stringify(SNAPSHOT))
  })
  on('ui.toast', () => ({ value: undefined }))
  on('command.run', () => ({ text: 'the skill ran' }))
  await $.command.run(typed('playbook-config'))

  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await band.press({ key: 'entry-2' })
  await band.unmount()

  const choices = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await choices.press({ key: 'entry-1' })
  await choices.unmount()
  expect(calls.some(argv => argv.slice(-3).join(' ') === 'set watch on')).toBe(true)
})

test('0 exits and the band gives its row back', async ($, on) => {
  on('process.run', () => exited(0, JSON.stringify(SNAPSHOT)))
  on('command.run', () => ({ text: 'the skill ran' }))
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)

    return <Text>engine</Text>
  })
  await $.command.run(typed('playbook-config'))
  await $.prompt.submit(sent('0'))

  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await band.find({ type: 'Button', key: 'entry-1' })).toBeUndefined()
  await band.unmount()
})

test('Learn closes the menu and hands the request to the model', async ($, on) => {
  const toModel: string[] = []
  on('process.run', () => exited(0, JSON.stringify(SNAPSHOT)))
  on('prompt.submit', (_, e) => {
    toModel.push(e.text)

    return { text: e.text }
  })
  on('command.run', () => ({ text: 'the skill ran' }))
  await $.command.run(typed('playbook-config'))

  // Two settings, so Status is 3 and Learn is 4.
  await $.prompt.submit(sent('4'))
  expect(toModel.some(text => text.includes('engineering-learn'))).toBe(true)
})

test('prompts pass through when the menu is closed or the text is no digit', async ($, on) => {
  const toModel: string[] = []
  on('process.run', () => exited(0, JSON.stringify(SNAPSHOT)))
  on('prompt.submit', (_, e) => {
    toModel.push(e.text)

    return { text: e.text }
  })
  on('command.run', () => ({ text: 'the skill ran' }))

  await $.prompt.submit(sent('1'))
  await $.command.run(typed('playbook-config'))
  await $.prompt.submit(sent('fix the login bug'))
  expect(toModel).toEqual(['1', 'fix the login bug'])

  // Other text closed the menu: a digit after it is the person's, for the model.
  await $.prompt.submit(sent('2'))
  expect(toModel).toEqual(['1', 'fix the login bug', '2'])
})

test('a script failure is shown in red, not swallowed', async ($, on) => {
  on('process.run', (_, e) =>
    e.argv.includes('set')
      ? exited(1, '', '[playbook:config] prose must be one of low, mid, high')
      : exited(0, JSON.stringify(SNAPSHOT)),
  )
  on('ui.toast', () => ({ value: undefined }))
  on('command.run', () => ({ text: 'the skill ran' }))

  await $.command.run(typed('playbook-config'))
  await $.prompt.submit(sent('1'))
  await $.prompt.submit(sent('3'))
  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  const said = await band.find({ type: 'Text', text: /must be one of/ })
  expect(said?.props.color).toBe('red')
  await band.unmount()
})

test('a gate denial is toasted', async ($, on) => {
  const toasts: string[] = []
  on('ui.toast', (_, e) => {
    toasts.push(e.text)

    return { value: undefined }
  })
  on('tool.call', () => ({ deny: '[playbook:skill-gate] load engineering-governance first' }))

  await $.tool.call({ tool: 'Bash', command: 'echo x > src/a.ts' })
  expect(toasts).toContain('playbook skill-gate: blocked Bash')
})

test('output that only mentions a tag is not toasted as a block', async ($, on) => {
  const toasts: string[] = []
  on('ui.toast', (_, e) => {
    toasts.push(e.text)

    return { value: undefined }
  })
  on('tool.call', () => ({ result: {}, text: 'FAIL: expected [playbook:watch] in the output', isError: true }))

  await $.tool.call({ tool: 'Bash', command: 'python3 tests/run_all.py' })
  expect(toasts).toEqual([])
})
