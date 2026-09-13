import type {RequestInput} from '@vibesys/backend-client';
import type {PaneView} from './session-model.js';
import {isThemeName, THEME_NAMES, type ThemeName} from './ui/theme.js';

type CommandRequest = Exclude<RequestInput, {type: 'query.chat'}>;

/** Every input surface that resolves slash commands through this registry. */
export type CommandSurface = 'command' | 'chat';

/** The help grouping a command is listed under. */
export type CommandSection = 'general' | 'run' | 'view' | 'chat';

/** A stable identifier per command, used where code needs the name without spelling it. */
export type CommandId =
  | 'help'
  | 'chat'
  | 'pause'
  | 'resume'
  | 'steer'
  | 'open-round'
  | 'perf'
  | 'design'
  | 'todos'
  | 'prompt'
  | 'theme'
  | 'clear'
  | 'model'
  | 'switch';

/**
 * The result of resolving one line of input. A discriminated union so every
 * consumer switches on `kind`, the compiler can prove the switch exhaustive,
 * and no consumer re-spells a command name.
 */
export type ParsedCommand =
  | {kind: 'help'}
  | {kind: 'openChat'; chatMessage?: string}
  | {kind: 'toggle'; toggle: 'todos' | 'prompt'}
  | {kind: 'theme'; themeName?: ThemeName}
  | {kind: 'openRound'; round?: number}
  | {kind: 'request'; request: CommandRequest; responseView?: 'perf'; paneView?: PaneView}
  | {kind: 'chatClear'}
  | {kind: 'chatModel'}
  | {kind: 'chatSwitch'}
  | {kind: 'unknown'; text: string}
  | {kind: 'error'; error: string};

/**
 * Where the chat is a pane of the current view there is nothing for `/chat` to
 * open, so it leaves the suggestion surface rather than sitting in it as a
 * command that does nothing new. It is still accepted, and still opens the chat
 * anywhere the chat is not already on screen.
 */
export interface CommandContext {
  chatDocked?: boolean;
}

/** A context bound to the surface whose input is being resolved. */
export interface SurfaceContext extends CommandContext {
  surface: CommandSurface;
}

/** The name/description shape the suggestion menu and its renderer consume. */
export interface SlashCommand {
  name: string;
  description: string;
}

/** How many arguments a command accepts, enforced before its parser runs. */
export type CommandArity = 'none' | 'optional' | 'required';

/**
 * A command's declared contract, without its handler. This is what the
 * suggestion menu, the help text, the README, and the argument check are all
 * derived from, so tests can enumerate the contract without reaching into the
 * parsers.
 */
export interface CommandSpec {
  readonly id: CommandId;
  readonly name: string;
  readonly aliases?: readonly string[];
  readonly description: string;
  readonly args: CommandArity;
  readonly usage?: string;
  readonly surfaces: readonly CommandSurface[];
  readonly section: CommandSection;
}

/**
 * The single definition of one slash command. `parse` is required, so a command
 * cannot be registered without a handler, and it is the only place the command's
 * behavior is spelled: parsers, suggestions, and help are all derived from this
 * table, so a match string can never drift from a registry name.
 */
interface CommandDef extends CommandSpec {
  /** Hides the command from suggestions and help in a context; parsing still accepts it. */
  readonly hiddenWhen?: (context: CommandContext) => boolean;
  readonly parse: (argument: string) => ParsedCommand;
}

const BOTH: readonly CommandSurface[] = ['command', 'chat'];
const CHAT_ONLY: readonly CommandSurface[] = ['chat'];

/**
 * The one place every slash command is defined. Both the command bar and the
 * chat composer resolve input through this table, so a fix to one is a fix to
 * both. `/resume` resumes the paused run on every surface; the chat's own
 * thread switch is `/switch`, so the two never collide by name.
 */
