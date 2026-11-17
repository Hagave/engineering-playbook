"""
Engineering Playbook — the fixed text a person reads, per language.

The Claude Code settings panel (hooks/config_panel.tsx) draws fixed text, not
text the model writes, so the model's "reply in the user's language" cannot
reach it. Every string the panel shows comes from here, through
`scripts/playbook_config.py show --json`, in the language the `language`
preference resolves to.

Text addressed to the model stays in English; only what the person reads is
here. A string missing in a language falls back to English, never to a blank.
"""

import os

# Stored value -> name shown in the language picker, each in its own language.
LANGUAGE_LABELS = {
    'auto': 'auto',
    'en': 'English',
    'pt-br': 'Português (Brasil)',
    'es': 'Español',
}
LANGUAGES = tuple(LANGUAGE_LABELS)

# The name the model is told to answer in, for prompts the panel sends.
LANGUAGE_NAMES = {
    'en': 'English',
    'pt-br': 'Brazilian Portuguese',
    'es': 'Spanish',
}

DEFAULT_LANGUAGE = 'en'

# Locale variables in the order POSIX gives them precedence for messages.
_LOCALE_VARS = ('LC_ALL', 'LC_MESSAGES', 'LANG')


def normalize(raw: str) -> str | None:
    """A user's spelling ('pt-BR', 'pt_br', 'EN') as the stored value, or None
    when it names no supported language."""
    value = (raw or '').strip().lower().replace('_', '-')
    return value if value in LANGUAGES else None


def from_locale(environ: dict) -> str:
    """The supported language a POSIX locale names ('pt_BR.UTF-8' -> 'pt-br');
    English for C, POSIX, an unset locale or a language not offered."""
    for var in _LOCALE_VARS:
        value = (environ.get(var) or '').strip()
        if not value:
            continue
        code = value.split('.')[0].split('@')[0].lower()
        if code.startswith('pt'):
            return 'pt-br'
        if code.startswith('es'):
            return 'es'
        return DEFAULT_LANGUAGE
    return DEFAULT_LANGUAGE


def resolve(preference: str, environ: dict | None = None) -> str:
    """The language text is shown in: the preference, or the locale for 'auto'."""
    if preference in LANGUAGE_NAMES:
        return preference
    return from_locale(os.environ if environ is None else environ)


