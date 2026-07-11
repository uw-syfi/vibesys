import {
  BoxRenderable,
  InputRenderable,
  MarkdownRenderable,
  ScrollBoxRenderable,
  SyntaxStyle,
  TextRenderable,
  type CliRenderer,
  type KeyEvent,
} from '@opentui/core';
import type {SessionController} from './session-controller.js';
import {statusText, type ConversationEntry, type SessionState} from './session-model.js';

const MAX_TOOL_OUTPUT_LINES = 12;

export interface OpenTuiApp {
  destroy(): void;
}

export function createOpenTuiApp(
  renderer: CliRenderer,
  controller: SessionController,
): OpenTuiApp {
  let exitScheduled = false;
  const root = new BoxRenderable(renderer, {
    id: 'app', width: '100%', height: '100%', flexDirection: 'column',
  });
  const header = new TextRenderable(renderer, {
    id: 'header', height: 1, fg: '#22d3ee', content: 'VibeServe · connecting',
  });
  const output = new BoxRenderable(renderer, {
    id: 'output', width: '100%', flexDirection: 'column', paddingLeft: 1, paddingRight: 1,
  });
  const markdownStyle = SyntaxStyle.fromStyles({
    default: {fg: '#e2e8f0'},
    heading: {fg: '#67e8f9', bold: true},
    strong: {fg: '#f8fafc', bold: true},
    em: {fg: '#cbd5e1', italic: true},
    code: {fg: '#a5f3fc', bg: '#1e293b'},
    link: {fg: '#38bdf8', underline: true},
    blockquote: {fg: '#94a3b8', italic: true},
  });
  let renderedConversation: ConversationEntry[] = [];
  const viewport = new ScrollBoxRenderable(renderer, {
    id: 'viewport',
    width: '100%',
    flexGrow: 1,
    border: true,
    borderStyle: 'rounded',
    borderColor: '#475569',
    stickyScroll: true,
    stickyStart: 'bottom',
    viewportCulling: true,
    verticalScrollbarOptions: {showArrows: true},
  });
  const help = new TextRenderable(renderer, {
    id: 'key-help',
    height: 1,
    fg: '#64748b',
    content: '↑/↓ · PgUp/PgDn · Home/End · Ctrl+L: live · /help',
  });
  const inputBox = new BoxRenderable(renderer, {
    id: 'input-box',
    height: 3,
    width: '100%',
    border: true,
    borderStyle: 'rounded',
    borderColor: '#22c55e',
    title: ' Ask or command ',
    paddingLeft: 1,
    paddingRight: 1,
  });
  const input = new InputRenderable(renderer, {
    id: 'input',
    width: '100%',
    placeholder: 'Type a question or /help',
    textColor: '#f8fafc',
    focusedTextColor: '#f8fafc',
    onSubmit: () => {
      const value = input.value;
      input.value = '';
      void controller.submit(value);
    },
  });

  viewport.add(output);
  inputBox.add(input);
  root.add(header);
  root.add(viewport);
  root.add(help);
  root.add(inputBox);
  renderer.root.add(root);
  input.focus();

  function render(state: SessionState): void {
    header.content = `VibeServe · ${statusText(state)}`;
    if (state.view === 'live') renderConversation(state.conversation);
    else renderDetail(state.detailContent);
    if (!exitScheduled && state.terminal) {
      exitScheduled = true;
      setTimeout(() => renderer.destroy(), 100);
    }
  }

  function clearOutput(): void {
    for (const child of [...output.getChildren()]) {
      output.remove(child);
      child.destroyRecursively();
    }
  }

  function renderDetail(content: string): void {
    renderedConversation = [];
    clearOutput();
    output.add(new TextRenderable(renderer, {content, fg: '#e2e8f0', width: '100%'}));
  }

  function renderConversation(entries: ConversationEntry[]): void {
    if (entries === renderedConversation) return;
    renderedConversation = entries;
    clearOutput();
    if (entries.length === 0) {
      output.add(new TextRenderable(renderer, {content: 'Waiting for run events…', fg: '#64748b'}));
      return;
    }
    for (const entry of entries) output.add(renderConversationEntry(entry));
  }

  function renderConversationEntry(entry: ConversationEntry): BoxRenderable {
    const palette = entryPalette(entry);
    const card = new BoxRenderable(renderer, {
      id: `event-${entry.id}`,
      width: '100%',
      flexDirection: 'column',
      marginTop: 1,
      paddingLeft: entry.kind === 'status' ? 0 : 1,
      paddingRight: 1,
      border: entry.kind !== 'status',
      borderStyle: 'rounded',
      borderColor: palette.border,
      backgroundColor: palette.background,
    });
    card.add(new TextRenderable(renderer, {
      content: entry.label ?? entry.kind,
      fg: palette.label,
      height: 1,
    }));
    if (entry.kind === 'assistant' || entry.kind === 'prompt') {
      card.add(new MarkdownRenderable(renderer, {
        content: entry.content,
        syntaxStyle: markdownStyle,
        conceal: true,
        streaming: !controller.state.terminal,
        width: '100%',
      }));
    } else if (entry.kind === 'tool' && entry.toolCall) {
      card.add(new TextRenderable(renderer, {
        content: entry.toolCall.trimEnd(),
        fg: '#dbeafe',
        bg: '#1e3a8a',
        width: '100%',
      }));
      if (entry.toolResponse) {
        card.add(new TextRenderable(renderer, {
          content: toolOutputPreview(entry.toolResponse),
          fg: '#a1a1aa',
          bg: '#18181b',
          width: '100%',
        }));
      }
    } else {
      const content = entry.kind === 'tool' || entry.kind === 'diagnostic'
        || entry.kind === 'subprocess'
        ? toolOutputPreview(entry.content)
        : entry.content;
      card.add(new TextRenderable(renderer, {
        content,
        fg: palette.content,
        width: '100%',
      }));
    }
    return card;
  }

  function onKey(key: KeyEvent): void {
    if (key.ctrl && key.name === 'l') {
      controller.live();
      viewport.scrollTo(viewport.scrollHeight);
      key.preventDefault();
      return;
    }
    if (key.name === 'pageup') viewport.scrollBy(-1, 'viewport');
    else if (key.name === 'pagedown') viewport.scrollBy(1, 'viewport');
    else if (key.ctrl && key.name === 'up') viewport.scrollBy(-1);
    else if (key.ctrl && key.name === 'down') viewport.scrollBy(1);
    else if (key.name === 'home') viewport.scrollTo(0);
    else if (key.name === 'end') viewport.scrollTo(viewport.scrollHeight);
    else return;
    key.preventDefault();
  }

  renderer.keyInput.on('keypress', onKey);
  const unsubscribe = controller.subscribe(render);
  return {
    destroy(): void {
      unsubscribe();
      renderer.keyInput.off('keypress', onKey);
      root.destroyRecursively();
      markdownStyle.destroy();
    },
  };
}

export function toolOutputPreview(content: string, maxLines = MAX_TOOL_OUTPUT_LINES): string {
  const lines = content.split('\n');
  const hasTrailingNewline = lines.at(-1) === '';
  if (hasTrailingNewline) lines.pop();
  if (lines.length <= maxLines) return content;
  const hidden = lines.length - maxLines;
  return `${lines.slice(0, maxLines).join('\n')}\n… ${hidden} more line${hidden === 1 ? '' : 's'} hidden`;
}

function entryPalette(entry: ConversationEntry): {
  border: string; background: string; label: string; content: string;
} {
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
