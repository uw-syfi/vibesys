import type {CliRenderer, KeyEvent, ScrollBoxRenderable} from '@opentui/core';
import type {SessionController} from '../session-controller.js';

export interface KeybindingActions {
  toggleLatestPrompt(): void;
}

export function bindKeybindings(
  renderer: CliRenderer,
  controller: SessionController,
  viewport: ScrollBoxRenderable,
  actions: KeybindingActions,
): () => void {
  const onKey = (key: KeyEvent): void => {
    if (key.ctrl && key.name === 'c') {
      key.preventDefault();
      renderer.destroy();
      return;
    }
    if (key.name === 'escape' && controller.state.view !== 'live') {
      controller.live();
      viewport.scrollTo(viewport.scrollHeight);
      key.preventDefault();
      return;
    }
    if (key.ctrl && key.name === 'p') {
      actions.toggleLatestPrompt();
      key.preventDefault();
      return;
    }
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
  };

  renderer.keyInput.on('keypress', onKey);
  return () => renderer.keyInput.off('keypress', onKey);
}
