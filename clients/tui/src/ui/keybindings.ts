import type {CliRenderer, KeyEvent, ScrollBoxRenderable} from '@opentui/core';
import type {SessionController} from '../session-controller.js';
import {chatPaneFocused, chatPaneVisible, experimentLogVisible} from '../session-model.js';

export interface KeybindingActions {
  completeInput(): boolean;
  inputIsEmpty(): boolean;
  closeChat(): void;
  toggleLatestPrompt(): void;
  selectNextAgent(): void;
  selectPreviousAgent(): void;
  selectNextRound(): void;
  selectPreviousRound(): void;
  toggleTodos(): void;
  scrollRightPane(delta: number): void;
  scrollChatPane(delta: number): void;
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
    // Pane switching works from anywhere a second column is on screen, which
    // on the landing view includes the docked chat, so the operator never has
    // to leave the table to scroll back through an answer.
    if (
      key.ctrl &&
      key.name === 'w' &&
      (controller.state.layout.right !== null || chatPaneVisible(controller.state))
    ) {
      controller.cyclePaneFocus();
      key.preventDefault();
      return;
    }
    // The focused pane takes the scroll keys. Everything else the chat or the
    // transcript would normally handle is left alone.
    if (
      controller.state.layout.focus === 'right' &&
      controller.state.layout.right !== null &&
      (key.name === 'pageup' || key.name === 'pagedown' || key.name === 'escape')
    ) {
      if (key.name === 'escape') controller.closePane();
      else actions.scrollRightPane(key.name === 'pageup' ? -1 : 1);
      key.preventDefault();
      return;
    }
    // The docked chat takes the scroll keys while it holds focus, so Page Up
    // reads the conversation back instead of moving the table's selection.
    // Escape hands the keys back to the table rather than closing anything:
    // the pane is part of the view, not a dialog over it.
    if (
      chatPaneFocused(controller.state) &&
      (key.name === 'pageup' || key.name === 'pagedown' || key.name === 'escape')
    ) {
      if (key.name === 'escape') controller.focusPane('left');
      else actions.scrollChatPane(key.name === 'pageup' ? -1 : 1);
      key.preventDefault();
      return;
    }
    // The theme picker owns its navigation keys wherever it was opened from,
    // so arrows never reach the view behind it. A typed command still belongs
    // to the input, which is why Enter is claimed only on an empty line.
    if (controller.state.themePicker !== null) {
      if (key.name === 'up') controller.moveThemeSelection(-1);
      else if (key.name === 'down') controller.moveThemeSelection(1);
      else if (key.name === 'pageup') controller.moveThemeSelection(-10);
      else if (key.name === 'pagedown') controller.moveThemeSelection(10);
      else if (key.name === 'escape') controller.closeThemePicker();
      else if (key.name === 'return' || key.name === 'enter') {
        if (!actions.inputIsEmpty()) return;
        controller.applySelectedTheme();
      } else return;
      key.preventDefault();
      return;
    }
    if (controller.state.chatOpen) {
      if (key.name === 'escape') {
        actions.closeChat();
        key.preventDefault();
      }
      return;
    }
    // An overlay floats above whatever pane is behind it, so it takes Escape
    // first whether that pane is the transcript or the experiment log.
    if (key.name === 'escape' && controller.state.overlay !== null) {
      controller.live();
      viewport.scrollTo(viewport.scrollHeight);
      key.preventDefault();
      return;
    }
    // The experiment log owns navigation while the table is on screen: arrows
    // move the selection instead of the input cursor, and Enter opens the
    // rounds behind the selected hypothesis. The input keeps priority over the
    // table, so a typed command runs on its own Enter and its result opens over
    // the log rather than the log swallowing the keystroke.
    if (experimentLogVisible(controller.state)) {
      // Escape has nowhere to go from the root view: the log is the root.
      if (key.name === 'up') controller.moveExperimentSelection(-1);
      else if (key.name === 'down') controller.moveExperimentSelection(1);
      else if (key.name === 'pageup') controller.moveExperimentSelection(-10);
      else if (key.name === 'pagedown') controller.moveExperimentSelection(10);
      else if (key.name === 'return' || key.name === 'enter') {
        // A typed command belongs to the input; let its own handler run it so
        // one Enter is enough. An overlay is in front of the table, so Enter
        // behind it must not move the operator somewhere they cannot see.
        if (!actions.inputIsEmpty()) return;
        if (controller.state.overlay === null) controller.enterExperimentDrilldown();
      } else return;
      key.preventDefault();
      return;
    }
    // Inside a hypothesis, Escape steps back out to the table rather than
    // dropping the operator into unfiltered live output.
    if (key.name === 'escape' && controller.state.hypothesisScope !== null) {
      controller.leaveExperimentDrilldown();
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
