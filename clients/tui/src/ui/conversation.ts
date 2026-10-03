import {
  BoxRenderable,
  bold,
  type CliRenderer,
  CodeRenderable,
  fg,
  MarkdownRenderable,
  StyledText,
  type SyntaxStyle,
  // The terminal mouse event, not the DOM global of the same name.
  type MouseEvent as TerminalMouseEvent,
  type TextChunk,
  TextRenderable,
} from '@opentui/core';
import {hasRunEnded} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import type {ConversationEntry, SessionState} from '../session-model.js';
import {visibleConversation} from '../session-model.js';
import {
  type CollapsiblePreview,
  promptPreview,
  toolCallPreview,
  toolResultPreview,
} from './previews.js';
import {
  codeSurface,
  createMarkdownBlockOptions,
  drawOnCodeSurface,
  type EntryPalette,
  entryPalette,
  type MarkdownBlockOptions,
} from './styles.js';
import {ensureContrast, RUN_DIVIDER_MIN_CONTRAST, type Theme} from './theme.js';

export interface ConversationViewOptions {
  selectConversation?: (state: SessionState) => ConversationEntry[];
  emptyContent?: string;
  /**
   * Entry kinds drawn through the markdown pipeline; every other kind draws
   * its content verbatim. Defaults to all prose-bearing kinds, which is what
   * the main transcript wants; the chat narrows this to assistant answers so
   * the operator's own typed markers are never concealed as markup.
   */
  markdownKinds?: readonly ConversationEntry['kind'][];
  /**
   * Forces the markdown parser's incremental mode. By default cards finalize
   * once the run is terminal, but finalized parsing is asynchronous and draws
   * nothing until a later redraw; a view that still receives entries after the
   * run ends (the chat answering a post-mortem question) sets this so a fresh
   * answer is never an invisible card waiting on a keypress.
   */
  markdownStreaming?: boolean;
  /** Whether this view draws the entry cursor. */
  showsSelection?: boolean;
  /** Gives the containing semantic pane focus when any conversation surface is clicked. */
  onFocusRequest?: () => void;
  /**
   * Asked for entries older than the rendered window when a scroll gesture
   * reaches back past it. The container owns scrolling, so it decides how to
   * keep the viewport steady across the newly materialized cards.
   */
  onRevealOlder?: () => void;
}

/**
 * Above this many visible entries the first paint renders only a tail window.
 * Below it a full build costs a few hundred milliseconds, which is not worth
 * the windowing bookkeeping.
 */
const CONVERSATION_WINDOW_THRESHOLD = 2_000;

/** How many entries a windowed paint materializes, and grows by on demand. */
const CONVERSATION_WINDOW = 200;

type ConversationPatch =
  | {kind: 'same'}
  | {kind: 'append'; entries: ConversationEntry[]; from: number}
  | {kind: 'prepend'; entries: ConversationEntry[]; revealed: number}
  | {kind: 'replace'; entries: ConversationEntry[]; index: number}
  | {kind: 'rebuild'; entries: ConversationEntry[]};

export class ConversationView {
  readonly output: BoxRenderable;
  #theme: Theme;
  #markdownBlockOptions: MarkdownBlockOptions;
  readonly #expandedPrompts = new Set<string>();
  readonly #expandedTools = new Set<string>();
  readonly #selectConversation: (state: SessionState) => ConversationEntry[];
  #emptyContent: string;
  readonly #markdownKinds: ReadonlySet<ConversationEntry['kind']>;
  readonly #markdownStreaming: boolean | undefined;
  readonly #showsSelection: boolean;
  readonly #onFocusRequest: (() => void) | undefined;
  #renderedConversation: ConversationEntry[] = [];
  #renderedCards: BoxRenderable[] = [];
  /** The selection the cards in `#renderedCards` currently depict. */
  #renderedSelection: string | null = null;
  #selectedId: string | null = null;
  /** First visible entry the window renders; 0 once it covers everything. */
  #windowStart = 0;
  /**
   * The entry `#windowStart` pointed at when it was last resolved. It survives
   * the invalidations that clear `#renderedConversation`, so expanding the
   * window is not undone by a selection move, a theme swap, or a toggle.
   */
  #windowAnchor: ConversationEntry | null = null;

