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

import json
import os
import re
import tempfile

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


STATE_DIR = os.path.expanduser('~/.claude/hooks')
_REMEMBERED_FILE = '.playbook-language.json'

# What the words of each language give away. Words two of the languages share
# (para, como, porque, esta) are left out: they decide nothing.
def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


_MARKERS: dict[str, frozenset[str]] = {
    'en': _words('the is are was were and to of that with this for it you not have has be '
                 'what how why when where which can could should would do does did if but '
                 'from they we will my your please'),
    'pt-br': _words('não você vocês isso uma são também então pra já pelo pela dos das nos '
                    'ao às ainda esse essa isto aqui só mesmo sim ele ela nós meu seu '
                    'minha tem foi estão com mas quando agora fazer esses essas deste desta '
                    'nesse nessa onde seja pode podem preciso quero eu dele dela ter tudo'),
    'es': _words('qué cómo también pero muy hay el los las usted ustedes con esto eso ahora '
                 'cuando sí aquí yo mi tu y una un por favor hacer fue son puede pueden '
                 'quiero necesito tengo del estos donde todo'),
}
_DIACRITICS = {'pt-br': 'ãõç', 'es': 'ñ¿¡'}
_WORD_RE = re.compile(r"[a-zà-ÿñ¿¡']+")
_DETECT_MIN_HITS = 3
_DETECT_MAX_CHARS = 4000


def detect(text: str) -> str | None:
    """The language a typed message is written in, or None when it does not say:
    too short, mixed, or in a language the playbook does not offer.

    A word list, not a model: the hook runs on every prompt and must stay in
    milliseconds. The trade-off is accepted knowingly: a short prompt ("sim")
    settles nothing and the language stays as it was, and a message needs a
    clear lead (twice the runner-up) so pasted English inside Portuguese prose
    does not flip it."""
    lowered = text[:_DETECT_MAX_CHARS].lower()
    words = _WORD_RE.findall(lowered)
    scores = {language: sum(word in markers for word in words)
              for language, markers in _MARKERS.items()}
    for language, marks in _DIACRITICS.items():
        scores[language] += 2 * len({char for char in lowered if char in marks})
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    return best if top >= _DETECT_MIN_HITS and top >= 2 * second else None