STRINGS: dict[str, dict[str, str]] = {
    # Panel chrome.
    'title': {
        'en': 'Engineering Playbook settings',
        'pt-br': 'Configurações do Engineering Playbook',
        'es': 'Ajustes de Engineering Playbook',
    },
    'hint_main': {
        'en': 'type a number and Enter, 0 exits',
        'pt-br': 'digite um número e Enter, 0 sai',
        'es': 'escribe un número y Enter, 0 sale',
    },
    'hint_sub': {
        'en': 'type a number and Enter, 0 goes back',
        'pt-br': 'digite um número e Enter, 0 volta',
        'es': 'escribe un número y Enter, 0 vuelve',
    },
    'opened': {
        'en': 'Playbook settings are above the prompt: type a number and Enter (0 exits).',
        'pt-br': 'As configurações do playbook estão acima do prompt: digite um número e Enter (0 sai).',
        'es': 'Los ajustes del playbook están encima del prompt: escribe un número y Enter (0 sale).',
    },
    'no_entry': {
        'en': 'no entry {digit} on this screen.',
        'pt-br': 'não há opção {digit} nesta tela.',
        'es': 'no hay opción {digit} en esta pantalla.',
    },
    'current': {'en': 'current', 'pt-br': 'atual', 'es': 'actual'},
    'default': {'en': 'default', 'pt-br': 'padrão', 'es': 'por defecto'},
    'status': {'en': 'Status', 'pt-br': 'Status', 'es': 'Estado'},
    'learn': {'en': 'Learn', 'pt-br': 'Aprender', 'es': 'Aprender'},
    'diff': {'en': 'Diff panel', 'pt-br': 'Painel de diff', 'es': 'Panel de diff'},
    'back': {'en': 'Back', 'pt-br': 'Voltar', 'es': 'Volver'},
    'exit': {'en': 'Exit', 'pt-br': 'Sair', 'es': 'Salir'},
    # red-lab-playbook: the name is kept in every language; the words after it
    # say what it is, because a bare "lab" told the person nothing.
    'lab': {
        'en': 'red-lab-playbook: attack your own app in an isolated local copy',
        'pt-br': 'red-lab-playbook: ataca a sua própria app numa cópia local isolada',
        'es': 'red-lab-playbook: ataca tu propia app en una copia local aislada',
    },
    'lab_title': {'en': 'red-lab-playbook', 'pt-br': 'red-lab-playbook', 'es': 'red-lab-playbook'},
    'lab_help': {
        'en': ('Security scanners (OWASP ZAP, sqlmap) run against your own app inside an '
               'isolated copy of its Docker Compose stack; nothing outside that copy can be '
               'reached. The model runs the step you pick and asks for the service, port or '
               'endpoint it needs.'),
        'pt-br': ('Scanners de segurança (OWASP ZAP, sqlmap) atacam a sua própria aplicação '
                  'dentro de uma cópia isolada da stack Docker Compose; nada fora dessa cópia é '
                  'alcançável. O modelo executa o passo escolhido e pergunta o serviço, a porta '
                  'ou o endpoint de que precisar.'),
        'es': ('Escáneres de seguridad (OWASP ZAP, sqlmap) atacan tu propia aplicación dentro de '
               'una copia aislada de su stack de Docker Compose; nada fuera de esa copia es '
               'alcanzable. El modelo ejecuta el paso elegido y pregunta el servicio, el puerto '
               'o el endpoint que necesite.'),
    },
    'lab_setup': {
        'en': 'Set up (check Docker, download ZAP, build sqlmap)',
        'pt-br': 'Preparar (verifica o Docker, baixa o ZAP, compila o sqlmap)',
        'es': 'Preparar (comprueba Docker, descarga ZAP, compila sqlmap)',
    },
    'lab_passive': {
        'en': 'Passive scan (ZAP baseline, reads only)',
        'pt-br': 'Scan passivo (ZAP baseline, só observa)',
        'es': 'Escaneo pasivo (ZAP baseline, solo observa)',
    },
    'lab_active': {
        'en': 'Active scan (ZAP sends attack payloads)',
        'pt-br': 'Scan ativo (ZAP envia payloads de ataque)',
        'es': 'Escaneo activo (ZAP envía payloads de ataque)',
    },
    'lab_sqlmap': {
        'en': 'SQL injection scan (sqlmap, one endpoint)',
        'pt-br': 'Scan de SQL injection (sqlmap, um endpoint)',
        'es': 'Escaneo de inyección SQL (sqlmap, un endpoint)',
    },
    'lab_uninstall': {
        'en': 'Remove the scanner images',
        'pt-br': 'Remover as imagens dos scanners',
        'es': 'Eliminar las imágenes de los escáneres',
    },
    # Setting names, as the panel lists them.
    'label_prose': {'en': 'Prose level', 'pt-br': 'Nível de prosa', 'es': 'Nivel de prosa'},
    'label_watch': {
        'en': 'Known-failure warnings',
        'pt-br': 'Avisos de falhas conhecidas',
        'es': 'Avisos de fallos conocidos',
    },
    'label_claude_md_budget': {
        'en': 'CLAUDE.md budget (characters)',
        'pt-br': 'Orçamento do CLAUDE.md (caracteres)',
        'es': 'Presupuesto de CLAUDE.md (caracteres)',
    },
    'label_context_budget': {
        'en': 'Context budget (tokens)',
        'pt-br': 'Orçamento de contexto (tokens)',
        'es': 'Presupuesto de contexto (tokens)',
    },
    'label_language': {'en': 'Language', 'pt-br': 'Idioma', 'es': 'Idioma'},
    # What each setting does, shown under its name on its own screen.
    'help_prose': {
        'en': 'How much explanation a response carries. Applies from the next session.',
        'pt-br': 'Quanta explicação uma resposta traz. Vale a partir da próxima sessão.',
        'es': 'Cuánta explicación lleva una respuesta. Se aplica desde la próxima sesión.',
    },
    'help_watch': {
        'en': ('Before a write or command that touches something that already failed in '
               'this project, show the recorded failure. Warns, never blocks.'),
        'pt-br': ('Antes de uma escrita ou comando que toca algo que já falhou neste projeto, '
                  'mostra a falha registrada. Avisa, nunca bloqueia.'),
        'es': ('Antes de una escritura o comando que toca algo que ya falló en este proyecto, '
               'muestra el fallo registrado. Avisa, nunca bloquea.'),
    },
    'help_claude_md_budget': {
        'en': ('Blocks the end of a turn that grew the project CLAUDE.md past this '
               'size: the file is re-sent to every session and every subagent.'),
        'pt-br': ('Bloqueia o fim de um turno que fez o CLAUDE.md do projeto passar deste '
                  'tamanho: o arquivo é reenviado a toda sessão e a todo subagente.'),
        'es': ('Bloquea el final de un turno que hizo crecer el CLAUDE.md del proyecto más allá '
               'de este tamaño: el archivo se reenvía a cada sesión y a cada subagente.'),
    },
    'help_context_budget': {
        'en': ('Past this much context, the model is told once to finish the step, '
               'write the handoff and ask for /clear. Never blocks.'),
        'pt-br': ('Passado este tanto de contexto, o modelo é avisado uma vez para concluir o '
                  'passo, escrever o handoff e pedir /clear. Nunca bloqueia.'),
        'es': ('Pasado este contexto, se indica una vez al modelo que termine el paso, escriba '
               'el handoff y pida /clear. Nunca bloquea.'),
    },
    'help_language': {
        'en': ('Language of this menu and its messages. auto follows the system locale. '
               'Replies follow the language you write in.'),
        'pt-br': ('Idioma deste menu e das mensagens dele. auto segue o locale do sistema. '
                  'As respostas seguem o idioma em que você escreve.'),
        'es': ('Idioma de este menú y sus mensajes. auto sigue el locale del sistema. '
               'Las respuestas siguen el idioma en que escribes.'),
    },
    'off': {'en': 'off', 'pt-br': 'desligado', 'es': 'apagado'},
    'on': {'en': 'on', 'pt-br': 'ligado', 'es': 'encendido'},
    # When a just-set preference takes effect (scripts/playbook_config.py effect_note).
    'effect_prose': {
        'en': ('Applies from the next session start or compaction: the level is read '
               'once at session start, so this session keeps the level it started with.'),
        'pt-br': ('Vale a partir do próximo início de sessão ou compactação: o nível é lido '
                  'uma vez no início, então esta sessão mantém o nível com que começou.'),
        'es': ('Se aplica desde el próximo inicio de sesión o compactación: el nivel se lee '
               'una vez al inicio, así que esta sesión mantiene el nivel con que empezó.'),
    },
    'effect_watch_on': {
        'en': ('Warnings start now, at the next write or command that matches a recorded '
               'failure; the instruction telling the model how to read them changes at the '
               'next session start.'),
        'pt-br': ('Os avisos começam agora, na próxima escrita ou comando que coincida com uma '
                  'falha registrada; a instrução ao modelo sobre como lê-los muda no próximo '
                  'início de sessão.'),
        'es': ('Los avisos empiezan ahora, en la próxima escritura o comando que coincida con un '
               'fallo registrado; la instrucción al modelo sobre cómo leerlos cambia en el '
               'próximo inicio de sesión.'),
    },
    'effect_watch_off': {
        'en': ('Warnings stop now, at the next write or command that matches a recorded '
               'failure; the instruction telling the model how to read them changes at the '
               'next session start.'),
        'pt-br': ('Os avisos param agora; a instrução ao modelo sobre como lê-los muda no '
                  'próximo início de sessão.'),
        'es': ('Los avisos se detienen ahora; la instrucción al modelo sobre cómo leerlos '
               'cambia en el próximo inicio de sesión.'),
    },
    'effect_claude_md_budget_off': {
        'en': 'In force now: the CLAUDE.md size check is off.',
        'pt-br': 'Em vigor agora: a verificação de tamanho do CLAUDE.md está desligada.',
        'es': 'En vigor ahora: la comprobación de tamaño de CLAUDE.md está apagada.',
    },
    'effect_claude_md_budget': {
        'en': ('In force now: a turn that grows the project CLAUDE.md past {value} '
               'characters is blocked when it ends.'),
        'pt-br': ('Em vigor agora: um turno que fizer o CLAUDE.md do projeto passar de {value} '
                  'caracteres é bloqueado ao terminar.'),
        'es': ('En vigor ahora: un turno que haga crecer el CLAUDE.md del proyecto más allá de '
               '{value} caracteres se bloquea al terminar.'),
    },
    'effect_context_budget_off': {
        'en': 'In force now: the context budget is off.',
        'pt-br': 'Em vigor agora: o orçamento de contexto está desligado.',
        'es': 'En vigor ahora: el presupuesto de contexto está apagado.',
    },
    'effect_context_budget': {
        'en': ('In force now: past {value} context tokens the model is told, once per '
               'multiple, to finish the step and hand off.'),
        'pt-br': ('Em vigor agora: passados {value} tokens de contexto, o modelo é avisado, uma '
                  'vez por múltiplo, para concluir o passo e fazer o handoff.'),
        'es': ('En vigor ahora: pasados {value} tokens de contexto, se indica al modelo, una vez '
               'por múltiplo, que termine el paso y haga el handoff.'),
    },
    'effect_language': {
        'en': 'In force now: this menu is shown in {value}.',
        'pt-br': 'Em vigor agora: este menu aparece em {value}.',
        'es': 'En vigor ahora: este menú se muestra en {value}.',
    },
    'effect_generic': {'en': 'In force now.', 'pt-br': 'Em vigor agora.', 'es': 'En vigor ahora.'},
    # The output filter's totals line (hooks/output_filter.py --report).
    'filter_empty': {
        'en': 'Output filter: on, nothing filtered yet',
        'pt-br': 'Filtro de saída: ligado, nada filtrado ainda',
        'es': 'Filtro de salida: encendido, nada filtrado aún',
    },
    'filter_totals': {
        'en': ('Output filter: {count} outputs filtered, {before} -> {after} chars '
               '(-{percent} %, about {tokens} tokens)'),
        'pt-br': ('Filtro de saída: {count} saídas filtradas, {before} -> {after} caracteres '
                  '(-{percent} %, cerca de {tokens} tokens)'),
        'es': ('Filtro de salida: {count} salidas filtradas, {before} -> {after} caracteres '
               '(-{percent} %, unos {tokens} tokens)'),
    },
}

# The panel's own strings, sent whole in `show --json` as `ui`.
UI_KEYS = (
    'title', 'hint_main', 'hint_sub', 'opened', 'no_entry', 'current', 'default',
    'status', 'learn', 'lab', 'diff', 'back', 'exit',
    'lab_title', 'lab_help', 'lab_setup', 'lab_passive', 'lab_active', 'lab_sqlmap',
    'lab_uninstall',
)


def text(language: str, key: str, **values: object) -> str:
    """`key` in `language`, English when that language lacks it, with `values`
    filled in."""
    entry = STRINGS[key]
    template = entry.get(language) or entry[DEFAULT_LANGUAGE]
    return template.format(**values) if values else template


def number(value: int, language: str) -> str:
    """A count with the thousands separator the language reads (40,000 / 40.000)."""
    shown = f'{value:,}'
    return shown if language == DEFAULT_LANGUAGE else shown.replace(',', '.')


def ui(language: str) -> dict[str, str]:
    """Every string the panel draws on its own, in `language`."""
    return {key: text(language, key) for key in UI_KEYS}
