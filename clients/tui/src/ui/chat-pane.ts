import {
  BoxRenderable,
  type CliRenderer,
  ScrollBoxRenderable,
  type SyntaxStyle,
} from '@opentui/core';
import type {SessionController} from '../session-controller.js';
import {chatPaneFocused, type SessionState} from '../session-model.js';
import {ConversationView} from './conversation.js';
import {LOG_CLAIM_PANEL_WIDTH, LOG_COMPACT_PANEL_WIDTH} from './experiment-log.js';
import type {Theme} from './theme.js';

/**
 * Columns the chat needs before a question and its answer read as prose rather
 * than as a column of fragments.
 */
const CHAT_PANE_MIN = 25;
const CHAT_PANE_MAX = 52;

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
 * it. It has no input of its own: the client already has one, and plain text
 * typed there is a question for the agent, so a second box would only split the
 * operator's attention.
 */
export class ChatPaneView {
  readonly output: BoxRenderable;
  readonly #scroll: ScrollBoxRenderable;
  readonly #conversation: ConversationView;
  #theme: Theme;
  #renderedState: SessionState | null = null;

  constructor(
    renderer: CliRenderer,
    controller: SessionController,
    markdownStyle: SyntaxStyle,
    theme: Theme,
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
      borderStyle: 'rounded',
      borderColor: theme.border,
      title: ' Experiment chat ',
      visible: false,
    });
    this.#scroll = new ScrollBoxRenderable(renderer, {
      id: 'chat-pane-scroll',
      width: '100%',
      flexGrow: 1,
      stickyScroll: true,
      stickyStart: 'bottom',
      viewportCulling: true,
      verticalScrollbarOptions: {showArrows: false},
    });
    this.#conversation = new ConversationView(renderer, controller, markdownStyle, theme, {
      selectConversation: state => state.chatConversation,
      emptyContent: 'Ask about this run: progress, a failure, or what a hypothesis changed.',
      renderMarkdown: false,
    });
    this.#scroll.add(this.#conversation.output);
    this.output.add(this.#scroll);
  }

  applyTheme(theme: Theme, markdownStyle: SyntaxStyle): void {
    this.#theme = theme;
    this.output.borderColor = theme.border;
    this.#conversation.applyTheme(theme, markdownStyle);
    this.#renderedState = null;
  }

  /** Scrolled by Page Up/Page Down while this pane holds focus. */
  scrollBy(delta: number): void {
    this.#scroll.scrollBy(delta, 'viewport');
  }

  render(state: SessionState, visible: boolean, width: number): void {
    this.output.visible = visible;
    if (!visible) {
      this.#renderedState = null;
      return;
    }
    this.output.width = width;
    const focused = chatPaneFocused(state);
    this.output.borderColor = focused ? this.#theme.borderFocus : this.#theme.border;
    // The column can be as narrow as its minimum, where a spelled-out "focused"
    // costs the title itself: a box with no title reads as nothing at all. The
    // marker is the one the table already uses for the row that has the keys.
    this.output.title = focused ? ' ▸ Experiment chat ' : ' Experiment chat ';
    if (state === this.#renderedState) return;
    this.#renderedState = state;
    this.#conversation.render(state);
    this.#scroll.scrollTo(this.#scroll.scrollHeight);
  }
}
