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

const UI = {
  title: 'Engineering Playbook settings',
  hint_main: 'type a number and Enter, 0 exits',
  hint_sub: 'type a number and Enter, 0 goes back',
  opened: 'Playbook settings are above the prompt: type a number and Enter (0 exits).',
  no_entry: 'no entry {digit} on this screen.',
  current: 'current',
  default: 'default',
  status: 'Status',
  learn: 'Learn',
  lab: 'red-lab-playbook: attack your own app in an isolated local copy',
  diff: 'Diff panel',
  back: 'Back',
  exit: 'Exit',
  custom: 'Custom value',
  lab_title: 'red-lab-playbook',
  lab_help: 'Security scanners run against your own app inside an isolated copy of its stack.',
  lab_setup: 'Set up',
  lab_passive: 'Passive scan',
  lab_active: 'Active scan',
  lab_sqlmap: 'SQL injection scan',
  lab_uninstall: 'Remove the scanner images',
  update_now: 'Update now',
  update_check_now: 'Check for updates',
}

const SNAPSHOT = {
  language: 'en',
  language_name: 'English',
  version: '4.2.1',
  update: { available: false, latest: '' },
  ui: UI,
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
      custom: false,
      group: '',
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
      custom: false,
      group: '',
    },
  ],
  groups: [] as { key: string; label: string; help: string }[],
}

const BUDGET = {
  key: 'context_budget',
  label: 'Context budget',
  description: 'context tokens past which to wrap up',
  help: 'Past this much context, the model is told once to hand off.',
  value: '550000',
  default: '550000',
  choices: [
    { value: '0', label: 'off' },
    { value: '550000', label: '550k' },
    { value: '650000', label: '650k' },
    { value: '850000', label: '850k' },
  ],
  custom: true,
  group: '',
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

// Two settings in SNAPSHOT: Status 3, Learn 4, red-lab-playbook 5, Diff panel 6.
const LAB_DIGIT = '5'

test('the main menu lists red-lab-playbook with what it is, and it opens its steps', async ($, on) => {
  on('process.run', () => exited(0, JSON.stringify(SNAPSHOT)))
  on('command.run', () => ({ text: 'the skill ran' }))
  await $.command.run(typed('playbook-config'))

  const main = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await main.find({ type: 'Button', key: `entry-${LAB_DIGIT}` }))?.props.label).toBe(UI.lab)
  expect((await main.find({ type: 'Button', key: 'entry-6' }))?.props.label).toBe('Diff panel')
  await main.unmount()

  const opened = await $.prompt.submit(sent(LAB_DIGIT))
  expect(opened.drop).toBeTruthy()
  const lab = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await lab.find({ type: 'Text', text: /red-lab-playbook/ }))?.text).toBeTruthy()
  expect((await lab.find({ type: 'Text', text: /isolated copy/ }))?.text).toBe(UI.lab_help)
  const labels: unknown[] = []
  for (const digit of ['1', '2', '3', '4', '5', '0']) {
    labels.push((await lab.find({ type: 'Button', key: `entry-${digit}` }))?.props.label)
  }
  expect(labels).toEqual([UI.lab_setup, UI.lab_passive, UI.lab_active, UI.lab_sqlmap, UI.lab_uninstall, 'Back'])
  await lab.unmount()
})

test('a setting that takes a typed value offers it after its buttons and hands it to the model', async ($, on) => {
  const toModel: string[] = []
  const withBudget = { ...SNAPSHOT, settings: [...SNAPSHOT.settings, BUDGET] }
  on('process.run', () => exited(0, JSON.stringify(withBudget)))
  on('prompt.submit', (_, e) => {
    toModel.push(e.text)

    return { text: e.text }
  })
  on('command.run', () => ({ text: 'the skill ran' }))
  await $.command.run(typed('playbook-config'))
  await $.prompt.submit(sent('3'))

  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  const custom = await band.find({ type: 'Button', key: 'entry-5' })
  expect(custom?.props.label).toBe('Custom value')

  // Four values, then the typed one: no `set` runs, the model asks for the number.
  await $.prompt.submit(sent('5'))
  expect(toModel.at(-1)).toContain('context_budget to a custom value')
})

test('a setting without a typed value has only its buttons and Back', async ($, on) => {
  on('process.run', () => exited(0, JSON.stringify(SNAPSHOT)))
  on('command.run', () => ({ text: 'the skill ran' }))
  await $.command.run(typed('playbook-config'))
  await $.prompt.submit(sent('2'))

  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await band.find({ type: 'Button', key: 'entry-3' })).toBeUndefined()
})

