import {
  BoxRenderable,
  type CliRenderer,
  ScrollBoxRenderable,
  type SyntaxStyle,
} from '@opentui/core';
import type {SessionController} from '../session-controller.js';
import {
  type ConversationEntry,
  chatPaneFocused,
  chatThreadHeading,
  type SessionState,
} from '../session-model.js';
import {ChatComposerView, type ChatDraft} from './chat-composer.js';
import {ConversationView} from './conversation.js';
import {LOG_CLAIM_PANEL_WIDTH, LOG_COMPACT_PANEL_WIDTH} from './experiment-log.js';
import {applyPaneFocus, paneBorderColor, paneBorderStyle, paneTitle} from './focus.js';
import type {Theme} from './theme.js';

/**
 * Columns the chat needs before a question and its answer read as prose rather
 * than as a column of fragments.
 */
const CHAT_PANE_MIN = 25;
const CHAT_PANE_MAX = 52;

/** Stands in until the first render names the active thread. */
const CHAT_PANE_TITLE = 'Experiment chat';

/**
 * The chat is part of the landing view, so it docks wherever the table is still
 * usable beside it rather than only where the table keeps every column. Below
 * that the table has the row to itself and the chat opens over it as a modal,
 * which is the one case where two columns would both be unreadable.
 */
export const MIN_DOCK_WIDTH = LOG_COMPACT_PANEL_WIDTH + CHAT_PANE_MIN;

/** True when the terminal can carry the chat beside a usable table. */
export function chatDockFits(terminalWidth: number, rightPaneWidth = 0): boolean {
  return terminalWidth - rightPaneWidth - CHAT_PANE_MIN >= LOG_COMPACT_PANEL_WIDTH;
}

/**
 * The row the command column gives up so its box shares a bottom edge with the
 * docked Message box. The pane is bordered and the column is not, so the pane
 * spends the terminal's last row on its own bottom border and the column has to
 * skip the same row to end level with it.
 */
const DOCK_ALIGNMENT_ROWS = 1;

/**
 * Rows the landing table keeps for itself before presentation may spend one.
 * Nine is the kickoff panel: two borders around the round's title, the phase
 * list, and the lines saying what the round produces.
 */
const MIN_LANDING_ROWS = 9;

/**
 * Rows the command column insets itself by so the Command and Message boxes
 * line up beneath a docked chat.
 *
 * The row is not free: it comes out of the table above it. A terminal short
 * enough that the table would lose a line keeps the row instead and lets the
 * two boxes sit one row apart. Alignment is presentation and the table is the
 * view, so a short terminal degrades in the direction the operator can see and
 * undo (resize) rather than silently clipping the content.
 *
 * `landingRows` is what the table has before this inset is taken.
 */
export function commandColumnInset(chatDocked: boolean, landingRows: number): number {
  if (!chatDocked) return 0;
  return landingRows - DOCK_ALIGNMENT_ROWS >= MIN_LANDING_ROWS ? DOCK_ALIGNMENT_ROWS : 0;
}

/**
 * Width in columns for the docked chat. It asks for the columns left over once
 * the table has enough for its claim, so a wide terminal loses no table column
 * to the chat; where there is no such surplus it still takes its readable
 * minimum, and it never takes the table below its compact set.
 */
export function chatPaneWidth(terminalWidth: number, rightPaneWidth = 0): number {
  const available = terminalWidth - rightPaneWidth;
  const surplus = available - LOG_CLAIM_PANEL_WIDTH;
  const wanted = Math.max(CHAT_PANE_MIN, Math.min(CHAT_PANE_MAX, surplus));
  return Math.min(wanted, available - LOG_COMPACT_PANEL_WIDTH);
}

/**
 * The experiment chat as a column of the landing view rather than a dialog over
 * it. Its composer is the only surface where ordinary text becomes a question;
 * slash-prefixed text delegates to the shared command path.
 */
export class ChatPaneView {
  readonly output: BoxRenderable;
  readonly #scroll: ScrollBoxRenderable;
  readonly #conversation: ConversationView;
  readonly #composer: ChatComposerView;
  #theme: Theme;
  #renderedConversation: ConversationEntry[] | null = null;

