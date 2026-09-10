import {
  BoxRenderable,
  type CliRenderer,
  ScrollBoxRenderable,
  type SyntaxStyle,
} from '@opentui/core';
import type {SessionController} from '../session-controller.js';
import {chatThreadHeading, type SessionState} from '../session-model.js';
import {ChatComposerView, type ChatDraft} from './chat-composer.js';
import {ConversationView} from './conversation.js';
import type {Theme} from './theme.js';

/**
 * The centred modal's four edges as fractions of the terminal: 80% of the
 * columns, centred, and 76% of the rows starting a tenth of the way down.
 */
const MODAL_LEFT = 0.1;
const MODAL_RIGHT = 0.9;
const MODAL_TOP = 0.1;
const MODAL_BOTTOM = 0.86;

/** Columns the modal's border and padding take from every child. */
const MODAL_CHROME = 4;
/** A border, one row to type on, and a border. */
const MIN_MODAL_HEIGHT = 3;

/** Screen rectangle the chat occupies when it shares the row with a pane. */
export interface PaneBounds {
  left: number;
  width: number;
  top: number;
  height: number;
}

function samePaneBounds(left: PaneBounds | null, right: PaneBounds | null): boolean {
  if (left === null || right === null) return left === right;
  return (
    left.left === right.left &&
    left.width === right.width &&
    left.top === right.top &&
    left.height === right.height
  );
}

export class ChatOverlayView {
  readonly output: BoxRenderable;
  readonly #transcript: ScrollBoxRenderable;
  readonly #conversation: ConversationView;
  readonly #composer: ChatComposerView;
  #bounds: PaneBounds | null = null;
  /**
   * Columns inside the modal's chrome, as the geometry below just set them.
   * `output.width` answers with the last width the layout computed rather than
   * the one assigned, so after a resize it still describes the previous
   * rectangle; the composer would wrap its draft against a stale width for as
   * long as no state change happened to ask again.
   */
  #contentWidth = 1;

