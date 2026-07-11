import type {RequestInput} from './protocol.js';

export type ParsedInput = {
  localView?: 'live' | 'help';
  request?: RequestInput;
  error?: string;
};

export const HELP_TEXT = [
  '/steer <guidance>  Guide the next agent invocation',
  '/pause             Pause after the current agent call',
  '/resume            Resume a paused run',
  '/live              Return to live output (Ctrl+L)',
  '/history           Show the audited event history',
  '/help              Show this help',
].join('\n');

export function parseInput(text: string): ParsedInput {
  if (text === '/live') return {localView: 'live'};
  if (text === '/help') return {localView: 'help'};
  if (text.startsWith('/steer ')) return {request: {type: 'command.steer', text: text.slice(7), target: 'next_safe_point'}};
  if (text === '/pause') return {request: {type: 'command.pause', mode: 'after_current_agent_call'}};
  if (text === '/resume') return {request: {type: 'command.resume'}};
  if (text === '/history') return {request: {type: 'query.history'}};
  if (text.startsWith('/')) return {error: `Unknown command: ${text}`};
  return {request: {type: 'query.chat', text}};
}
