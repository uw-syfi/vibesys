import type {RequestInput} from './protocol.js';
import type {PaneView} from './session-model.js';
import {isThemeName, THEME_NAMES, type ThemeName} from './ui/theme.js';

export type ParsedInput = {
  localView?: 'chat' | 'help' | 'theme';
  chatMessage?: string;
  themeName?: ThemeName;
  request?: RequestInput;
  responseView?: 'perf';
  /** Opens a hypothesis trajectory: the selected row, or the given round. */
  openRound?: {round?: number};
  /**
   * Renders the response in the right pane beside the transcript. Every
   * visualization command opts in through this field; modal surfaces such as
   * /help and errors leave it unset.
   */
  paneView?: PaneView;
  error?: string;
};

export interface SlashCommand {
  name: string;
  description: string;
}

export const SLASH_COMMANDS: readonly SlashCommand[] = [
  {name: '/help', description: 'Show this help'},
  {name: '/chat', description: 'Open experiment chat'},
  {name: '/pause', description: 'Pause after the current agent call'},
  {name: '/resume', description: 'Resume a paused run'},
  {name: '/steer', description: 'Guide the next agent invocation: /steer <message>'},
  {
    name: '/open-round',
    description: 'Open the selected hypothesis, or /open-round --N for round N',
  },
  {name: '/perf', description: 'Plot performance by round in the right pane'},
  {name: '/theme', description: 'List themes, or switch with /theme <name>'},
];

/**
 * Where the chat is a pane of the current view there is nothing for `/chat` to
 * open, so it leaves the command surface rather than sitting in it as a command
 * that does nothing new. It is still accepted, and still opens the chat
 * anywhere the chat is not already on screen.
 */
export interface CommandContext {
  chatDocked?: boolean;
}

export function availableCommands(context: CommandContext = {}): readonly SlashCommand[] {
  if (!context.chatDocked) return SLASH_COMMANDS;
  return SLASH_COMMANDS.filter(command => command.name !== '/chat');
}

export function helpText(context: CommandContext = {}): string {
  return [
    'Available',
    ...availableCommands(context).map(
      command => `  ${command.name.padEnd(18)} ${command.description}`,
    ),
    '',
    'Planned',
    '  /round <number>    Inspect a completed round',
    '  /invocation <id>   Inspect an agent invocation',
  ].join('\n');
}

export function suggestSlashCommands(
  text: string,
  context: CommandContext = {},
): readonly SlashCommand[] {
  if (!text.startsWith('/') || /\s/.test(text)) return [];
  return availableCommands(context).filter(command => command.name.startsWith(text));
}

export function slashCommandRange(text: string): {start: number; end: number} | null {
  const match = /^\/[a-z][a-z0-9-]*/i.exec(text);
  if (match === null) return null;
  return {start: 0, end: match[0].length};
}

export function parseInput(text: string): ParsedInput {
  if (text === '/help') return {localView: 'help'};
  const chat = text.match(/^\/chat(?:\s+(.*))?$/);
  if (chat) {
    const message = chat[1]?.trim();
    return {localView: 'chat', ...(message ? {chatMessage: message} : {})};
  }
  const theme = text.match(/^\/theme(?:\s+(.*))?$/);
  if (theme) {
    const requested = theme[1]?.trim();
    if (!requested) return {localView: 'theme'};
    if (!isThemeName(requested)) {
      return {error: `Unknown theme: ${requested}. Available: ${THEME_NAMES.join(', ')}.`};
    }
    return {localView: 'theme', themeName: requested};
  }
  if (text === '/pause') return {request: {type: 'command.pause'}};
  if (text === '/resume') return {request: {type: 'command.resume'}};
  const steer = text.match(/^\/steer(?:\s+([\s\S]*))?$/);
  if (steer) {
    const message = steer[1]?.trim();
    if (!message) return {error: 'Usage: /steer <message>'};
    return {request: {type: 'command.steer', text: message}};
  }
  const openRound = text.match(/^\/open-round(?:\s+(.*))?$/);
  if (openRound) {
    const argument = openRound[1]?.trim();
    if (!argument) return {openRound: {}};
    // ``--N`` is the documented form; a bare number is accepted because it is
    // the obvious thing to type.
    const match = argument.match(/^(?:--)?(\d+)$/);
    if (!match) {
      return {error: `Unknown round: ${argument}. Use /open-round or /open-round --N.`};
    }
    return {openRound: {round: Number(match[1])}};
  }
  if (text === '/perf') {
    return {request: {type: 'query.performance'}, responseView: 'perf', paneView: 'perf'};
  }
  if (text.startsWith('/')) return {error: `Unknown command: ${text}. Use /help.`};
  if (text === '') return {error: 'Enter a question or use /help.'};
  return {request: {type: 'query.chat', text}};
}