const COMMAND_REGISTRY: readonly CommandDef[] = [
  {
    id: 'help',
    name: '/help',
    description: 'Show this help',
    args: 'none',
    surfaces: BOTH,
    section: 'general',
    parse: () => ({kind: 'help'}),
  },
  {
    id: 'chat',
    name: '/chat',
    description: 'Open experiment chat',
    args: 'optional',
    usage: '/chat <question>',
    // Only the command bar offers /chat: inside the chat there is nothing to open.
    surfaces: ['command'],
    section: 'general',
    hiddenWhen: context => context.chatDocked === true,
    parse: argument => (argument ? {kind: 'openChat', chatMessage: argument} : {kind: 'openChat'}),
  },
  {
    id: 'pause',
    name: '/pause',
    description: 'Pause after the current agent call',
    args: 'none',
    surfaces: BOTH,
    section: 'run',
    parse: () => ({kind: 'request', request: {type: 'command.pause'}}),
  },
  {
    id: 'resume',
    name: '/resume',
    description: 'Resume a paused run',
    args: 'none',
    surfaces: BOTH,
    section: 'run',
    parse: () => ({kind: 'request', request: {type: 'command.resume'}}),
  },
  {
    id: 'steer',
    name: '/steer',
    description: 'Guide the next agent invocation: /steer <message>',
    args: 'required',
    usage: '/steer <message>',
    surfaces: BOTH,
    section: 'run',
    parse: argument =>
      argument
        ? {kind: 'request', request: {type: 'command.steer', text: argument}}
        : {kind: 'error', error: 'Usage: /steer <message>'},
  },
  {
    id: 'open-round',
    name: '/open-round',
    description: 'Open the selected hypothesis, or /open-round --N for round N',
    args: 'optional',
    usage: '/open-round --N',
    surfaces: BOTH,
    section: 'view',
    parse: argument => {
      if (!argument) return {kind: 'openRound'};
      // ``--N`` is the documented form; a bare number is accepted because it is
      // the obvious thing to type.
      const match = argument.match(/^(?:--)?(\d+)$/);
      if (!match) {
        return {
          kind: 'error',
          error: `Unknown round: ${argument}. Use /open-round or /open-round --N.`,
        };
      }
      return {kind: 'openRound', round: Number(match[1])};
    },
  },
  {
    id: 'perf',
    name: '/perf',
    description: 'Plot performance by round in the right pane',
    args: 'none',
    surfaces: BOTH,
    section: 'view',
    parse: () => ({
      kind: 'request',
      request: {type: 'query.performance'},
      responseView: 'perf',
      paneView: 'perf',
    }),
  },
  {
    id: 'design',
    name: '/design',
    description: 'Summarize each round’s file changes in the right pane',
    args: 'none',
    surfaces: BOTH,
    section: 'view',
    parse: () => ({kind: 'request', request: {type: 'query.design'}, paneView: 'design'}),
  },
  {
    id: 'todos',
    name: '/todos',
    description: "Expand or collapse the visible agent's todo list",
    args: 'none',
    surfaces: BOTH,
    section: 'view',
    parse: () => ({kind: 'toggle', toggle: 'todos'}),
  },
  {
    id: 'prompt',
    name: '/prompt',
    description: 'Expand or collapse the latest prompt in view',
    args: 'none',
    surfaces: BOTH,
    section: 'view',
    parse: () => ({kind: 'toggle', toggle: 'prompt'}),
  },
  {
    id: 'theme',
    name: '/theme',
    description: 'List themes, or switch with /theme <name>',
    args: 'optional',
    usage: '/theme <name>',
    surfaces: BOTH,
    section: 'view',
    parse: argument => {
      if (!argument) return {kind: 'theme'};
      if (!isThemeName(argument)) {
        return {
          kind: 'error',
          error: `Unknown theme: ${argument}. Available: ${THEME_NAMES.join(', ')}.`,
        };
      }
      return {kind: 'theme', themeName: argument};
    },
  },
  {
    id: 'clear',
    name: '/clear',
    description: 'Start a fresh thread with this thread’s agent and model',
    args: 'none',
    surfaces: CHAT_ONLY,
    section: 'chat',
    parse: () => ({kind: 'chatClear'}),
  },
  {
    id: 'model',
    name: '/model',
    description: 'Pick a harness and model, and start a thread on it',
    args: 'none',
    surfaces: CHAT_ONLY,
    section: 'chat',
    parse: () => ({kind: 'chatModel'}),
  },
  {
    id: 'switch',
    name: '/switch',
    description: 'Switch to another chat thread',
    args: 'none',
    surfaces: CHAT_ONLY,
    section: 'chat',
    parse: () => ({kind: 'chatSwitch'}),
  },
];

/**
 * Every registered command's declared contract, in registry order. Exposed so
 * documentation and contract tests enumerate the same table the parser uses
 * rather than restating it.
 */
