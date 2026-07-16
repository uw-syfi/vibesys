import type {RequestInput} from './protocol.js';

export type ParsedInput = {
  localView?: 'help';
  request?: RequestInput;
  responseView?: 'history' | 'perf';
  error?: string;
};

export const HELP_TEXT = [
  'Available',
  '  /help              Show this help',
  '  /history           List rounds and their elapsed time',
  '  /perf              Plot performance by round',
  '',
  'Planned',
  '  /pause             Pause at the next safe point',
  '  /resume            Resume a paused run',
  '  /steer <message>   Guide a future agent invocation',
  '  /round <number>    Inspect a completed round',
  '  /invocation <id>   Inspect an agent invocation',
].join('\n');

export function parseInput(text: string): ParsedInput {
  if (text === '/help') return {localView: 'help'};
  if (text === '/history') return {request: {type: 'query.history'}, responseView: 'history'};
  if (text === '/perf') return {request: {type: 'query.history'}, responseView: 'perf'};
  return {error: `Unknown command: ${text || '(empty)'}. Use /help.`};
}
