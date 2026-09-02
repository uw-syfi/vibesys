import {
  type BorderCharacters,
  type BorderSides,
  BoxRenderable,
  type CliRenderer,
  MarkdownRenderable,
  type SyntaxStyle,
  // The terminal mouse event, not the DOM global of the same name.
  type MouseEvent as TerminalMouseEvent,
  TextRenderable,
} from '@opentui/core';
import {hasRunEnded} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import type {ConversationEntry, SessionState} from '../session-model.js';
import {visibleConversation} from '../session-model.js';
import {promptPreview, toolCallPreview, toolResultPreview} from './previews.js';
import {createMarkdownBlockOptions, entryPalette, type MarkdownBlockOptions} from './styles.js';
import type {Theme} from './theme.js';

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

/** The one side of the card body the stripe is drawn on. */
const STRIPE_SIDE: BorderSides = 'left';

/**
 * A border set whose only reachable member is `vertical`: corners need two
 * adjacent sides and the stripe has one, so the rest are spaces because
 * `BorderCharacters` is a closed shape.
 */
function stripeChars(glyph: string): BorderCharacters {
  return {
    vertical: glyph,
    horizontal: ' ',
    topLeft: ' ',
    topRight: ' ',
    bottomLeft: ' ',
    bottomRight: ' ',
    topT: ' ',
    bottomT: ' ',
    leftT: ' ',
    rightT: ' ',
    cross: ' ',
  };
}

/**
 * A half block carries the role's hue at a glance while staying visibly not a
 * border: the card's rounded frame already owns the box-drawing vocabulary, so
 * a `│` inside it reads as a double frame. A full block would be the fill this
 * replaces, only narrower, and would compete with the frame for the eye; the
 * frame has to stay the loudest mark on the card because it carries the cursor.
 */
const ROLE_STRIPE = stripeChars('▌');

