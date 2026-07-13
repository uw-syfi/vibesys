import {BoxRenderable, type CliRenderer, ScrollBoxRenderable, TextRenderable} from '@opentui/core';
import type {SessionController} from '../session-controller.js';
import {type SessionState, statusText} from '../session-model.js';
import {ConversationView} from './conversation.js';
import {createInputPanel} from './input.js';
import {bindKeybindings} from './keybindings.js';
import {createMarkdownStyle} from './styles.js';

export interface OpenTuiApp {
  destroy(): void;
}

export function createOpenTuiApp(renderer: CliRenderer, controller: SessionController): OpenTuiApp {
  const root = new BoxRenderable(renderer, {
    id: 'app',
    width: '100%',
    height: '100%',
    flexDirection: 'column',
  });
  const header = new TextRenderable(renderer, {
    id: 'header',
    height: 1,
    fg: '#22d3ee',
    content: 'VibeServe · connecting',
  });
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
    content: '↑/↓ · PgUp/PgDn · Ctrl+P: prompt · Ctrl+L: live · /help',
  });
  const markdownStyle = createMarkdownStyle();
  const conversation = new ConversationView(renderer, controller, markdownStyle);
  const input = createInputPanel(renderer, value => void controller.submit(value));

  viewport.add(conversation.output);
  root.add(header);
  root.add(viewport);
  root.add(help);
  root.add(input.box);
  renderer.root.add(root);
  input.focus();

  const render = (state: SessionState): void => {
    const returnHint = state.view === 'live' ? '' : ' · Esc: back to live';
    header.content = `VibeServe · ${statusText(state)}${returnHint}`;
    conversation.render(state);
  };
  const unbindKeys = bindKeybindings(renderer, controller, viewport, {
    toggleLatestPrompt: () => conversation.toggleLatestPrompt(),
  });
  const unsubscribe = controller.subscribe(render);

  return {
    destroy(): void {
      unsubscribe();
      unbindKeys();
      input.destroy();
      root.destroyRecursively();
      markdownStyle.destroy();
    },
  };
}
