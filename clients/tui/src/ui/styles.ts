import {SyntaxStyle} from '@opentui/core';
import type {ConversationEntry} from '../session-model.js';

export interface EntryPalette {
  border: string;
  background: string;
  label: string;
  content: string;
}

export function createMarkdownStyle(): SyntaxStyle {
  return SyntaxStyle.fromStyles({
    default: {fg: '#e2e8f0'},
    heading: {fg: '#67e8f9', bold: true},
    strong: {fg: '#f8fafc', bold: true},
    em: {fg: '#cbd5e1', italic: true},
    code: {fg: '#a5f3fc', bg: '#1e293b'},
    link: {fg: '#38bdf8', underline: true},
    blockquote: {fg: '#94a3b8', italic: true},
  });
}

export function entryPalette(entry: ConversationEntry): EntryPalette {
  if (entry.tone === 'failure') {
    return {border: '#ef4444', background: '#1f1215', label: '#f87171', content: '#fecaca'};
  }
  if (entry.tone === 'success') {
    return {border: '#22c55e', background: '#102018', label: '#4ade80', content: '#bbf7d0'};
  }
  if (entry.kind === 'assistant') {
    return {border: '#0891b2', background: '#0f1b24', label: '#67e8f9', content: '#e2e8f0'};
  }
  if (entry.kind === 'prompt') {
    return {border: '#3b82f6', background: '#102548', label: '#93c5fd', content: '#dbeafe'};
  }
  if (entry.kind === 'analysis') {
    return {border: '#475569', background: '#171923', label: '#a78bfa', content: '#94a3b8'};
  }
  if (entry.kind === 'tool' || entry.kind === 'diagnostic' || entry.kind === 'subprocess') {
    return {border: '#3f3f46', background: '#18181b', label: '#a1a1aa', content: '#a1a1aa'};
  }
  return {border: '#475569', background: '#111827', label: '#94a3b8', content: '#cbd5e1'};
}
