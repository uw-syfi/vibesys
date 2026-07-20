import type {RequestInput} from './protocol.js';

export type ParsedInput = {
  localView?: 'help';
  request?: RequestInput;
  responseView?: 'history' | 'perf';
  error?: string;
};

export interface SlashCommand {
  name: string;
  description: string;
}

export const SLASH_COMMANDS: readonly SlashCommand[] = [
  {name: '/help', description: 'Show this help'},
  {name: '/history', description: 'List rounds and their elapsed time'},
  {name: '/perf', description: 'Plot performance by round'},
];

export const HELP_TEXT = [
  'Available',
  ...SLASH_COMMANDS.map(command => `  ${command.name.padEnd(18)} ${command.description}`),
  '',
  'Planned',
  '  /pause             Pause at the next safe point',
  '  /resume            Resume a paused run',
  '  /steer <message>   Guide a future agent invocation',
  '  /round <number>    Inspect a completed round',
  '  /invocation <id>   Inspect an agent invocation',
].join('\n');

export function suggestSlashCommands(text: string): readonly SlashCommand[] {
  if (!text.startsWith('/') || /\s/.test(text)) return [];
  return SLASH_COMMANDS.filter(command => command.name.startsWith(text));
}

export function slashCommandRange(text: string): {start: number; end: number} | null {
  const match = /^\/[a-z][a-z0-9-]*/i.exec(text);
  if (match === null) return null;
  return {start: 0, end: match[0].length};
}

export function parseInput(text: string): ParsedInput {
  if (text === '/help') return {localView: 'help'};
  if (text === '/history') return {request: {type: 'query.history'}, responseView: 'history'};
  if (text === '/perf') return {request: {type: 'query.performance'}, responseView: 'perf'};
  return {error: `Unknown command: ${text || '(empty)'}. Use /help.`};
}