  constructor(
    private readonly renderer: CliRenderer,
    private readonly controller: SessionController,
    markdownStyle: SyntaxStyle,
    theme: Theme,
    options: ConversationViewOptions = {},
  ) {
    this.#markdownBlockOptions = createMarkdownBlockOptions(theme, markdownStyle);
    this.#theme = theme;
    this.#selectConversation = options.selectConversation ?? visibleConversation;
    this.#emptyContent = options.emptyContent ?? 'Waiting for run events…';
    this.#markdownKinds = new Set(options.markdownKinds ?? ['assistant', 'prompt', 'user']);
    this.#markdownStreaming = options.markdownStreaming;
    this.#showsSelection = options.showsSelection ?? false;
    this.#onFocusRequest = options.onFocusRequest;
    const onRevealOlder = options.onRevealOlder;
    // Horizontal inset belongs to the pane (or the chat surface's own frame)
    // that contains this view, not to the cards below: see #565. A card that
    // padded itself again on top of that sat one layer deeper than a
    // non-card sibling in the same pane, such as the empty-transcript
    // message added directly below.
    this.output = new BoxRenderable(renderer, {
      id: 'output',
      width: '100%',
      flexDirection: 'column',
      ...(this.#onFocusRequest === undefined ? {} : {onMouseUp: this.#onFocusRequest}),
      // Not consumed: the containing scroll box still handles the wheel. This
      // only notices that the reader is heading into history.
      ...(onRevealOlder === undefined
        ? {}
        : {
            onMouseScroll: (event: TerminalMouseEvent): void => {
              if (event.scroll?.direction === 'up' && this.hasOlderEntries()) onRevealOlder();
            },
          }),
    });
  }

  /** Whether entries older than the rendered window are still unmaterialized. */
  hasOlderEntries(): boolean {
    return this.#windowStart > 0;
  }

  /**
   * Materializes the next block of older entries, returning whether the window
   * actually grew. Callers own any scroll compensation for the added cards.
   */
  revealOlderEntries(): boolean {
    if (this.#windowStart === 0) return false;
    this.#windowStart = Math.max(0, this.#windowStart - CONVERSATION_WINDOW);
    const entries = this.#selectConversation(this.controller.state);
    this.#windowAnchor = entries[this.#windowStart] ?? null;
    this.#renderConversation(entries);
    return true;
  }

  render(state: SessionState): void {
    this.#selectedId = this.#selectionFor(state);
    this.#renderConversation(this.#selectConversation(state));
  }

  /** The transcript owns the entry cursor; the chat panes never show one. */
  #selectionFor(state: SessionState): string | null {
    return this.#showsSelection ? state.selectedEntryId : null;
  }

  /**
   * What an empty transcript says. A round with no turns because it has not run
   * is a different thing from a round whose turns have not arrived, and the
   * operator should not have to guess which one they are looking at.
   */
  setEmptyContent(content: string): void {
    if (content === this.#emptyContent) return;
    this.#emptyContent = content;
    this.#renderedConversation = [];
  }

  /** Scrolls the selected card into view; the viewport owns the scrolling. */
  selectedCard(): BoxRenderable | null {
    if (this.#selectedId === null) return null;
    const index = this.#renderedConversation.findIndex(entry => entry.id === this.#selectedId);
    return index === -1 ? null : (this.#renderedCards[index] ?? null);
  }

  applyTheme(theme: Theme, markdownStyle: SyntaxStyle): void {
    this.#theme = theme;
    this.#markdownBlockOptions = createMarkdownBlockOptions(theme, markdownStyle);
    this.#clear();
    this.#renderedConversation = [];
  }

  toggleLatestPrompt(): void {
    const latestPrompt = [...this.#selectConversation(this.controller.state)]
      .reverse()
      .find(entry => entry.kind === 'prompt');
    if (latestPrompt) this.#togglePrompt(latestPrompt.id);
  }

  toggleSelectedTool(): boolean {
    if (this.#selectedId === null) return false;
    const entry = this.#selectConversation(this.controller.state).find(
      candidate => candidate.id === this.#selectedId,
    );
    if (entry?.kind !== 'tool') return false;
    return this.#toggleTool(entry);
  }

  #clear(): void {
    for (const child of [...this.output.getChildren()]) {
      this.output.remove(child);
      child.destroyRecursively();
    }
    this.#renderedCards = [];
  }

  /**
   * The slice of the conversation that gets cards.
   *
   * A boot against a long-lived run lands 20k entries in one frame, and one
   * card is several native renderables, so building them all blocks the first
   * paint (and, past roughly 6k cards, exhausts the renderer's native
   * handles). The window keeps the tail immediate; older entries materialize
   * when the reader scrolls back for them.
   *
   * The anchor keeps `#windowStart` pointing at the same entry across renders,
   * so live appends stay a prefix extension of what is on screen and still
   * take the incremental path.
   */
  #windowed(entries: ConversationEntry[]): ConversationEntry[] {
    if (entries.length <= CONVERSATION_WINDOW_THRESHOLD) {
      this.#windowStart = 0;
      // Anchored even while everything is rendered, so a backfill that pushes
      // the transcript past the threshold keeps the same first entry instead of
      // windowing the reader onto the tail.
      this.#windowAnchor = entries[0] ?? null;
      return entries;
    }
    if (this.#windowAnchor === null || entries[this.#windowStart] !== this.#windowAnchor) {
      // History loaded on demand is prepended, which shifts every index without
      // changing what the reader is looking at. Follow the anchor to where it
      // moved rather than snapping back to the tail, which would throw the
      // reader out of the history they scrolled into. Identity is checked first
      // because it is the common case; the search runs only when it fails.
      const anchorId = this.#windowAnchor?.id;
      const moved = anchorId === undefined ? -1 : entries.findIndex(entry => entry.id === anchorId);
      this.#windowStart = moved === -1 ? entries.length - CONVERSATION_WINDOW : moved;
    }
    if (this.#selectedId !== null) {
      // A cursor above the window has to stay reachable and revealable.
      const selected = entries.findIndex(entry => entry.id === this.#selectedId);
      if (selected !== -1 && selected < this.#windowStart) this.#windowStart = selected;
    }
    this.#windowAnchor = entries[this.#windowStart] ?? null;
    return entries.slice(this.#windowStart);
  }

  /**
   * Redraws the cards a selection move touches. The cursor is drawn into the
   * cards, but only into the two it moves between: every other card renders
   * identically under either selection, so the move costs two card
   * replacements instead of the full-window rebuild it used to force. This
   * runs before the structural comparison so the rendered cards agree with
   * `#selectedId` again, which is the invariant every incremental path below
   * assumes; an endpoint that is not rendered yet (it sits in history the
   * window is about to reveal, or in entries about to be appended) is built by
   * whichever path materializes it, since they all draw with `#selectedId`.
   */
  #syncSelectionCards(): void {
    if (this.#selectedId === this.#renderedSelection) return;
    for (const id of [this.#renderedSelection, this.#selectedId]) {
      if (id === null) continue;
      const index = this.#renderedConversation.findIndex(entry => entry.id === id);
      if (index !== -1) this.#replaceCard(index, this.#renderedConversation);
    }
    this.#renderedSelection = this.#selectedId;
  }

  #renderConversation(conversation: ConversationEntry[]): void {
    const entries = this.#windowed(conversation);
    this.#syncSelectionCards();
    const patch = planConversationPatch(
      this.#renderedConversation,
      entries,
      this.output.getChildren().length > 0,
    );
    if (patch.kind === 'same') return;
    if (patch.kind === 'append') {
      this.#appendEntries(patch.entries, patch.from);
      return;
    }
    if (patch.kind === 'prepend') {
      this.#prependEntries(patch.entries, patch.revealed);
      return;
    }
    if (patch.kind === 'replace') {
      this.#replaceEntry(patch.entries, patch.index);
      return;
    }
    this.#rebuildConversation(patch.entries);
  }

  #appendEntries(entries: ConversationEntry[], from: number): void {
    for (let index = from; index < entries.length; index += 1) {
      const entry = entries[index];
      if (entry === undefined) continue;
      const card = this.#renderEntry(entry, entries[index - 1]);
      this.output.add(card);
      this.#renderedCards.push(card);
    }
    this.#renderedConversation = entries;
  }

  #prependEntries(entries: ConversationEntry[], revealed: number): void {
    const cards: BoxRenderable[] = [];
    for (let index = revealed - 1; index >= 0; index -= 1) {
      const entry = entries[index];
      if (entry === undefined) continue;
      const card = this.#renderEntry(entry, entries[index - 1]);
      this.output.add(card, 0);
      cards.unshift(card);
    }
    this.#renderedCards = [...cards, ...this.#renderedCards];
    this.#renderedConversation = entries;
    const head = entries[revealed];
    const above = entries[revealed - 1];
    if (head !== undefined && above !== undefined && sameSpeaker(above, head))
      this.#replaceCard(revealed, entries);
  }

  #replaceEntry(entries: ConversationEntry[], index: number): void {
    const before = this.#renderedConversation[index];
    const entry = entries[index];
    if (this.#renderedCards[index] === undefined || before === undefined || entry === undefined) {
      this.#rebuildConversation(entries);
      return;
    }
    this.#renderedConversation = entries;
    this.#replaceCard(index, entries);
    if (!sameSpeaker(before, entry)) this.#replaceCard(index + 1, entries);
  }

  #rebuildConversation(entries: ConversationEntry[]): void {
    this.#clear();
    this.#renderedConversation = entries;
    if (entries.length === 0) {
      this.output.add(
        new TextRenderable(this.renderer, {
          content: this.#emptyContent,
          fg: this.#theme.textSubtle,
          marginLeft: 1,
        }),
      );
      return;
    }
    for (const [index, entry] of entries.entries()) {
      const card = this.#renderEntry(entry, entries[index - 1]);
      this.output.add(card);
      this.#renderedCards.push(card);
    }
  }

  /** Re-renders one already-rendered card in place, chrome included. */
  #replaceCard(index: number, entries: ConversationEntry[]): void {
    const previousCard = this.#renderedCards[index];
    const entry = entries[index];
    if (previousCard === undefined || entry === undefined) return;
    this.output.remove(previousCard);
    previousCard.destroyRecursively();
    const card = this.#renderEntry(entry, entries[index - 1]);
    this.output.add(card, index);
    this.#renderedCards[index] = card;
  }

  #togglePrompt(id: string): void {
    if (this.#expandedPrompts.has(id)) this.#expandedPrompts.delete(id);
    else this.#expandedPrompts.add(id);
    this.#renderedConversation = [];
    this.#renderConversation(this.#selectConversation(this.controller.state));
  }

  #toggleTool(entry: ConversationEntry): boolean {
    const response = entry.toolResult?.content ?? entry.toolResponse;
    if (
      response === undefined ||
      !toolResultPreview(response, entry.toolResult?.payload).collapsible
    )
      return false;
    if (this.#expandedTools.has(entry.id)) this.#expandedTools.delete(entry.id);
    else this.#expandedTools.add(entry.id);
    this.#renderedConversation = [];
    this.#renderConversation(this.#selectConversation(this.controller.state));
    return true;
  }

  /**
   * Draws one entry. `previous` is the entry rendered directly above it, or
   * `undefined` for the first one in the view, which is what decides whether
   * this entry opens a speaker run and so draws the divider and heading.
   */
  #renderEntry(entry: ConversationEntry, previous: ConversationEntry | undefined): BoxRenderable {
    const palette = entryPalette(entry, this.#theme);
    const selected = this.#selectedId === entry.id;
    const opensRun = previous === undefined || !sameSpeaker(previous, entry);
    const card = new BoxRenderable(this.renderer, {
      ...this.#entryOptions(entry, opensRun, selected),
    });
    if (opensRun) this.#renderHeading(card, entry, palette, selected);
    this.#renderBody(card, entry, palette);
    return card;
  }

  #entryOptions(entry: ConversationEntry, opensRun: boolean, selected: boolean) {
    const borderSides: ('top' | 'left')[] = [];
    if (opensRun) borderSides.push('top');
    if (selected) borderSides.push('left');
    const onMouseUp = this.#entryMouseHandler(entry);
    return {
      id: `event-${entry.id}`,
      width: '100%' as const,
      flexDirection: 'column' as const,
      ...(selected ? {} : {paddingLeft: 1}),
      ...(borderSides.length === 0
        ? {border: false}
        : {
            border: borderSides,
            borderStyle: 'single' as const,
            borderColor: selected
              ? this.#theme.borderFocus
              : ensureContrast(this.#theme.border, this.#theme.canvas, RUN_DIVIDER_MIN_CONTRAST),
          }),
      ...(onMouseUp === undefined ? {} : {onMouseUp}),
    };
  }

  #entryMouseHandler(entry: ConversationEntry): (() => void) | undefined {
    if (!this.#showsSelection && entry.kind !== 'prompt' && entry.kind !== 'tool') return undefined;
    return () => {
      this.#onFocusRequest?.();
      if (entry.kind === 'prompt') this.#togglePrompt(entry.id);
      else if (this.#showsSelection) {
        this.controller.selectNextEntry(0, entry.id);
        if (entry.kind === 'tool') this.#toggleTool(entry);
      } else this.#toggleTool(entry);
    };
  }

  #renderHeading(
    card: BoxRenderable,
    entry: ConversationEntry,
    palette: EntryPalette,
    selected: boolean,
  ): void {
    const heading = new BoxRenderable(this.renderer, {
      id: `event-${entry.id}-heading`,
      width: '100%',
      height: 1,
      flexDirection: 'row',
      justifyContent: 'space-between',
    });
    const {role, runId} = speaker(entry);
    const color = selected ? this.#theme.textStrong : palette.label;
    heading.add(new TextRenderable(this.renderer, {content: role, fg: color, height: 1}));
    if (runId !== null)
      heading.add(new TextRenderable(this.renderer, {content: runId, fg: color, height: 1}));
    card.add(heading);
  }