export const COMMAND_SPECS: readonly CommandSpec[] = COMMAND_REGISTRY.map(
  ({hiddenWhen: _hiddenWhen, parse: _parse, ...spec}) => spec,
);

/** The canonical name per command, so callers reference a command without a literal. */
export const COMMAND_NAMES = Object.fromEntries(
  COMMAND_REGISTRY.map(command => [command.id, command.name]),
) as Record<CommandId, string>;

const NAME_TOKEN = /^\/[a-z][a-z0-9-]*/i;

function findCommand(token: string, surface: CommandSurface): CommandDef | undefined {
  const lower = token.toLowerCase();
  return COMMAND_REGISTRY.find(
    command =>
      command.surfaces.includes(surface) &&
      (command.name === lower || command.aliases?.includes(lower) === true),
  );
}

/**
 * Enforces a command's `args` contract before dispatch. A `none` command rejects
 * any trailing text, and a `required` command rejects an empty argument, so a
 * slash-prefixed phrase like `/pause typo` cannot slip past a parser that ignores
 * its argument. Returns the registry usage error, or `undefined` when the
 * argument is admissible and the parser should run.
 */
function checkArgument(command: CommandDef, argument: string): ParsedCommand | undefined {
  if (command.args === 'none' && argument !== '') {
    return {kind: 'error', error: `Usage: ${command.usage ?? command.name}`};
  }
  if (command.args === 'required' && argument === '') {
    return {kind: 'error', error: `Usage: ${command.usage ?? command.name}`};
  }
  return undefined;
}

/**
 * The commands a surface offers, in the order suggestions and help list them.
 * The chat leads with its own thread commands, then the run and view commands
 * it forwards; the command bar keeps registry order.
 */
export function availableCommands(context: SurfaceContext): readonly SlashCommand[] {
  const matches = COMMAND_REGISTRY.filter(
    command => command.surfaces.includes(context.surface) && command.hiddenWhen?.(context) !== true,
  );
  const ordered =
    context.surface === 'chat'
      ? [
          ...matches.filter(command => command.section === 'chat'),
          ...matches.filter(command => command.section !== 'chat'),
        ]
      : matches;
  return ordered.map(command => ({name: command.name, description: command.description}));
}

/**
 * Resolves one line of input for a surface. The command bar and chat composer
 * both call this, so case handling, unknown-command diagnosis, and usage errors
 * are identical everywhere. `hiddenWhen` gates suggestions, not parsing, so a
 * docked `/chat` still opens the chat.
 */
export function parseCommand(text: string, {surface}: SurfaceContext): ParsedCommand {
  const match = NAME_TOKEN.exec(text);
  if (match === null) {
    if (text === '')
      return {kind: 'error', error: 'Enter a slash command: try /help for the list.'};
    if (!text.startsWith('/')) {
      return {kind: 'error', error: 'Not a command: try /help, or ask in Experiment chat.'};
    }
    return {kind: 'unknown', text};
  }
  const token = match[0];
  const argument = text.slice(token.length).replace(/^\s+/, '').trimEnd();
  const command = findCommand(token, surface);
  if (command === undefined) return {kind: 'unknown', text};
  const argumentError = checkArgument(command, argument);
  if (argumentError !== undefined) return argumentError;
  return command.parse(argument);
}

export function suggestSlashCommands(
  text: string,
  context: SurfaceContext,
): readonly SlashCommand[] {
  if (!text.startsWith('/') || /\s/.test(text)) return [];
  const lower = text.toLowerCase();
  return availableCommands(context).filter(command => command.name.startsWith(lower));
}

export function slashCommandRange(text: string): {start: number; end: number} | null {
  const match = NAME_TOKEN.exec(text);
  if (match === null) return null;
  return {start: 0, end: match[0].length};
}

/** The command bar's help, generated from the registry for the command surface. */
export function helpText(context: CommandContext = {}): string {
  const commands = availableCommands({surface: 'command', ...context});
  return [
    'Available',
    ...commands.map(command => `  ${command.name.padEnd(18)} ${command.description}`),
  ].join('\n');
}

/** The chat composer's help, generated from the registry for the chat surface. */
export function chatHelpText(): string {
  const commands = availableCommands({surface: 'chat'});
  return [
    'Chat commands',
    ...commands.map(command => `  ${command.name.padEnd(13)} ${command.description}`),
    '',
    'Anything else you type is a question for the chat agent.',
  ].join('\n');
}
