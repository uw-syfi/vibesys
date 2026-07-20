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

export class ChatOverlayView {
  readonly output: BoxRenderable;
  readonly #transcript: ScrollBoxRenderable;
  readonly #conversation: ConversationView;
  readonly #input: InputRenderable;

  constructor(
    renderer: CliRenderer,
    private readonly controller: SessionController,
    markdownStyle: SyntaxStyle,
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
      borderColor: '#a78bfa',
      backgroundColor: '#020617',
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
    this.#conversation = new ConversationView(renderer, controller, markdownStyle, {
      selectConversation: state => state.chatConversation,
      emptyContent: 'Ask a question about the current experiment, its progress, or a failure.',
      renderMarkdown: false,
    });
    const inputBox = new BoxRenderable(renderer, {
      id: 'chat-input-box',
      height: 3,
      width: '100%',
      border: true,
      borderStyle: 'rounded',
      borderColor: '#8b5cf6',
      title: ' Message ',
      paddingLeft: 1,
      paddingRight: 1,
    });
    this.#input = new InputRenderable(renderer, {
      id: 'chat-input',
      width: '100%',
      placeholder: 'Ask about this experiment',
      textColor: '#f8fafc',
      focusedTextColor: '#f8fafc',
    });
    this.#input.on(InputRenderableEvents.ENTER, this.#submit);
    inputBox.add(this.#input);
    this.#transcript.add(this.#conversation.output);
    this.output.add(this.#transcript);
    this.output.add(inputBox);
    this.output.add(
      new TextRenderable(renderer, {
        content: 'Enter to send · Esc to close',
        fg: '#64748b',
        height: 1,
        width: '100%',
      }),
    );
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
    void this.controller.sendChat(value);
  };
}