  #renderBody(card: BoxRenderable, entry: ConversationEntry, palette: EntryPalette): void {
    if (this.#markdownKinds.has(entry.kind)) {
      this.#renderMarkdownEntry(card, entry);
      return;
    }
    if (
      entry.kind === 'tool' &&
      (entry.toolCall !== undefined ||
        (entry.toolName !== undefined && entry.toolArguments !== undefined))
    ) {
      this.#renderToolTurn(card, entry);
      return;
    }
    this.#renderPlainEntry(card, entry, palette);
  }

  #renderPlainEntry(card: BoxRenderable, entry: ConversationEntry, palette: EntryPalette): void {
    const prompt =
      entry.kind === 'prompt'
        ? promptPreview(entry.content, this.#expandedPrompts.has(entry.id))
        : null;
    const output =
      prompt === null &&
      (entry.kind === 'tool' || entry.kind === 'diagnostic' || entry.kind === 'subprocess')
        ? toolResultPreview(entry.content, entry.toolResult?.payload)
        : null;
    const content = prompt?.content ?? output?.content ?? entry.content;
    card.add(
      new TextRenderable(this.renderer, {
        content: styleTranscriptText(content, palette, this.#theme),
        fg: palette.content,
        width: '100%',
        wrapMode: 'word',
      }),
    );
    if (entry.command !== undefined) this.#renderCommand(card, entry.command);
    if (output?.collapsible) this.#renderHiddenOutput(card, output);
    if (prompt !== null && (prompt.hiddenLines > 0 || this.#expandedPrompts.has(entry.id)))
      this.#renderPromptHint(card, prompt.hiddenLines, this.#expandedPrompts.has(entry.id));
  }

  #renderCommand(card: BoxRenderable, command: string): void {
    const commandBlock = new CodeRenderable(this.renderer, {
      content: command,
      filetype: 'bash',
      syntaxStyle: this.#markdownBlockOptions.syntaxStyle,
      width: '100%',
      wrapMode: 'char',
    });
    drawOnCodeSurface(commandBlock, codeSurface(this.#theme));
    card.add(commandBlock);
  }

  #renderHiddenOutput(
    card: BoxRenderable,
    output: {hiddenLines: number; hiddenCharacters: number},
  ): void {
    const hidden =
      output.hiddenLines > 0
        ? `${output.hiddenLines} more line${output.hiddenLines === 1 ? '' : 's'}`
        : `${output.hiddenCharacters} more characters`;
    card.add(
      new TextRenderable(this.renderer, {
        content: `… ${hidden} hidden`,
        fg: this.#theme.info,
        width: '100%',
      }),
    );
  }

  #renderPromptHint(card: BoxRenderable, hiddenLines: number, expanded: boolean): void {
    card.add(
      new TextRenderable(this.renderer, {
        content: expanded ? '▴ click to collapse' : `▾ ${hiddenLines} more lines · click to expand`,
        fg: this.#theme.info,
        width: '100%',
      }),
    );
  }
  #renderMarkdownEntry(card: BoxRenderable, entry: ConversationEntry): void {
    const expanded = this.#expandedPrompts.has(entry.id);
    const preview =
      entry.kind === 'prompt'
        ? promptPreview(entry.content, expanded)
        : {content: entry.content, hiddenLines: 0};
    card.add(
      new MarkdownRenderable(this.renderer, {
        ...this.#markdownBlockOptions,
        content: preview.content,
        streaming: this.#markdownStreaming ?? !hasRunEnded(this.controller.state.core),
      }),
    );
    if (entry.kind === 'prompt' && (preview.hiddenLines > 0 || expanded)) {
      card.add(
        new TextRenderable(this.renderer, {
          content: expanded
            ? '▴ click or Ctrl+P to collapse'
            : `▾ ${preview.hiddenLines} more lines · click or Ctrl+P to expand`,
          fg: this.#theme.info,
          width: '100%',
        }),
      );
    }
  }

  #renderToolTurn(card: BoxRenderable, entry: ConversationEntry): void {
    const toolCall = toolCallText(entry);
    const toolResponse = entry.toolResult?.content ?? entry.toolResponse;
    card.add(
      new TextRenderable(this.renderer, {
        content: toolCall.trimEnd(),
        fg: this.#theme.toolCall.foreground,
        bg: this.#theme.toolCall.background,
        width: '100%',
        // A shell command is longer than the card is wide more often than not.
        // Wrapping keeps the whole command readable; clipping it at the border
        // hides exactly the flags and paths that say what ran.
        wrapMode: 'word',
      }),
    );
    if (toolResponse) {
      const expanded = this.#expandedTools.has(entry.id);
      const response = toolResultPreview(toolResponse, entry.toolResult?.payload, expanded);
      card.add(
        new TextRenderable(this.renderer, {
          content: `← ${response.content}`,
          fg: this.#theme.toolResult.foreground,
          bg: this.#theme.toolResult.background,
          width: '100%',
          // Expanded output is stdout and stderr verbatim, which is wider than
          // the card whenever a compiler or a test runner produced it.
          wrapMode: 'word',
        }),
      );
      if (response.collapsible) {
        card.add(
          new TextRenderable(this.renderer, {
            content: expanded
              ? '▴ click or Enter to collapse response'
              : `▾ Show full response · ${hiddenToolResponseSize(response)} · click or Enter`,
            fg: this.#theme.info,
            width: '100%',
          }),
        );
      }
    }
  }
}

