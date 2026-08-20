import {
  BoxRenderable,
  type CliRenderer,
  InputRenderable,
  InputRenderableEvents,
  ScrollBoxRenderable,
  type SyntaxStyle,
  TextRenderable,
} from '@opentui/core';
import type {SessionController} from '../session-controller.js';
import type {SessionState} from '../session-model.js';
import {ConversationView} from './conversation.js';
import type {Theme} from './theme.js';

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
  readonly #input: InputRenderable;
  readonly #inputBox: BoxRenderable;
  readonly #hint: TextRenderable;
  #bounds: PaneBounds | null = null;

  constructor(
    renderer: CliRenderer,
    private readonly controller: SessionController,
    markdownStyle: SyntaxStyle,
    theme: Theme,
  ) {
    this.output = new BoxRenderable(renderer, {
      id: 'chat-overlay',
      width: '80%',
      height: '76%',
      position: 'absolute',
      left: '10%',
      top: '10%',
      flexDirection: 'column',
      paddingLeft: 1,
      paddingRight: 1,
      border: true,
      borderStyle: 'rounded',
      borderColor: theme.conversation.analysis.label,
      backgroundColor: theme.elevatedSurface,
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
      renderMarkdown: false,
    });
    this.#inputBox = new BoxRenderable(renderer, {
      id: 'chat-input-box',
      height: 3,
      width: '100%',
      border: true,
      borderStyle: 'rounded',
      borderColor: theme.conversation.analysis.label,
      title: ' Message ',
      paddingLeft: 1,
      paddingRight: 1,
    });
    this.#input = new InputRenderable(renderer, {
      id: 'chat-input',
      width: '100%',
      placeholder: 'Ask about this experiment',
      textColor: theme.textStrong,
      focusedTextColor: theme.textStrong,
    });
    this.#input.on(InputRenderableEvents.ENTER, this.#submit);
    this.#inputBox.add(this.#input);
    this.#transcript.add(this.#conversation.output);
    this.output.add(this.#transcript);
    this.output.add(this.#inputBox);
    this.#hint = new TextRenderable(renderer, {
      content: 'Enter to send · /perf and other commands work here · Esc to close',
      fg: theme.textSubtle,
      height: 1,
      width: '100%',
    });
    this.output.add(this.#hint);
  }

  /**
   * Confines the chat to the left pane while a visualization is on screen, so
   * the operator can ask about what they are looking at without covering it.
   * ``null`` restores the centred modal geometry.
   */
  setPaneBounds(bounds: PaneBounds | null): void {
    if (samePaneBounds(this.#bounds, bounds)) return;
    this.#bounds = bounds;
    if (bounds === null) {
      this.output.left = '10%';
      this.output.width = '80%';
      this.output.top = '10%';
      this.output.height = '76%';
      return;
    }
    this.output.left = bounds.left;
    this.output.width = Math.max(1, bounds.width);
    this.output.top = bounds.top;
    this.output.height = Math.max(3, bounds.height);
  }

  applyTheme(theme: Theme, markdownStyle: SyntaxStyle): void {
    this.output.borderColor = theme.conversation.analysis.label;
    this.output.backgroundColor = theme.elevatedSurface;
    this.#inputBox.borderColor = theme.conversation.analysis.label;
    this.#input.textColor = theme.textStrong;
    this.#input.focusedTextColor = theme.textStrong;
    this.#hint.fg = theme.textSubtle;
    this.#conversation.applyTheme(theme, markdownStyle);
  }

  render(state: SessionState): void {
    this.output.visible = state.chatOpen;
    if (!state.chatOpen) return;
    this.#conversation.render(state);
    this.#transcript.scrollTo(this.#transcript.scrollHeight);
  }

  focus(): void {
    this.#input.focus();
  }

  destroy(): void {
    this.#input.off(InputRenderableEvents.ENTER, this.#submit);
  }

  readonly #submit = (value: string): void => {
    if (!value.trim()) return;
    this.#input.value = '';
    void this.controller.submitChat(value);
  };
}
