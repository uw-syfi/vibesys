import type {CliRenderer, KeyEvent, ScrollBoxRenderable} from '@opentui/core';
import type {SessionController} from '../session-controller.js';

export interface KeybindingActions {
  completeInput(): boolean;
  closeChat(): void;
  toggleLatestPrompt(): void;
  selectNextAgent(): void;
  selectPreviousAgent(): void;
  selectNextRound(): void;
  selectPreviousRound(): void;
  toggleTodos(): void;
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
    if (controller.state.chatOpen) {
      if (key.name === 'escape') {
        actions.closeChat();
        key.preventDefault();
      }
      return;
    }
    if (key.name === 'escape' && controller.state.overlay !== null) {
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
    if (key.ctrl && key.name === 't') {
      actions.toggleTodos();
      key.preventDefault();
      return;
    }
    if (key.ctrl && key.name === 'l') {
      controller.live();
      viewport.scrollTo(viewport.scrollHeight);
      key.preventDefault();
      return;
    }
    if (key.name === 'tab' && !key.shift && actions.completeInput()) {
      key.preventDefault();
      return;
    }
    if (key.name === 'tab') {
      if (key.shift) actions.selectPreviousAgent();
      else actions.selectNextAgent();
      viewport.scrollTo(viewport.scrollHeight);
      key.preventDefault();
      return;
    }
    if (key.name === ']') {
      actions.selectNextRound();
      viewport.scrollTo(viewport.scrollHeight);
      key.preventDefault();
      return;
    }
    if (key.name === '[') {
      actions.selectPreviousRound();
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