function toolCallText(entry: ConversationEntry): string {
  if (entry.toolName !== undefined && entry.toolArguments !== undefined) {
    return toolCallPreview(entry.toolName, entry.toolArguments);
  }
  return entry.toolCall ?? '';
}

function hiddenToolResponseSize(response: CollapsiblePreview): string {
  if (response.hiddenLines === 0) return `${response.hiddenCharacters} more characters`;
  const unit = response.hiddenLines === 1 ? 'line' : 'lines';
  return `${response.hiddenLines} more ${unit}`;
}

/**
 * A bracketed source tag at the start of a line: `[git-tracking]`,
 * `[framework-validation]`. Legacy adapter only: since #692 the backend emits
 * typed framework events instead of bracket-tagged log text, so this and
 * `styleSourceTags` survive solely for journals recorded before #692.
 */
const SOURCE_TAG = /^\[[A-Za-z0-9][\w-]*\]/;

/**
 * The verdict a gate prints: `[framework-validation] PASS`,
 * `[framework-benchmark] FAIL: ...`. Whole words, so `PASSED` and a path
 * component spelled `fail` are left alone.
 */
const VERDICT = /\bPASS\b|\bFAIL\b/g;

/**
 * Emphasizes the two words in `text` a reader is actually scanning for, and
 * draws everything around them in the entry's content color.
 *
 * `PASS` and `FAIL` are the outcome of a run, and after the transcript stopped
 * spending colour on role they are close to the only saturated thing left on
 * the line. The word is the non-colour channel: green and red alone say nothing
 * to the ~8% of men with red-green CVD, and the two high-contrast themes barely
 * have a palette to say it with.
 */
