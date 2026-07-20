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
  #renderedCards: BoxRenderable[] = [];

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
    const visible = visibleConversation(this.controller.state);
    const latestPrompt = [...visible].reverse().find(entry => entry.kind === 'prompt');
    if (latestPrompt) this.#togglePrompt(latestPrompt.id, visible);
  }

  #clear(): void {
    for (const child of [...this.output.getChildren()]) {
      this.output.remove(child);
      child.destroyRecursively();
    }
    this.#renderedCards = [];
  }

  #renderConversation(entries: ConversationEntry[]): void {
    if (sameEntries(entries, this.#renderedConversation)) return;
    if (isEntryPrefix(this.#renderedConversation, entries)) {
      for (const entry of entries.slice(this.#renderedConversation.length)) {
        const card = this.#renderEntry(entry);
        this.output.add(card);
        this.#renderedCards.push(card);
      }
      this.#renderedConversation = entries;
      return;
    }
    const changedIndex = singleChangedEntryIndex(this.#renderedConversation, entries);
    if (changedIndex !== -1) {
      const previousCard = this.#renderedCards[changedIndex];
      const entry = entries[changedIndex];
      if (previousCard !== undefined && entry !== undefined) {
        this.output.remove(previousCard);
        previousCard.destroyRecursively();
        const card = this.#renderEntry(entry);
        this.output.add(card, changedIndex);
        this.#renderedCards[changedIndex] = card;
        this.#renderedConversation = entries;
        return;
      }
    }
    this.#clear();
    this.#renderedConversation = entries;
    if (entries.length === 0) {
      const card = new TextRenderable(this.renderer, {
        content: 'Waiting for run events…',
        fg: '#64748b',
      });
      this.output.add(card);
      return;
    }
    for (const entry of entries) {
      const card = this.#renderEntry(entry);
      this.output.add(card);
      this.#renderedCards.push(card);
    }
  }

  #togglePrompt(id: string, visible = visibleConversation(this.controller.state)): void {
    if (this.#expandedPrompts.has(id)) this.#expandedPrompts.delete(id);
    else this.#expandedPrompts.add(id);
    this.#renderedConversation = [];
    this.#renderConversation(visible);
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

function sameEntries(left: ConversationEntry[], right: ConversationEntry[]): boolean {
  return left.length === right.length && left.every((entry, index) => entry === right[index]);
}

function isEntryPrefix(prefix: ConversationEntry[], entries: ConversationEntry[]): boolean {
  return (
    prefix.length > 0 &&
    prefix.length < entries.length &&
    prefix.every((entry, index) => entry === entries[index])
  );
}

function singleChangedEntryIndex(
  previous: ConversationEntry[],
  entries: ConversationEntry[],
): number {
  if (previous.length === 0 || previous.length !== entries.length) return -1;
  let changedIndex = -1;
  for (let index = 0; index < entries.length; index += 1) {
    if (previous[index] === entries[index]) continue;
    if (changedIndex !== -1 || previous[index]?.id !== entries[index]?.id) return -1;
    changedIndex = index;
  }
  return changedIndex;
}
