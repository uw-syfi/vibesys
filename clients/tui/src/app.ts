import {
  BoxRenderable,
  InputRenderable,
  ScrollBoxRenderable,
  TextRenderable,
  type CliRenderer,
  type KeyEvent,
} from '@opentui/core';
import type {SessionController} from './session-controller.js';
import {statusText, type SessionState} from './session-model.js';

export interface OpenTuiApp {
  destroy(): void;
}

export function createOpenTuiApp(
  renderer: CliRenderer,
  controller: SessionController,
): OpenTuiApp {
  let exitScheduled = false;
  const root = new BoxRenderable(renderer, {
    id: 'app', width: '100%', height: '100%', flexDirection: 'column',
  });
  const header = new TextRenderable(renderer, {
    id: 'header', height: 1, fg: '#22d3ee', content: 'VibeServe · connecting',
  });
  const output = new TextRenderable(renderer, {id: 'output', width: '100%', content: ''});
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
    content: '↑/↓ · PgUp/PgDn · Home/End · Ctrl+L: live · /help',
  });
  const inputBox = new BoxRenderable(renderer, {
    id: 'input-box',
    height: 3,
    width: '100%',
    border: true,
    borderStyle: 'rounded',
    borderColor: '#22c55e',
    title: ' Ask or command ',
    paddingLeft: 1,
    paddingRight: 1,
  });
  const input = new InputRenderable(renderer, {
    id: 'input',
    width: '100%',
    placeholder: 'Type a question or /help',
    textColor: '#f8fafc',
    focusedTextColor: '#f8fafc',
    onSubmit: () => {
      const value = input.value;
      input.value = '';
      void controller.submit(value);
    },
  });

  viewport.add(output);
  inputBox.add(input);
  root.add(header);
  root.add(viewport);
  root.add(help);
  root.add(inputBox);
  renderer.root.add(root);
  input.focus();

  function render(state: SessionState): void {
    header.content = `VibeServe · ${statusText(state)}`;
    output.content = state.view === 'live' ? state.liveContent : state.detailContent;
    if (!exitScheduled && state.terminal) {
      exitScheduled = true;
      setTimeout(() => renderer.destroy(), 100);
    }
  }

  function onKey(key: KeyEvent): void {
    if (key.ctrl && key.name === 'l') {
      controller.live();
      viewport.scrollTo(viewport.scrollHeight);
      key.preventDefault();
      return;
    }
    if (key.name === 'pageup') viewport.scrollBy(-1, 'viewport');
    else if (key.name === 'pagedown') viewport.scrollBy(1, 'viewport');
    else if (key.ctrl && key.name === 'up') viewport.scrollBy(-1);
    else if (key.ctrl && key.name === 'down') viewport.scrollBy(1);
    else if (key.name === 'home') viewport.scrollTo(0);
    else if (key.name === 'end') viewport.scrollTo(viewport.scrollHeight);
    else return;
    key.preventDefault();
  }

  renderer.keyInput.on('keypress', onKey);
  const unsubscribe = controller.subscribe(render);
  return {
    destroy(): void {
      unsubscribe();
      renderer.keyInput.off('keypress', onKey);
      root.destroyRecursively();
    },
  };
}