function pushBody(chunks: TextChunk[], text: string, palette: EntryPalette, theme: Theme): boolean {
  let cursor = 0;
  let emphasized = false;
  for (const match of text.matchAll(VERDICT)) {
    if (match.index > cursor) chunks.push(fg(palette.content)(text.slice(cursor, match.index)));
    const role = match[0] === 'PASS' ? theme.conversation.success : theme.conversation.failure;
    chunks.push(bold(fg(role.label)(match[0])));
    cursor = match.index + match[0].length;
    emphasized = true;
  }
  if (cursor < text.length) chunks.push(fg(palette.content)(text.slice(cursor)));
  return emphasized;
}

/**
 * Colors the parts of a transcript line that are not the line's own prose.
 *
 * A leading bracketed source tag recedes into the theme's muted text, and any
 * `PASS` or `FAIL` is lifted into its verdict colour and bolded. Everything
 * else stays in the entry's content color. `SOURCE_TAG` is anchored to the
 * start of the line, so a bracket elsewhere in a line (`see [x] here`) is left
 * alone.
 *
 * The tags used to take the card's label colour, which put a saturated
 * 24-column prefix on nearly every subprocess line. A marker that appears on
 * almost every row marks nothing, so it is drawn as what it is: a frequent,
 * low-information prefix that should recede.
 *
 * Returns `content` unchanged when no line carries a tag or a verdict, so an
 * ordinary entry keeps rendering as the single content-colored string it always
 * has. Exported so the line-splitting and anchoring are tested directly, the
 * way previews.ts exports `unwrapShellCommand` for the same reason.
 */