  constructor(
    renderer: CliRenderer,
    controller: SessionController,
    markdownStyle: SyntaxStyle,
    theme: Theme,
    draft: ChatDraft,
  ) {
    this.#theme = theme;
    this.output = new BoxRenderable(renderer, {
      id: 'chat-pane',
      height: '100%',
      flexShrink: 0,
      flexDirection: 'column',
      paddingLeft: 1,
      paddingRight: 1,
      border: true,
      borderStyle: paneBorderStyle(false),
      borderColor: paneBorderColor(theme, false),
      // No fill: `paneBorderStyle` is rounded, and a box may have one or the
      // other, never both (tui-conventions.md). A pane keeps the rounded frame
      // that `PANE_BORDER` argues for, so the fill is the side that goes. It
      // was only a surface tint: this box sits in its own reserved column of
      // `body`, so nothing shows through where it used to paint, and every
      // theme token is already held to `minContrast` against the canvas it now
      // falls through to rather than against the elevated surface.
      title: paneTitle(CHAT_PANE_TITLE, false),
      visible: false,
      // Clicking into the chat gives it the keys, the same thing Ctrl+W does.
      // Without this the pane took the click but the focus border stayed on the
      // table, so the operator could not tell where their keys were going.
      onMouseUp: () => controller.focusPane('chat'),
    });
    this.#scroll = new ScrollBoxRenderable(renderer, {
      id: 'chat-pane-scroll',
      width: '100%',
      flexGrow: 1,
      stickyScroll: true,
      stickyStart: 'bottom',
      viewportCulling: true,
      verticalScrollbarOptions: {showArrows: false},
      // The pointer lands on whatever is innermost, so the outer box's handler
      // never fires for a click on the conversation itself. Both surfaces ask
      // for focus, and the border then agrees with where the keys go.
      onMouseUp: () => controller.focusPane('chat'),
    });
    this.#conversation = new ConversationView(renderer, controller, markdownStyle, theme, {
      selectConversation: state => state.chatConversation,
      emptyContent: 'Ask about this run: progress, a failure, or what a hypothesis changed.',
      // Answers are agent-authored markdown; the operator's own messages stay
      // verbatim so typed ** or # is never concealed as markup. The chat keeps
      // answering after the run turns terminal, so it never switches to the
      // finalized parse that would leave a fresh answer blank until a redraw.
      markdownKinds: ['assistant'],
      markdownStreaming: true,
      onFocusRequest: () => controller.focusPane('chat'),
    });
    this.#composer = new ChatComposerView(
      renderer,
      draft,
      value => void controller.submitChat(value),
      theme,
      'chat-dock',
      () => controller.focusPane('chat'),
    );
    this.#scroll.add(this.#conversation.output);
    this.output.add(this.#scroll);
    // Absolute inside the pane rather than over the screen, so the menu rises
    // out of the composer it belongs to instead of covering the whole view.
    this.output.add(this.#composer.menu);
    this.output.add(this.#composer.output);
  }

  applyTheme(theme: Theme, markdownStyle: SyntaxStyle): void {
    this.#theme = theme;
    // Resting colour: `render` repaints from the live focus on the next frame.
    this.output.borderColor = paneBorderColor(theme, false);
    this.#conversation.applyTheme(theme, markdownStyle);
    this.#composer.applyTheme(theme);
    this.#renderedConversation = null;
  }

  /** Scrolled by Page Up/Page Down while this pane holds focus. */
  scrollBy(delta: number): void {
    this.#scroll.scrollBy(delta, 'viewport');
  }

  /** Releases the composer's spinner timer when the app tears down. */
  destroy(): void {
    this.#composer.destroy();
  }

  isComposerEmpty(): boolean {
    return this.#composer.isEmpty();
  }

  focusComposer(): void {
    this.#composer.focus();
  }

  navigateSuggestions(direction: 1 | -1): boolean {
    return this.#composer.navigateSuggestions(direction);
  }

  completeSuggestion(): boolean {
    return this.#composer.completeSuggestion();
  }

  render(state: SessionState, visible: boolean, width: number): void {
    this.output.visible = visible;
    // Ahead of the visibility gate: an answer that lands while the dock is
    // hidden still has to stop the composer's spinner.
    this.#composer.syncPending(state.chatPending, visible);
    if (!visible) {
      return;
    }
    this.output.width = width;
    const focused = chatPaneFocused(state);
    // The chat is a pane, so it wears the same focus channels as the panes
    // beside it rather than a treatment of its own. `focus.ts` reserves the
    // marker's cell in the title, which keeps the label at a fixed column
    // whether or not the chat holds the keys. The label names the active thread
    // so switching is visible at a glance, and its runtime so the operator can
    // tell which agent is answering.
    applyPaneFocus(this.output, this.#theme, chatThreadHeading(state), focused);
    this.#composer.activate(Math.max(1, width - 4), focused, state.chatPending);
    this.#composer.renderMenu(state);
    if (state.chatConversation === this.#renderedConversation) return;
    this.#renderedConversation = state.chatConversation;
    this.#conversation.render(state);
    this.#scroll.scrollTo(this.#scroll.scrollHeight);
  }
}