test('every red-lab-playbook step closes the menu and hands that step to the model', async ($, on) => {
  const toModel: string[] = []
  on('process.run', () => exited(0, JSON.stringify(SNAPSHOT)))
  on('prompt.submit', (_, e) => {
    toModel.push(e.text)

    return { text: e.text }
  })
  on('command.run', () => ({ text: 'the skill ran' }))

  const steps = ['setup', 'passive', 'active', 'sqlmap', 'uninstall']
  for (const [index, step] of steps.entries()) {
    await $.command.run(typed('playbook-config'))
    await $.prompt.submit(sent(LAB_DIGIT))
    await $.prompt.submit(sent(String(index + 1)))
    expect(toModel.at(-1)).toContain(`red-lab-playbook step "${step}"`)
  }
  expect(toModel.length).toBe(steps.length)

  // The menu closed with the step: a digit after it is the person's, for the model.
  await $.prompt.submit(sent('1'))
  expect(toModel.at(-1)).toBe('1')
})

test('0 on the red-lab-playbook screen goes back to the main menu', async ($, on) => {
  on('process.run', () => exited(0, JSON.stringify(SNAPSHOT)))
  on('command.run', () => ({ text: 'the skill ran' }))
  await $.command.run(typed('playbook-config'))
  await $.prompt.submit(sent(LAB_DIGIT))
  await $.prompt.submit(sent('0'))

  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await band.find({ type: 'Button', key: 'entry-0' }))?.props.label).toBe('Exit')
  await band.unmount()
})

const PT_UI = {
  ...UI,
  title: 'Configurações do Engineering Playbook',
  hint_main: 'digite um número e Enter, 0 sai',
  opened: 'As configurações do playbook estão acima do prompt: digite um número e Enter (0 sai).',
  no_entry: 'não há opção {digit} nesta tela.',
  default: 'padrão',
  learn: 'Aprender',
  exit: 'Sair',
}
const PT_SNAPSHOT = {
  ...SNAPSHOT,
  language: 'pt-br',
  language_name: 'Brazilian Portuguese',
  ui: PT_UI,
  settings: SNAPSHOT.settings.map(setting =>
    setting.key === 'prose' ? { ...setting, label: 'Nível de prosa' } : setting,
  ),
}

test('the menu draws the strings of the language the script resolved', async ($, on) => {
  on('process.run', (_, e) => exited(0, e.argv.includes('--report') ? REPORT : JSON.stringify(PT_SNAPSHOT)))
  on('command.run', () => ({ text: 'the skill ran' }))

  const ran = await $.command.run(typed('playbook-config'))
  expect(ran.text).toBe(PT_UI.opened)

  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await band.find({ type: 'Text', text: /Configurações/ }))?.text).toBeTruthy()
  expect((await band.find({ type: 'Button', key: 'entry-1' }))?.props.label).toBe('Nível de prosa: low (padrão mid)')
  expect((await band.find({ type: 'Button', key: 'entry-0' }))?.props.label).toBe('Sair')
  await band.unmount()

  const missing = await $.prompt.submit(sent('9'))
  expect(missing.drop).toBe('Configurações do Engineering Playbook: não há opção 9 nesta tela.')
})

test('a prompt the menu hands the model asks for the answer in the person language', async ($, on) => {
  const toModel: string[] = []
  on('process.run', () => exited(0, JSON.stringify(PT_SNAPSHOT)))
  on('prompt.submit', (_, e) => {
    toModel.push(e.text)

    return { text: e.text }
  })
  on('command.run', () => ({ text: 'the skill ran' }))

  await $.command.run(typed('playbook-config'))
  await $.prompt.submit(sent('4'))
  expect(toModel.at(-1)).toContain('engineering-learn')
  expect(toModel.at(-1)).toContain('Answer in Brazilian Portuguese.')
})

const GROUPED = {
  ...SNAPSHOT,
  settings: [
    ...SNAPSHOT.settings,
    { ...BUDGET, group: 'budgets' },
    { ...BUDGET, key: 'claude_md_budget', label: 'CLAUDE.md budget', custom: false, group: 'budgets' },
    {
      ...SNAPSHOT.settings[1],
      key: 'reports',
      label: 'Deviation reports to the maintainer',
      group: '',
    },
  ],
  groups: [{ key: 'budgets', label: 'Cost budgets', help: 'The CLAUDE.md size and the context size.' }],
}

