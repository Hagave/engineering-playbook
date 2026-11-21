// State contract of the settings menu (hooks/config_panel.tsx).
//
// The menu draws what `scripts/playbook_config.py show --json` returns; it
// holds no list of keys, values or text of its own.

export type ConfigChoice = { value: string; label: string }

export type ConfigSetting = {
  key: string
  label: string
  description: string
  help: string
  value: string
  default: string
  choices: ConfigChoice[]
  // Whether the setting screen also offers a typed value, which the model asks for.
  custom: boolean
  // The key of the group the main screen lists it under ('' for its own row).
  group: string
}

// Settings the main screen lists as one row that opens their own screen.
export type ConfigGroup = { key: string; label: string; help: string }

// The panel's own strings, already in the person's language
// (hooks/playbook_i18n.py, UI_KEYS).
export type PanelText = {
  title: string
  hint_main: string
  hint_sub: string
  opened: string
  // Holds `{digit}`.
  no_entry: string
  current: string
  default: string
  status: string
  learn: string
  lab: string
  diff: string
  back: string
  exit: string
  custom: string
  lab_title: string
  lab_help: string
  lab_setup: string
  lab_passive: string
  lab_active: string
  lab_sqlmap: string
  lab_uninstall: string
}

export type ConfigSnapshot = {
  // 'en', 'pt-br' or 'es': what the `language` setting resolved to.
  language: string
  // That language's English name, for the prompts the menu hands the model.
  language_name: string
  ui: PanelText
  settings: ConfigSetting[]
  groups: ConfigGroup[]
}

// What the last action said: the script's two lines after a `set`, or why it failed.
export type PanelNotice = { text: string; isError: boolean }

// Which screen the band shows: the main list, a group's settings, one
// setting's choices, the red-lab-playbook steps, or none.
export type MenuScreen =
  | { kind: 'main' }
  | { kind: 'group'; group: string }
  | { kind: 'setting'; key: string }
  | { kind: 'lab' }

declare module 'claude-code' {
  interface PluginState {
    'engineering-playbook': {
      snapshot: Shaped<ConfigSnapshot | null>
      notice: Shaped<PanelNotice | null>
      // `hooks/output_filter.py --report`, its one line; null when it failed.
      filterReport: Shaped<string | null>
      // null while the menu is closed.
      screen: Shaped<MenuScreen | null>
    }
  }
}
