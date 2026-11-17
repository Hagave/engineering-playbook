// State contract of the settings menu (hooks/config_panel.tsx).
//
// The menu draws what `scripts/playbook_config.py show --json` returns; it
// holds no list of keys or values of its own.

export type ConfigChoice = { value: string; label: string }

export type ConfigSetting = {
  key: string
  label: string
  description: string
  help: string
  value: string
  default: string
  choices: ConfigChoice[]
}

export type ConfigSnapshot = { settings: ConfigSetting[] }

// What the last action said: the script's two lines after a `set`, or why it failed.
export type PanelNotice = { text: string; isError: boolean }

// Which screen the band shows: the main list, one setting's choices, or none.
export type MenuScreen = { kind: 'main' } | { kind: 'setting'; key: string }

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
