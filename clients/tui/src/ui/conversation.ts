import {
  BoxRenderable,
  type CliRenderer,
  MarkdownRenderable,
  type SyntaxStyle,
  TextRenderable,
} from '@opentui/core';
import type {SessionController} from '../session-controller.js';
import type {ConversationEntry, SessionState} from '../session-model.js';
import {visibleConversation} from '../session-model.js';
import {promptPreview, toolOutputPreview} from './previews.js';
import {entryPalette} from './styles.js';

export class ConversationView {
  readonly output: BoxRenderable;
  readonly #expandedPrompts = new Set<string>();
  #renderedConversation: ConversationEntry[] = [];

  constructor(
    private readonly renderer: CliRenderer,
    private readonly controller: SessionController,
    private readonly markdownStyle: SyntaxStyle,
  ) {
    this.output = new BoxRenderable(renderer, {
      id: 'output',
      width: '100%',
      flexDirection: 'column',
      paddingLeft: 1,
      paddingRight: 1,
    });
  }

  render(state: SessionState): void {
    this.#renderConversation(visibleConversation(state));
  }

  toggleLatestPrompt(): void {
    const latestPrompt = [...this.controller.state.conversation]
      .reverse()
      .find(entry => entry.kind === 'prompt');
    if (latestPrompt) this.#togglePrompt(latestPrompt.id);
  }

  #clear(): void {
    for (const child of [...this.output.getChildren()]) {
      this.output.remove(child);
      child.destroyRecursively();
    }
  }

  #renderConversation(entries: ConversationEntry[]): void {
    if (entries === this.#renderedConversation) return;
    this.#renderedConversation = entries;
    this.#clear();
    if (entries.length === 0) {
      this.output.add(
        new TextRenderable(this.renderer, {
          content: 'Waiting for run events…',
          fg: '#64748b',
        }),
      );
      return;
    }
    for (const entry of entries) this.output.add(this.#renderEntry(entry));
  }

  #togglePrompt(id: string): void {
    if (this.#expandedPrompts.has(id)) this.#expandedPrompts.delete(id);
    else this.#expandedPrompts.add(id);
    this.#renderedConversation = [];
    this.#renderConversation(this.controller.state.conversation);
  }

  #renderEntry(entry: ConversationEntry): BoxRenderable {
    const palette = entryPalette(entry);
    const card = new BoxRenderable(this.renderer, {
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
      ...(entry.kind === 'prompt' ? {onMouseUp: () => this.#togglePrompt(entry.id)} : {}),
    });
    card.add(
      new TextRenderable(this.renderer, {
        content: entry.label ?? entry.kind,
        fg: palette.label,
        height: 1,
      }),
    );
    if (entry.kind === 'assistant' || entry.kind === 'prompt') {
      this.#renderMarkdownEntry(card, entry);
    } else if (entry.kind === 'tool' && entry.toolCall) {
      this.#renderToolTurn(card, entry);
    } else {
      const content =
        entry.kind === 'tool' || entry.kind === 'diagnostic' || entry.kind === 'subprocess'
          ? toolOutputPreview(entry.content)
          : entry.content;
      card.add(new TextRenderable(this.renderer, {content, fg: palette.content, width: '100%'}));
    }
    return card;
  }

  #renderMarkdownEntry(card: BoxRenderable, entry: ConversationEntry): void {
    const expanded = this.#expandedPrompts.has(entry.id);
    const preview =
      entry.kind === 'prompt'
        ? promptPreview(entry.content, expanded)
        : {content: entry.content, hiddenLines: 0};
    card.add(
      new MarkdownRenderable(this.renderer, {
        content: preview.content,
        syntaxStyle: this.markdownStyle,
        conceal: true,
        streaming: !this.controller.state.terminal,
        width: '100%',
      }),
    );
    if (entry.kind === 'prompt' && (preview.hiddenLines > 0 || expanded)) {
      card.add(
        new TextRenderable(this.renderer, {
          content: expanded
            ? '▴ click or Ctrl+P to collapse'
            : `▾ ${preview.hiddenLines} more lines · click or Ctrl+P to expand`,
          fg: '#60a5fa',
          width: '100%',
        }),
      );
    }
  }

  #renderToolTurn(card: BoxRenderable, entry: ConversationEntry): void {
    card.add(
      new TextRenderable(this.renderer, {
        content: entry.toolCall?.trimEnd() ?? '',
        fg: '#dbeafe',
        bg: '#1e3a8a',
        width: '100%',
      }),
    );
    if (entry.toolResponse) {
      card.add(
        new TextRenderable(this.renderer, {
          content: toolOutputPreview(entry.toolResponse),
          fg: '#a1a1aa',
          bg: '#18181b',
          width: '100%',
        }),
      );
    }
  }
}