test('a group of settings is one main row that opens a screen of its own', async ($, on) => {
  const sets: string[][] = []
  on('process.run', (_, e) => {
    if (e.argv.includes('set')) {
      sets.push(e.argv.slice(-2))
      return exited(0, `[playbook:config] done\n${EFFECT}`)
    }
    return exited(0, e.argv.includes('--report') ? REPORT : JSON.stringify(GROUPED))
  })
  on('command.run', () => ({ text: 'the skill ran' }))
  await $.command.run(typed('playbook-config'))

  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await band.find({ type: 'Button', key: 'entry-3' }))?.props.label).toBe('Cost budgets')
  expect((await band.find({ type: 'Button', key: 'entry-4' }))?.props.label).toBe(
    'Deviation reports to the maintainer: off',
  )
  expect((await band.find({ type: 'Button', key: 'entry-5' }))?.props.label).toBe('Status')
  await band.unmount()

  await $.prompt.submit(sent('3'))
  const group = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await group.find({ type: 'Text', text: /Cost budgets/ }))?.text).toBeTruthy()
  expect((await group.find({ type: 'Button', key: 'entry-1' }))?.props.label).toContain('Context budget')
  expect((await group.find({ type: 'Button', key: 'entry-2' }))?.props.label).toContain('CLAUDE.md budget')
  expect((await group.find({ type: 'Button', key: 'entry-0' }))?.props.label).toBe('Back')
  await group.unmount()

  await $.prompt.submit(sent('2'))
  await $.prompt.submit(sent('1'))
  expect(sets.at(-1)).toEqual(['claude_md_budget', '0'])
})

test('0 on a group screen goes back to the main menu', async ($, on) => {
  on('process.run', (_, e) => exited(0, e.argv.includes('--report') ? REPORT : JSON.stringify(GROUPED)))
  on('command.run', () => ({ text: 'the skill ran' }))
  await $.command.run(typed('playbook-config'))
  await $.prompt.submit(sent('3'))
  await $.prompt.submit(sent('0'))

  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await band.find({ type: 'Button', key: 'entry-1' }))?.props.label).toContain('Prose level')
})

test('settings the script cannot read leave the request to the skill', async ($, on) => {
  on('process.run', () => exited(1, '', 'python3: not found'))
  on('command.run', () => ({ text: 'the skill ran' }))

  const ran = await $.command.run(typed('playbook-config'))
  expect(ran.text).toBe('the skill ran')
})

const ONLINE = {
  ...SNAPSHOT,
  settings: [
    ...SNAPSHOT.settings,
    { ...SNAPSHOT.settings[1], key: 'update_check', label: 'Update check', value: 'on', default: 'on', group: 'online' },
    { ...SNAPSHOT.settings[1], key: 'reports', label: 'Deviation reports', group: 'online' },
  ],
  groups: [{ key: 'online', label: 'Online', help: 'What the playbook asks of the network.' }],
}
const NEWER = { ...ONLINE, update: { available: true, latest: '4.3.0' } }

test('the main title carries the installed version', async ($, on) => {
  on('process.run', (_, e) => exited(0, e.argv.includes('--report') ? REPORT : JSON.stringify(SNAPSHOT)))
  on('command.run', () => ({ text: 'the skill ran' }))
  await $.command.run(typed('playbook-config'))

  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await band.find({ type: 'Text', text: /Engineering Playbook settings v4\.2\.1/ }))?.text).toBeTruthy()
  await band.unmount()
})

test('a newer release is named on the Online row, and its screen offers the update before the check', async ($, on) => {
  on('process.run', (_, e) => exited(0, e.argv.includes('--report') ? REPORT : JSON.stringify(NEWER)))
  on('command.run', () => ({ text: 'the skill ran' }))
  await $.command.run(typed('playbook-config'))

  const main = await $.ui.mount({ ...BAND, surface: 'terminal' })
  const row = await main.find({ type: 'Button', key: 'entry-3' })
  expect(row?.props.label).toBe('Online (4.3.0)')
  await main.unmount()

  await $.prompt.submit(sent('3'))
  const online = await $.ui.mount({ ...BAND, surface: 'terminal' })
  const labels: unknown[] = []
  for (const digit of ['1', '2', '3', '4', '0']) {
    labels.push((await online.find({ type: 'Button', key: `entry-${digit}` }))?.props.label)
  }
  expect(labels[2]).toBe('Update now (4.3.0)')
  expect(labels[3]).toBe('Check for updates')
  expect(labels[4]).toBe('Back')
  await online.unmount()
})