export function styleTranscriptText(
  content: string,
  palette: EntryPalette,
  theme: Theme,
): StyledText | string {
  const lines = content.split('\n');
  const chunks: TextChunk[] = [];
  let styled = false;
  lines.forEach((line, index) => {
    const tag = SOURCE_TAG.exec(line);
    if (tag !== null) {
      chunks.push(fg(theme.textMuted)(tag[0]));
      styled = true;
    }
    const rest = tag === null ? line : line.slice(tag[0].length);
    if (pushBody(chunks, rest, palette, theme)) styled = true;
    if (index < lines.length - 1) chunks.push(fg(palette.content)('\n'));
  });
  return styled ? new StyledText(chunks) : content;
}

/**
 * Who an entry is from, as the heading splits it: an entry that names both an
 * agent and a round is that pair, and anything else is its single label. One
 * definition, so the heading and the run grouping cannot disagree about where
 * a run ends.
 */
function speaker(entry: ConversationEntry): {role: string; runId: string | null} {
  return entry.agentKind !== undefined && entry.roundLabel !== undefined
    ? {role: entry.agentKind, runId: entry.roundLabel}
    : {role: entry.label ?? entry.kind, runId: null};
}

function sameSpeaker(left: ConversationEntry, right: ConversationEntry): boolean {
  const before = speaker(left);
  const after = speaker(right);
  return before.role === after.role && before.runId === after.runId;
}