def remember(language: str, state_dir: str = STATE_DIR) -> None:
    """Keep the language the user writes in, for the text the playbook shows
    that no model writes (dialogs, the panel). Best effort: a state directory
    that cannot be written loses a convenience, never a prompt."""
    if language not in LANGUAGE_NAMES or remembered(state_dir) == language:
        return
    path = os.path.join(state_dir, _REMEMBERED_FILE)
    try:
        os.makedirs(state_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', dir=state_dir, delete=False, suffix='.tmp') as tmp:
            json.dump({'language': language}, tmp)
        os.replace(tmp.name, path)
    except OSError:
        pass


def remembered(state_dir: str = STATE_DIR) -> str | None:
    """The language `remember` kept, None when there is none or it is unreadable."""
    try:
        with open(os.path.join(state_dir, _REMEMBERED_FILE)) as handle:
            value = json.load(handle).get('language')
    except (OSError, ValueError, AttributeError):
        return None
    return value if value in LANGUAGE_NAMES else None


def resolve(preference: str, environ: dict | None = None, state_dir: str = STATE_DIR) -> str:
    """The language text is shown in: the preference; for 'auto', the language
    the user last wrote in, then the locale. A caller that passes `environ` asks
    for the locale alone."""
    if preference in LANGUAGE_NAMES:
        return preference
    if environ is None:
        return remembered(state_dir) or from_locale(os.environ)
    return from_locale(environ)


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
    'custom': {'en': 'Custom value', 'pt-br': 'Personalizado', 'es': 'Personalizado'},
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
    'label_reports': {
        'en': 'Deviation reports to the maintainer',
        'pt-br': 'Relatos de desvio ao mantenedor',
        'es': 'Informes de desvío al mantenedor',
    },
    # A group of settings, as the main screen lists it, and its screen's help.
    'group_budgets': {'en': 'Cost budgets', 'pt-br': 'Orçamentos de custo', 'es': 'Presupuestos de costo'},
    'group_reminders': {
        'en': 'Warnings and reminders',
        'pt-br': 'Avisos e lembretes',
        'es': 'Avisos y recordatorios',
    },
    'group_reminders_help': {
        'en': 'What the playbook tells you before a risky action, and what it keeps for later.',
        'pt-br': 'O que o playbook avisa antes de uma ação arriscada, e o que guarda para depois.',
        'es': 'Lo que el playbook avisa antes de una acción arriesgada, y lo que guarda para después.',
    },
    'label_temporary_data': {
        'en': 'Temporary data panel',
        'pt-br': 'Painel Temporary data',
        'es': 'Panel Temporary data',
    },
    'help_temporary_data': {
        'en': ('In auto mode, a deletion you decline on the button above the chat is kept in '
               'the Temporary data panel of this project, so you can remove it later. Off: a '
               'declined deletion is only dropped. On by default.'),
        'pt-br': ('No modo auto, uma remoção que você recusa no botão acima do chat fica no '
                  'painel Temporary data deste projeto, para você apagar depois. Desligado: a '
                  'remoção recusada só é descartada. Ligado por padrão.'),
        'es': ('En modo auto, un borrado que rechazas en el botón sobre el chat queda en el '
               'panel Temporary data de este proyecto, para borrarlo después. Apagado: el '
               'borrado rechazado solo se descarta. Encendido por defecto.'),
    },
    'effect_temporary_data_on': {
        'en': 'In force now: a deletion you decline is kept in the Temporary data panel.',
        'pt-br': 'Em vigor agora: uma remoção recusada fica no painel Temporary data.',
        'es': 'En vigor ahora: un borrado rechazado queda en el panel Temporary data.',
    },
    'effect_temporary_data_off': {
        'en': 'In force now: a deletion you decline is dropped; what the panel holds stays.',
        'pt-br': 'Em vigor agora: uma remoção recusada é descartada; o que o painel já tem fica.',
        'es': 'En vigor ahora: un borrado rechazado se descarta; lo que el panel ya tiene queda.',
    },
    'group_budgets_help': {
        'en': 'The CLAUDE.md size and the context size past which the playbook speaks up.',
        'pt-br': 'O tamanho do CLAUDE.md e o tamanho de contexto a partir dos quais o playbook avisa.',
        'es': 'El tamaño de CLAUDE.md y el tamaño de contexto a partir de los cuales el playbook avisa.',
    },
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
    'help_reports': {
        'en': ('When a model reports a deviation from a skill rule, open a GitHub issue for '
               'the maintainer with your account (gh), or show a pre-filled link. It sends only '
               'the skill, its version, the rule as the skill words it, the model and the '
               'playbook version: never your folder, code, session or reasoning. Off by default.'),
        'pt-br': ('Quando um modelo relata um desvio de uma regra de skill, abre uma issue no '
                  'GitHub para o mantenedor com a sua conta (gh), ou mostra um link '
                  'pré-preenchido. Envia só a skill, a versão, a regra como a skill a escreve, o '
                  'modelo e a versão do playbook: nunca a sua pasta, código, sessão ou raciocínio. '
                  'Desligado por padrão.'),
        'es': ('Cuando un modelo informa un desvío de una regla de skill, abre un issue en GitHub '
               'para el mantenedor con tu cuenta (gh), o muestra un enlace prellenado. Envía solo '
               'la skill, su versión, la regla como la skill la escribe, el modelo y la versión '
               'del playbook: nunca tu carpeta, código, sesión o razonamiento. Apagado por '
               'defecto.'),
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
    'effect_reports_on': {
        'en': ('In force from the next deviation a model reports: it is sent as an issue with '
               'your GitHub account, or shown as a link when gh is missing or logged out.'),
        'pt-br': ('Vale a partir do próximo desvio que um modelo relatar: ele sai como issue com '
                  'a sua conta do GitHub, ou aparece como link quando o gh falta ou está sem login.'),
        'es': ('En vigor desde el próximo desvío que informe un modelo: sale como issue con tu '
               'cuenta de GitHub, o aparece como enlace cuando falta gh o no tiene sesión.'),
    },
    'effect_reports_off': {
        'en': 'In force now: nothing is sent; deviations stay in the local journal only.',
        'pt-br': 'Em vigor agora: nada é enviado; os desvios ficam só no diário local.',
        'es': 'En vigor ahora: no se envía nada; los desvíos quedan solo en el diario local.',
    },
    'label_update_check': {
        'en': 'Update check',
        'pt-br': 'Verificação de atualização',
        'es': 'Búsqueda de actualizaciones',
    },
    'help_update_check': {
        'en': ('Once a day the playbook asks GitHub for the newest release and tells you, once '
               'per version, when it is newer than the one installed. The request carries nothing '
               'about you, your machine or your project. On by default.'),
        'pt-br': ('Uma vez por dia o playbook pergunta ao GitHub qual é a release mais nova e '
                  'avisa, uma vez por versão, quando ela é mais nova que a instalada. O pedido '
                  'não leva nada sobre você, a sua máquina ou o seu projeto. Ligado por padrão.'),
        'es': ('Una vez al día el playbook pregunta a GitHub por la release más nueva y avisa, '
               'una vez por versión, cuando es más nueva que la instalada. La petición no lleva '
               'nada sobre ti, tu máquina o tu proyecto. Encendido por defecto.'),
    },
    'effect_update_check_on': {
        'en': 'In force now: the next session start checks for a newer release.',
        'pt-br': 'Em vigor agora: o próximo início de sessão procura uma release mais nova.',
        'es': 'En vigor ahora: el próximo inicio de sesión busca una release más nueva.',
    },
    'effect_update_check_off': {
        'en': 'In force now: nothing is asked of GitHub; update by hand when you choose.',
        'pt-br': 'Em vigor agora: nada é perguntado ao GitHub; atualize à mão quando quiser.',
        'es': 'En vigor ahora: no se pregunta nada a GitHub; actualiza a mano cuando quieras.',
    },
    'group_online': {'en': 'Online', 'pt-br': 'Online', 'es': 'En línea'},
    'group_online_help': {
        'en': 'What the playbook asks of the network: the update check, the update itself and the deviation reports.',
        'pt-br': 'O que o playbook pede à rede: a verificação de atualização, a atualização em si e os relatos de desvio.',
        'es': 'Lo que el playbook pide a la red: la búsqueda de actualizaciones, la actualización en sí y los informes de desvío.',
    },
    'update_available': {
        'en': ('[playbook] Version {latest} is available (installed {installed}). Open '
               '/playbook-config, Online, to update.'),
        'pt-br': ('[playbook] A versão {latest} está disponível (instalada {installed}). Abra '
                  '/playbook-config, Online, para atualizar.'),
        'es': ('[playbook] La versión {latest} está disponible (instalada {installed}). Abre '
               '/playbook-config, En línea, para actualizar.'),
    },
    'update_now': {'en': 'Update now', 'pt-br': 'Atualizar agora', 'es': 'Actualizar ahora'},
    'update_check_now': {
        'en': 'Check for updates',
        'pt-br': 'Procurar atualização',
        'es': 'Buscar actualizaciones',
    },
    'update_new': {
        'en': 'Version {latest} is available (installed {installed}).',
        'pt-br': 'A versão {latest} está disponível (instalada {installed}).',
        'es': 'La versión {latest} está disponible (instalada {installed}).',
    },
    'update_current': {
        'en': 'Up to date: {installed} is the newest release.',
        'pt-br': 'Em dia: a {installed} é a release mais nova.',
        'es': 'Al día: la {installed} es la release más nueva.',
    },
    'update_unreachable': {
        'en': 'Could not reach GitHub, so the newest release is unknown. Installed: {installed}.',
        'pt-br': 'Não consegui falar com o GitHub, então a release mais nova é desconhecida. Instalada: {installed}.',
        'es': 'No pude comunicarme con GitHub, así que la release más nueva se desconoce. Instalada: {installed}.',
    },
    'update_done': {
        'en': 'Updated. Reloading the plugins to apply it.',
        'pt-br': 'Atualizado. Recarregando os plugins para aplicar.',
        'es': 'Actualizado. Recargando los plugins para aplicarlo.',
    },
    'update_done_manual': {
        'en': 'Updated. Type /reload-plugins to apply it in this session.',
        'pt-br': 'Atualizado. Digite /reload-plugins para aplicar nesta sessão.',
        'es': 'Actualizado. Escribe /reload-plugins para aplicarlo en esta sesión.',
    },
    'update_failed': {
        'en': 'The update failed: {error}',
        'pt-br': 'A atualização falhou: {error}',
        'es': 'La actualización falló: {error}',
    },
    'pending_waiting': {
        'en': ('[playbook] Waiting for your answer, above the chat: {list}. Nothing runs until '
               'you approve; decline to drop it.'),
        'pt-br': ('[playbook] Aguardando a sua resposta, acima do chat: {list}. Nada roda até você '
                  'aprovar; recuse para descartar.'),
        'es': ('[playbook] Esperando tu respuesta, sobre el chat: {list}. Nada se ejecuta hasta '
               'que apruebes; rechaza para descartarlo.'),
    },
    'tmp_no_output': {
        'en': 'the command wrote nothing',
        'pt-br': 'o comando não escreveu nada',
        'es': 'el comando no escribió nada',
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
    'destructive_ask': {
        'en': ('[playbook] This command {effect}: `{command}`. Approve only if you want exactly '
               'this to run. Where no one can answer this prompt it is denied: run the command '
               'yourself.'),
        'pt-br': ('[playbook] Este comando {effect}: `{command}`. Aprove só se você quer que '
                  'exatamente isso rode. Onde ninguém pode responder este pedido, ele é negado: '
                  'rode o comando você mesmo.'),
        'es': ('[playbook] Este comando {effect}: `{command}`. Apruébalo solo si quieres que se '
               'ejecute exactamente esto. Donde nadie puede responder esta solicitud, se '
               'deniega: ejecuta el comando tú mismo.'),
    },
    # The button above the chat and the Temporary data panel (hooks/temporary_data.tsx).
    'tmp_waiting': {
        'en': 'Auto mode is waiting for your answer:',
        'pt-br': 'O modo auto está esperando a sua resposta:',
        'es': 'El modo auto espera tu respuesta:',
    },
    'tmp_row': {'en': '#{id} {effect}: {command}', 'pt-br': '#{id} {effect}: {command}',
                'es': '#{id} {effect}: {command}'},
    'tmp_hint': {
        'en': 'Click, or ctrl+x tab, the arrows and Enter. The rest of the work goes on.',
        'pt-br': 'Clique, ou ctrl+x tab, as setas e Enter. O resto do trabalho segue.',
        'es': 'Haz clic, o ctrl+x tab, las flechas y Enter. El resto del trabajo sigue.',
    },
    'tmp_approve': {'en': 'Approve', 'pt-br': 'Aprovar', 'es': 'Aprobar'},
    'tmp_decline': {'en': 'Decline', 'pt-br': 'Recusar', 'es': 'Rechazar'},
    'tmp_more': {'en': '+{count} more waiting', 'pt-br': '+{count} na fila',
                 'es': '+{count} más en espera'},
    'tmp_cut': {
        'en': 'Too long to run from here: answer in the chat ("pode apagar #{id}").',
        'pt-br': 'Longo demais para rodar daqui: responda no chat ("pode apagar #{id}").',
        'es': 'Demasiado largo para ejecutarlo desde aquí: responde en el chat ("pode apagar #{id}").',
    },
    'tmp_open': {'en': 'Temporary data ({count})', 'pt-br': 'Temporary data ({count})',
                 'es': 'Temporary data ({count})'},
    'tmp_title': {'en': 'Temporary data', 'pt-br': 'Temporary data', 'es': 'Temporary data'},
    'tmp_help': {
        'en': ('Deletions of this project you have not run yet. Pick one to remove it or keep '
               'it: click, type its number and send, or ctrl+x tab and the arrows.'),
        'pt-br': ('Remoções deste projeto que você ainda não rodou. Escolha uma para apagar ou '
                  'manter: clique, digite o número e envie, ou ctrl+x tab e as setas.'),
        'es': ('Borrados de este proyecto que aún no ejecutaste. Elige uno para borrarlo o '
               'mantenerlo: haz clic, escribe su número y envía, o ctrl+x tab y las flechas.'),
    },
    'tmp_close': {'en': 'Close', 'pt-br': 'Fechar', 'es': 'Cerrar'},
    'tmp_empty': {
        'en': 'Nothing kept for this project.',
        'pt-br': 'Nada guardado para este projeto.',
        'es': 'Nada guardado para este proyecto.',
    },
    'tmp_item': {
        'en': '{effect}: {command} (asked by Claude on {when})',
        'pt-br': '{effect}: {command} (pedido pelo Claude em {when})',
        'es': '{effect}: {command} (pedido por Claude el {when})',
    },
    'tmp_item_created': {
        'en': '{effect}, made by Claude on {when}: {command}',
        'pt-br': '{effect}, criado pelo Claude em {when}: {command}',
        'es': '{effect}, creado por Claude el {when}: {command}',
    },
    'tmp_item_registered': {
        'en': '{effect}, kept by Claude on {when}: {command}',
        'pt-br': '{effect}, guardado pelo Claude em {when}: {command}',
        'es': '{effect}, guardado por Claude el {when}: {command}',
    },
    'tmp_what_worktree': {'en': 'git worktree {name}', 'pt-br': 'worktree do git {name}',
                          'es': 'worktree de git {name}'},
    'tmp_what_tempdir': {'en': 'temporary folder {name}', 'pt-br': 'pasta temporária {name}',
                         'es': 'carpeta temporal {name}'},
    'tmp_what_tempfile': {'en': 'temporary file {name}', 'pt-br': 'arquivo temporário {name}',
                          'es': 'archivo temporal {name}'},
    'tmp_what_container': {'en': 'Docker container {name}', 'pt-br': 'container Docker {name}',
                           'es': 'contenedor Docker {name}'},
    'tmp_what_volume': {'en': 'Docker volume {name}', 'pt-br': 'volume Docker {name}',
                        'es': 'volumen Docker {name}'},
    'tmp_what_network': {'en': 'Docker network {name}', 'pt-br': 'rede Docker {name}',
                         'es': 'red Docker {name}'},
    'tmp_registered': {
        'en': 'Kept in Temporary data.',
        'pt-br': 'Guardado no Temporary data.',
        'es': 'Guardado en Temporary data.',
    },
    'tmp_confirm': {
        'en': 'Remove it? This runs, in {cwd}:',
        'pt-br': 'Apagar? Isto roda, em {cwd}:',
        'es': '¿Borrarlo? Esto se ejecuta, en {cwd}:',
    },
    'tmp_yes': {'en': 'Yes, remove', 'pt-br': 'Sim, apagar', 'es': 'Sí, borrar'},
    'tmp_no': {'en': 'No, keep it', 'pt-br': 'Não, manter', 'es': 'No, mantener'},
    'tmp_ran': {'en': '#{id} ran.', 'pt-br': '#{id} rodou.', 'es': '#{id} se ejecutó.'},
    'tmp_failed': {
        'en': 'It failed (exit {code}): {error}',
        'pt-br': 'Falhou (saída {code}): {error}',
        'es': 'Falló (salida {code}): {error}',
    },
    'tmp_kept': {
        'en': '#{id} kept in Temporary data.',
        'pt-br': '#{id} guardado no Temporary data.',
        'es': '#{id} guardado en Temporary data.',
    },
    'tmp_dropped': {'en': '#{id} will not run.', 'pt-br': '#{id} não vai rodar.',
                    'es': '#{id} no se ejecutará.'},
    'tmp_removed': {'en': 'Removed.', 'pt-br': 'Apagado.', 'es': 'Borrado.'},
    'tmp_gone': {
        'en': 'Already answered: nothing to do.',
        'pt-br': 'Já respondido: nada a fazer.',
        'es': 'Ya respondido: nada que hacer.',
    },
    'snapshot_saved': {
        'en': ('[playbook:safety] This command discards uncommitted changes, so they were saved '
               'first and nothing is lost: {saved}. To get them back: `git -C <repo> stash apply '
               '<commit>`. Say so to the user when the discarded changes were theirs, not yours.'),
        'pt-br': ('[playbook:safety] Este comando descarta mudanças não commitadas, então elas '
                  'foram salvas antes e nada se perde: {saved}. Para recuperar: `git -C <repo> '
                  'stash apply <commit>`. Avise o usuário quando as mudanças descartadas eram '
                  'dele, não suas.'),
        'es': ('[playbook:safety] Este comando descarta cambios sin commit, así que se guardaron '
               'antes y no se pierde nada: {saved}. Para recuperarlos: `git -C <repo> stash apply '
               '<commit>`. Avisa al usuario cuando los cambios descartados eran suyos, no tuyos.'),
    },
    'destructive_part': {
        'en': 'That is one part of a longer command: approving runs the whole command.',
        'pt-br': 'Esse é um trecho de um comando maior: aprovar roda o comando inteiro.',
        'es': 'Es una parte de un comando más largo: aprobarlo ejecuta el comando entero.',
    },
    'destroys_delete_tree': {
        'en': 'deletes files and directories recursively',
        'pt-br': 'apaga arquivos e diretórios recursivamente',
        'es': 'borra archivos y directorios de forma recursiva',
    },
    'destroys_force_push': {
        'en': 'overwrites the remote branch, dropping the commits it has that you do not',
        'pt-br': 'sobrescreve o branch remoto, descartando os commits que ele tem e você não',
        'es': 'sobrescribe la rama remota y descarta los commits que tiene y tú no',
    },
    'destroys_reset_hard': {
        'en': 'discards every uncommitted change in the working tree and the index',
        'pt-br': 'descarta toda mudança não commitada na árvore de trabalho e no índice',
        'es': 'descarta todo cambio sin commit del árbol de trabajo y del índice',
    },
    'destroys_branch_delete': {
        'en': 'deletes a branch even when its commits are not merged',
        'pt-br': 'apaga um branch mesmo com commits ainda não mesclados',
        'es': 'borra una rama aunque sus commits no estén fusionados',
    },
    'destroys_discard_changes': {
        'en': 'discards uncommitted changes to the files it names',
        'pt-br': 'descarta mudanças não commitadas dos arquivos que cita',
        'es': 'descarta los cambios sin commit de los archivos que nombra',
    },
    'destroys_clean': {
        'en': 'deletes files git does not track',
        'pt-br': 'apaga arquivos que o git não rastreia',
        'es': 'borra archivos que git no rastrea',
    },
    'destroys_stash_drop': {
        'en': 'deletes stashed changes',
        'pt-br': 'apaga mudanças guardadas no stash',
        'es': 'borra cambios guardados en el stash',
    },
    'destroys_history': {
        'en': 'deletes or rewrites git history that the reflog may not bring back',
        'pt-br': 'apaga ou reescreve histórico do git que o reflog pode não recuperar',
        'es': 'borra o reescribe historial de git que el reflog quizá no recupere',
    },
    'destroys_worktree_remove': {
        'en': 'deletes a worktree together with its uncommitted changes',
        'pt-br': 'apaga um worktree junto com as mudanças não commitadas dele',
        'es': 'borra un worktree junto con sus cambios sin commit',
    },
    'destroys_sync_delete': {
        'en': 'deletes the files at the destination that the source does not have',
        'pt-br': 'apaga no destino os arquivos que a origem não tem',
        'es': 'borra en el destino los archivos que el origen no tiene',
    },
    'destroys_shred': {
        'en': 'overwrites files so they cannot be recovered',
        'pt-br': 'sobrescreve arquivos para que não possam ser recuperados',
        'es': 'sobrescribe archivos para que no se puedan recuperar',
    },
    'destroys_truncate': {
        'en': 'empties files',
        'pt-br': 'esvazia arquivos',
        'es': 'vacía archivos',
    },
    'destroys_mass_delete': {
        'en': 'deletes every file it matches',
        'pt-br': 'apaga todo arquivo que casar com o filtro',
        'es': 'borra todo archivo que coincida con el filtro',
    },
    'destroys_sql': {
        'en': 'deletes data from a database',
        'pt-br': 'apaga dados de um banco de dados',
        'es': 'borra datos de una base de datos',
    },
    'destroys_script_delete': {
        'en': 'deletes files recursively from inside a script',
        'pt-br': 'apaga arquivos recursivamente de dentro de um script',
        'es': 'borra archivos de forma recursiva desde un script',
    },
    'lsp_hint': {
        'en': ('[playbook] {stack} project without a code-intelligence plugin: with '
               '`{plugin}`, Claude sees type and name errors right after each edit, before '
               'any build. Install the `{binary}` language server, then run '
               '`/plugin install {plugin}@claude-plugins-official`. Shown once per project.'),
        'pt-br': ('[playbook] Projeto {stack} sem plugin de code intelligence: com o '
                  '`{plugin}`, o Claude vê erros de tipo e de nome logo após cada edição, antes '
                  'de qualquer build. Instale o language server `{binary}` e depois rode '
                  '`/plugin install {plugin}@claude-plugins-official`. Aviso único por projeto.'),
        'es': ('[playbook] Proyecto {stack} sin plugin de code intelligence: con '
               '`{plugin}`, Claude ve errores de tipo y de nombre justo después de cada edición, '
               'antes de cualquier build. Instala el language server `{binary}` y luego ejecuta '
               '`/plugin install {plugin}@claude-plugins-official`. Aviso único por proyecto.'),
    },
}

# The panel's own strings, sent whole in `show --json` as `ui`.
UI_KEYS = (
    'title', 'hint_main', 'hint_sub', 'opened', 'no_entry', 'current', 'default',
    'status', 'learn', 'lab', 'diff', 'back', 'exit', 'custom',
    'lab_title', 'lab_help', 'lab_setup', 'lab_passive', 'lab_active', 'lab_sqlmap',
    'lab_uninstall', 'update_now', 'update_check_now',
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