test('without a newer release the Online screen offers only the check', async ($, on) => {
  on('process.run', (_, e) => exited(0, e.argv.includes('--report') ? REPORT : JSON.stringify(ONLINE)))
  on('command.run', () => ({ text: 'the skill ran' }))
  await $.command.run(typed('playbook-config'))
  await $.prompt.submit(sent('3'))

  const online = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await online.find({ type: 'Button', key: 'entry-3' }))?.props.label).toBe('Check for updates')
  expect(await online.find({ type: 'Button', key: 'entry-4' })).toBeUndefined()
  await online.unmount()
})

const APPLIED = {
  text: 'Updated. Reloading the plugins to apply it.',
  manual_text: 'Updated. Type /reload-plugins to apply it in this session.',
  is_error: false,
  reload: true,
}

test('Update now from a button press runs the script, then reloads the plugins in this session', async ($, on) => {
  const argvs: string[][] = []
  const commands: string[] = []
  on('process.run', (_, e) => {
    argvs.push([...e.argv])
    if (e.argv.includes('update')) {
      return exited(0, JSON.stringify(APPLIED))
    }

    return exited(0, e.argv.includes('--report') ? REPORT : JSON.stringify(NEWER))
  })
  on('ui.toast', () => ({ value: undefined }))
  on('command.run', (_, e) => {
    commands.push(e.command)

    return { text: 'ran' }
  })
  await $.command.run(typed('playbook-config'))
  await $.prompt.submit(sent('3'))

  const online = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await online.press({ key: 'entry-3' })
  await online.unmount()

  expect(argvs.some(argv => argv.includes('update') && argv.includes('apply') && argv.includes('--json'))).toBe(true)
  expect(commands).toContain('reload-plugins')
})

test('Update now from a typed digit cannot run the reload, so it tells the person to type it', async ($, on) => {
  const commands: string[] = []
  on('process.run', (_, e) => {
    if (e.argv.includes('update')) {
      return exited(0, JSON.stringify(APPLIED))
    }

    return exited(0, e.argv.includes('--report') ? REPORT : JSON.stringify(NEWER))
  })
  on('ui.toast', () => ({ value: undefined }))
  on('command.run', (_, e) => {
    commands.push(e.command)

    return { text: 'ran' }
  })
  await $.command.run(typed('playbook-config'))
  await $.prompt.submit(sent('3'))
  const dropped = await $.prompt.submit(sent('3'))

  expect(commands).not.toContain('reload-plugins')
  expect(JSON.stringify(dropped)).toContain('/reload-plugins')
})

test('a failed update is shown in red and nothing is reloaded', async ($, on) => {
  const commands: string[] = []
  on('process.run', (_, e) => {
    if (e.argv.includes('update')) {
      return exited(0, JSON.stringify({ text: 'The update failed: not found', manual_text: 'The update failed: not found', is_error: true, reload: false }))
    }

    return exited(0, e.argv.includes('--report') ? REPORT : JSON.stringify(NEWER))
  })
  on('ui.toast', () => ({ value: undefined }))
  on('command.run', (_, e) => {
    commands.push(e.command)

    return { text: 'ran' }
  })
  await $.command.run(typed('playbook-config'))
  await $.prompt.submit(sent('3'))
  await $.prompt.submit(sent('3'))

  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  const said = await band.find({ type: 'Text', text: /The update failed/ })
  expect(said?.props.color).toBe('red')
  expect(commands).not.toContain('reload-plugins')
  await band.unmount()
})

test('Check for updates asks the script and shows its answer', async ($, on) => {
  const argvs: string[][] = []
  on('process.run', (_, e) => {
    argvs.push([...e.argv])
    if (e.argv.includes('update')) {
      return exited(0, JSON.stringify({
          text: 'Up to date: 4.2.1 is the newest release.',
          manual_text: 'Up to date: 4.2.1 is the newest release.',
          is_error: false,
          reload: false,
        }))
    }

    return exited(0, e.argv.includes('--report') ? REPORT : JSON.stringify(ONLINE))
  })
  on('ui.toast', () => ({ value: undefined }))
  on('command.run', () => ({ text: 'the skill ran' }))
  await $.command.run(typed('playbook-config'))
  await $.prompt.submit(sent('3'))
  await $.prompt.submit(sent('3'))

  expect(argvs.some(argv => argv.includes('update') && argv.includes('check'))).toBe(true)
  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await band.find({ type: 'Text', text: /Up to date/ }))?.text).toBeTruthy()
  await band.unmount()
})