/** The same column, unpainted, for a card that carries no conversation role. */
const RESERVED_STRIPE = stripeChars(' ');

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
    // The bordered surface owns horizontal inset so transcript siblings, such
    // as a fixed footer, share the same content origin as these turn cards.
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
    const selection = this.#selectionFor(state);
    if (selection !== this.#renderedSelection) {
      // The cursor is drawn into the cards, so a change has to redraw them even
      // when the entries are identical.
      this.#renderedConversation = [];
      this.#renderedSelection = selection;
    }
    this.#selectedId = selection;
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

  #renderConversation(conversation: ConversationEntry[]): void {
    const entries = this.#windowed(conversation);
    if (
      sameEntries(entries, this.#renderedConversation) &&
      (entries.length > 0 || this.output.getChildren().length > 0)
    )
      return;
    if (isEntryPrefix(this.#renderedConversation, entries)) {
      for (const entry of entries.slice(this.#renderedConversation.length)) {
        const card = this.#renderEntry(entry);
        this.output.add(card);
        this.#renderedCards.push(card);
      }
      this.#renderedConversation = entries;
      return;
    }
    const revealed = entrySuffixOffset(this.#renderedConversation, entries);
    if (revealed > 0) {
      // The window grew backwards: only the newly revealed head needs cards.
      const cards: BoxRenderable[] = [];
      for (let index = revealed - 1; index >= 0; index -= 1) {
        const entry = entries[index];
        if (entry === undefined) continue;
        const card = this.#renderEntry(entry);
        this.output.add(card, 0);
        cards.unshift(card);
      }
      this.#renderedCards = [...cards, ...this.#renderedCards];
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
        content: this.#emptyContent,
        fg: this.#theme.textSubtle,
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

  #renderEntry(entry: ConversationEntry): BoxRenderable {
    const palette = entryPalette(entry, this.#theme);
    const selected = this.#selectedId === entry.id;
    // A status line reports on the run rather than speaking in it, so it has no
    // conversation role to stripe. It still reserves the column, and so still
    // lines up with the cards around it.
    const striped = entry.kind !== 'status';
    const card = new BoxRenderable(this.renderer, {
      id: `event-${entry.id}`,
      width: '100%',
      flexDirection: 'column',
      marginTop: 1,
      paddingRight: 1,
      border: entry.kind !== 'status',
      borderStyle: 'rounded',
      // The cursor is the card's border, and the role is the body's left edge
      // stripe. Painting the role as a fill instead put it across every cell
      // of the card, which reads as "this pane is highlighted" rather than
      // "this card is role X", and it outweighed the cursor sitting on top of
      // it. No fill at all: the card sits on the canvas.
      borderColor: selected ? this.#theme.borderFocus : palette.border,
      ...(this.#showsSelection
        ? {
            onMouseUp: () => {
              this.#onFocusRequest?.();
              if (entry.kind === 'prompt') this.#togglePrompt(entry.id);
              else {
                this.controller.selectNextEntry(0, entry.id);
                if (entry.kind === 'tool') this.#toggleTool(entry);
              }
            },
          }
        : entry.kind === 'prompt' || entry.kind === 'tool'
          ? {
              onMouseUp: () => {
                this.#onFocusRequest?.();
                if (entry.kind === 'prompt') this.#togglePrompt(entry.id);
                else this.#toggleTool(entry);
              },
            }
          : {}),
    });
    // Everything the card holds hangs off the body, and the stripe is the
    // body's left border. A border, rather than a stripe renderable or a glyph
    // prefixed to each line, because it reserves its column: the column is the
    // one the card's `paddingLeft` used to hold, so no text moves, and it costs
    // the same cell whether or not a glyph is painted in it, so a card with a
    // stripe and a card without still start their text in the same place. It
    // also runs the body's full height, which a card holding a markdown block
    // does not know in advance.
    const body = new BoxRenderable(this.renderer, {
      id: `event-${entry.id}-body`,
      width: '100%',
      flexDirection: 'column',
      border: [STRIPE_SIDE],
      customBorderChars: striped ? ROLE_STRIPE : RESERVED_STRIPE,
      // The role's label colour, not its raw accent. The theme derives the
      // label to clear its own contrast floor, so the stripe is legible in all
      // eight themes, including the two high-contrast ones where several raw
      // accents sit under 3:1 against the canvas. It is also the colour the
      // card's own heading uses, so the stripe repeats the role rather than
      // introducing a second colour for it.
      borderColor: palette.label,
    });
    card.add(body);
    const heading = new BoxRenderable(this.renderer, {
      id: `event-${entry.id}-heading`,
      width: '100%',
      height: 1,
      flexDirection: 'row',
      justifyContent: 'space-between',
    });
    heading.add(
      new TextRenderable(this.renderer, {
        content: `${selected ? '▸ ' : ''}${entry.label ?? entry.kind}`,
        fg: selected ? this.#theme.textStrong : palette.label,
        height: 1,
      }),
    );
    body.add(heading);
    if (this.#markdownKinds.has(entry.kind)) {
      this.#renderMarkdownEntry(body, entry);
    } else if (
      entry.kind === 'tool' &&
      (entry.toolCall !== undefined ||
        (entry.toolName !== undefined && entry.toolArguments !== undefined))
    ) {
      this.#renderToolTurn(body, entry);
    } else {
      const prompt =
        entry.kind === 'prompt'
          ? promptPreview(entry.content, this.#expandedPrompts.has(entry.id))
          : null;
      const output =
        !prompt &&
        (entry.kind === 'tool' || entry.kind === 'diagnostic' || entry.kind === 'subprocess')
          ? toolResultPreview(entry.content, entry.toolResult?.payload)
          : null;
      const content = prompt ? prompt.content : (output?.content ?? entry.content);
      body.add(new TextRenderable(this.renderer, {content, fg: palette.content, width: '100%'}));
      if (output?.collapsible) {
        const hidden =
          output.hiddenLines > 0
            ? `${output.hiddenLines} more line${output.hiddenLines === 1 ? '' : 's'}`
            : `${output.hiddenCharacters} more characters`;
        body.add(
          new TextRenderable(this.renderer, {
            content: `… ${hidden} hidden`,
            fg: this.#theme.info,
            width: '100%',
          }),
        );
      }
      if (prompt && (prompt.hiddenLines > 0 || this.#expandedPrompts.has(entry.id))) {
        body.add(
          new TextRenderable(this.renderer, {
            content: this.#expandedPrompts.has(entry.id)
              ? '▴ click to collapse'
              : `▾ ${prompt.hiddenLines} more lines · click to expand`,
            fg: this.#theme.info,
            width: '100%',
          }),
        );
      }
    }
    return card;
  }

  #renderMarkdownEntry(body: BoxRenderable, entry: ConversationEntry): void {
    const expanded = this.#expandedPrompts.has(entry.id);
    const preview =
      entry.kind === 'prompt'
        ? promptPreview(entry.content, expanded)
        : {content: entry.content, hiddenLines: 0};
    body.add(
      new MarkdownRenderable(this.renderer, {
        ...this.#markdownBlockOptions,
        content: preview.content,
        streaming: this.#markdownStreaming ?? !hasRunEnded(this.controller.state.core),
      }),
    );
    if (entry.kind === 'prompt' && (preview.hiddenLines > 0 || expanded)) {
      body.add(
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

  #renderToolTurn(body: BoxRenderable, entry: ConversationEntry): void {
    const toolCall =
      entry.toolName !== undefined && entry.toolArguments !== undefined
        ? toolCallPreview(entry.toolName, entry.toolArguments)
        : (entry.toolCall ?? '');
    const toolResponse = entry.toolResult?.content ?? entry.toolResponse;
    body.add(
      new TextRenderable(this.renderer, {
        content: toolCall.trimEnd(),
        fg: this.#theme.toolCall.foreground,
        bg: this.#theme.toolCall.background,
        width: '100%',
      }),
    );
    if (toolResponse) {
      const expanded = this.#expandedTools.has(entry.id);
      const response = toolResultPreview(toolResponse, entry.toolResult?.payload, expanded);
      body.add(
        new TextRenderable(this.renderer, {
          content: `← ${response.content}`,
          fg: this.#theme.toolResult.foreground,
          bg: this.#theme.toolResult.background,
          width: '100%',
        }),
      );
      if (response.collapsible) {
        const hidden =
          response.hiddenLines > 0
            ? `${response.hiddenLines} more line${response.hiddenLines === 1 ? '' : 's'}`
            : `${response.hiddenCharacters} more characters`;
        body.add(
          new TextRenderable(this.renderer, {
            content: expanded
              ? '▴ click or Enter to collapse response'
              : `▾ Show full response · ${hidden} · click or Enter`,
            fg: this.#theme.info,
            width: '100%',
          }),
        );
      }
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