function sameEntries(left: ConversationEntry[], right: ConversationEntry[]): boolean {
  return left.length === right.length && left.every((entry, index) => entry === right[index]);
}

function planConversationPatch(
  rendered: ConversationEntry[],
  entries: ConversationEntry[],
  hasOutput: boolean,
): ConversationPatch {
  if (sameEntries(entries, rendered) && (entries.length > 0 || hasOutput)) return {kind: 'same'};
  if (isEntryPrefix(rendered, entries)) return {kind: 'append', entries, from: rendered.length};
  const revealed = entrySuffixOffset(rendered, entries);
  if (revealed > 0) return {kind: 'prepend', entries, revealed};
  const changedIndex = singleChangedEntryIndex(rendered, entries);
  if (changedIndex !== -1) return {kind: 'replace', entries, index: changedIndex};
  return {kind: 'rebuild', entries};
}

function isEntryPrefix(prefix: ConversationEntry[], entries: ConversationEntry[]): boolean {
  return (
    prefix.length > 0 &&
    prefix.length < entries.length &&
    prefix.every((entry, index) => entry === entries[index])
  );
}

/** How many entries were revealed ahead of an unchanged rendered tail, or 0. */
function entrySuffixOffset(rendered: ConversationEntry[], entries: ConversationEntry[]): number {
  const offset = entries.length - rendered.length;
  if (rendered.length === 0 || offset <= 0) return 0;
  return rendered.every((entry, index) => entry === entries[index + offset]) ? offset : 0;
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