  constructor(
    private readonly renderer: CliRenderer,
    controller: SessionController,
    markdownStyle: SyntaxStyle,
    theme: Theme,
    draft: ChatDraft,
  ) {
    this.output = new BoxRenderable(renderer, {
      id: 'chat-overlay',
      position: 'absolute',
      flexDirection: 'column',
      paddingLeft: 1,
      paddingRight: 1,
      border: true,
      // Square with an outer fill, the overlay exception (tui-conventions.md):
      // the fill is what makes this modal opaque over the run behind it, ring
      // included, and a fill that reaches the ring needs a square corner.
      borderStyle: 'single',
      borderColor: theme.conversation.analysis.label,
      backgroundColor: theme.canvas,
      title: ' Experiment chat ',
      zIndex: 20,
      visible: false,
    });
    this.#transcript = new ScrollBoxRenderable(renderer, {
      id: 'chat-transcript',
      width: '100%',
      flexGrow: 1,
      stickyScroll: true,
      stickyStart: 'bottom',
      viewportCulling: true,
      verticalScrollbarOptions: {showArrows: true},
    });
    this.#conversation = new ConversationView(renderer, controller, markdownStyle, theme, {
      selectConversation: state => state.chatConversation,
      emptyContent: 'Ask a question about the current experiment, its progress, or a failure.',
      // Answers are agent-authored markdown; the operator's own messages stay
      // verbatim so typed ** or # is never concealed as markup. The chat keeps
      // answering after the run turns terminal, so it never switches to the
      // finalized parse that would leave a fresh answer blank until a redraw.
      markdownKinds: ['assistant'],
      markdownStreaming: true,
    });
    this.#composer = new ChatComposerView(
      renderer,
      draft,
      value => void controller.submitChat(value),
      theme,
      'chat-modal',
    );
    this.#applyModalGeometry();
    this.#transcript.add(this.#conversation.output);
    this.output.add(this.#transcript);
    // Anchored to the composer, matching the docked pane: the same commands
    // read the same way whichever presentation the chat is in.
    this.output.add(this.#composer.menu);
    this.output.add(this.#composer.output);
  }

  /**
   * Confines the chat to the left pane while a visualization is on screen, so
   * the operator can ask about what they are looking at without covering it.
   * ``null`` restores the centred modal geometry.
   */
  setPaneBounds(bounds: PaneBounds | null): void {
    // The modal measures itself against the terminal, so it is recomputed even
    // when the bounds did not change: a resize changes the answer.
    if (bounds === null) {
      this.#bounds = null;
      this.#applyModalGeometry();
      return;
    }
    if (samePaneBounds(this.#bounds, bounds)) return;
    this.#bounds = bounds;
    this.#applyGeometry(
      bounds.left,
      bounds.top,
      Math.max(1, bounds.width),
      Math.max(MIN_MODAL_HEIGHT, bounds.height),
    );
  }

  /**
   * Places the centred modal on whole cells. Left as percentages, its edges
   * land mid-row at most terminal heights, and the layout rounds a child's
   * offset from its parent separately from that child's size: at a fractional
   * offset the two disagree and the transcript either runs a row into the
   * composer or leaves a row of the modal's floor blank.
   *
   * Rounding the four edges reproduces the rectangle the percentages already
   * produced, so the modal is the same size and in the same place as before;
   * only the offsets inside it become whole. That equivalence is a property of
   * how the layout rounds, which is per edge and not per size: it rounds a
   * node's absolute left and its absolute right, then takes the width from the
   * difference. So the width was already `round(0.9W) - round(0.1W)` and never
   * `round(0.8W)`, which disagrees with it at 104 of the 261 widths from 40 to
   * 300. `pins the modal rectangle across a width sweep` holds this.
   */
  #applyModalGeometry(): void {
    const {terminalWidth, terminalHeight} = this.renderer;
    const left = Math.round(terminalWidth * MODAL_LEFT);
    const top = Math.round(terminalHeight * MODAL_TOP);
    this.#applyGeometry(
      left,
      top,
      Math.max(1, Math.round(terminalWidth * MODAL_RIGHT) - left),
      Math.max(MIN_MODAL_HEIGHT, Math.round(terminalHeight * MODAL_BOTTOM) - top),
    );
  }

  /**
   * The one place the modal's rectangle is written, so the width its children
   * are told is the width just assigned rather than a read back out of the
   * layout.
   */
  #applyGeometry(left: number, top: number, width: number, height: number): void {
    this.output.left = left;
    this.output.top = top;
    this.output.width = width;
    this.output.height = height;
    this.#contentWidth = Math.max(1, width - MODAL_CHROME);
  }

  applyTheme(theme: Theme, markdownStyle: SyntaxStyle): void {
    this.output.borderColor = theme.conversation.analysis.label;
    this.output.backgroundColor = theme.canvas;
    this.#composer.applyTheme(theme);
    this.#conversation.applyTheme(theme, markdownStyle);
  }

  render(state: SessionState): void {
    this.output.visible = state.chatOpen;
    // Ahead of the visibility gate: an answer that lands while the modal is
    // closed still has to stop the composer's spinner.
    this.#composer.syncPending(state.chatPending, state.chatOpen);
    if (!state.chatOpen) return;
    this.output.title = ` ${chatThreadHeading(state)} `;
    this.#composer.activate(this.#contentWidth, true, state.chatPending);
    this.#composer.renderMenu(state);
    this.#conversation.render(state);
    this.#transcript.scrollTo(this.#transcript.scrollHeight);
  }

  focus(): void {
    this.#composer.focus();
  }

  isComposerEmpty(): boolean {
    return this.#composer.isEmpty();
  }

  navigateSuggestions(direction: 1 | -1): boolean {
    return this.#composer.navigateSuggestions(direction);
  }

  completeSuggestion(): boolean {
    return this.#composer.completeSuggestion();
  }

  /** Releases the composer's spinner timer when the app tears down. */
  destroy(): void {
    this.#composer.destroy();
  }
}
