import type {CliRenderer, KeyEvent, ScrollBoxRenderable} from '@opentui/core';
import type {SessionController} from '../session-controller.js';
import {
  chatPaneFocused,
  chatPaneVisible,
  experimentLogVisible,
  type RoundFocus,
  todoListFocused,
  visibleRoundNumber,
} from '../session-model.js';
import {agentGraphEnabled} from './agent-map.js';
import type {ClipboardCopyResult, SelectionClipboard} from './clipboard.js';
import {roundRailVisible} from './round-rail.js';

export interface KeybindingActions {
  completeInput(): boolean;
  navigateSuggestions(direction: 1 | -1): boolean;
  /** Navigates the chat composer's own typed-command suggestions, docked or modal. */
  navigateChatSuggestions(direction: 1 | -1): boolean;
  /** Tab-completes the chat composer's highlighted typed-command suggestion. */
  completeChatInput(): boolean;
  inputIsEmpty(): boolean;
  closeChat(): void;
  toggleLatestPrompt(): void;
  toggleSelectedTool(): boolean;
  /** Brings the entry the cursor moved to into view. */
  revealSelectedEntry(): void;
  /** Materializes the next block of conversation history above the window. */
  revealOlderEntries(): void;
  selectNextAgent(): void;
  selectPreviousAgent(): void;
  selectNextRound(): void;
  selectPreviousRound(): void;
  toggleTodos(): void;
  scrollRightPane(delta: number): void;
  scrollChatPane(delta: number): void;
  scrollExperimentDetail(delta: number): void;
  scrollErrorBanner(delta: number): void;
  scrollOverlay(delta: number): void;
  clearTransientStatus(): void;
  showClipboardStatus(result: Exclude<ClipboardCopyResult, 'no-selection'>): void;
}

export function bindKeybindings(
  renderer: CliRenderer,
  controller: SessionController,
  viewport: ScrollBoxRenderable,
  clipboard: SelectionClipboard,
  actions: KeybindingActions,
): () => void {
  const onKey = (key: KeyEvent): void => {
    if (key.ctrl && !key.shift && key.name === 'c') {
      key.preventDefault();
      const result = clipboard.copySelection();
      if (result === 'no-selection') renderer.destroy();
      else actions.showClipboardStatus(result);
      return;
    }
    actions.clearTransientStatus();
    if (
      key.name === 'f4' &&
      controller.state.chatOpen === false &&
      controller.state.overlay === null &&
      controller.state.themePicker === null &&
      controller.state.chatMenu === null
    ) {
      controller.togglePaneZoom();
      key.preventDefault();
      return;
    }
    if (
      controller.state.errorBanner !== null &&
      key.ctrl &&
      (key.name === 'pageup' || key.name === 'pagedown')
    ) {
      actions.scrollErrorBanner(key.name === 'pageup' ? -1 : 1);
      key.preventDefault();
      return;
    }
    if (controller.state.errorBanner !== null && key.name === 'escape') {
      controller.dismissErrorBanner();
      key.preventDefault();
      return;
    }
    if (
      key.ctrl &&
      key.name === 'w' &&
      (controller.state.layout.right !== null || chatPaneVisible(controller.state))
    ) {
      controller.cyclePaneFocus();
      key.preventDefault();
      return;
    }
    // The composer's inline menu owns the keys while it is open. On a custom
    // model entry that includes ordinary typing, which must never leak into
    // the composer underneath; anywhere else typing dismisses the menu and
    // goes back to writing a question, so the keystroke is left alone.
    const chatMenu = controller.state.chatMenu;
    if (chatMenu !== null) {
      const onCustomEntry = chatMenu.rows[chatMenu.selected]?.kind === 'custom';
      if (key.name === 'up') controller.moveChatMenuSelection(-1);
      else if (key.name === 'down') controller.moveChatMenuSelection(1);
      else if (key.name === 'escape') controller.closeChatMenu();
      else if (key.name === 'return' || key.name === 'enter' || key.name === 'kpenter') {
        void controller.confirmChatMenu();
      } else if (onCustomEntry && key.name === 'backspace') {
        controller.backspaceChatMenuCustomModel();
      } else if (onCustomEntry && isPrintable(key)) {
        controller.typeChatMenuCustomModel(key.sequence);
      } else {
        if (isPrintable(key)) controller.closeChatMenu();
        return;
      }
      key.preventDefault();
      return;
    }
    // The focused pane takes the scroll keys. Escape belongs to the modal/pane
    // ladder below, so a right pane's own Escape waits until any modal chat in
    // front of it has already closed.
    if (
      controller.state.layout.focus === 'right' &&
      controller.state.layout.right !== null &&
      (key.name === 'pageup' || key.name === 'pagedown')
    ) {
      actions.scrollRightPane(key.name === 'pageup' ? -1 : 1);
      key.preventDefault();
      return;
    }
    // Modal state is authoritative: the theme picker, command overlay, and
    // modal chat must contain input before the focused docked chat runs. Otherwise
    // a modal opened while the docked chat has focus would leak printable keys
    // into the hidden composer, let Up/Down drive chat suggestions, and route
    // Escape to the left pane instead of closing the modal.
    if (controller.state.themePicker !== null) {
      if (key.name === 'up') controller.moveThemeSelection(-1);
      else if (key.name === 'down') controller.moveThemeSelection(1);
      else if (key.name === 'pageup') controller.moveThemeSelection(-10);
      else if (key.name === 'pagedown') controller.moveThemeSelection(10);
      else if (key.name === 'escape') controller.closeThemePicker();
      else if (key.name === 'return' || key.name === 'enter') controller.applySelectedTheme();
      // The picker is modal: keys it does not use are swallowed here so they
      // cannot move panes or type into the still-focused input behind it.
      key.preventDefault();
      return;
    }
    if (controller.state.overlay !== null) {
      if (key.name === 'escape') {
        controller.live();
        viewport.scrollTo(viewport.scrollHeight);
      } else if (key.name === 'pageup' || key.name === 'pagedown') {
        // Content taller than the box scrolls here rather than falling through
        // to the transcript behind it.
        actions.scrollOverlay(key.name === 'pageup' ? -1 : 1);
      }
      // The overlay is modal: everything it does not handle is swallowed so
      // keys cannot reach the panes or the hidden command input behind it.
      key.preventDefault();
      return;
    }
    if (controller.state.chatOpen) {
      if (key.name === 'escape') {
        // The modal chat is the innermost layer: Escape closes only it,
        // regardless of whatever pane sits behind it. A pane open behind the
        // chat unwinds on its own Escape, once the chat is gone.
        actions.closeChat();
        key.preventDefault();
        return;
      }
      // Same suggestion-menu priority as the docked chat below.
      if (key.name === 'up' || key.name === 'down') {
        if (actions.navigateChatSuggestions(key.name === 'up' ? -1 : 1)) key.preventDefault();
        return;
      }
      if (key.name === 'tab' && !key.shift) {
        if (actions.completeChatInput()) key.preventDefault();
        return;
      }
      return;
    }
    // With the chat closed (or never open), Escape's next layer is the
    // visualization pane: one press folds it away on its own, leaving
    // whatever is behind it (a hypothesis trajectory, the round view) intact.
    if (
      key.name === 'escape' &&
      controller.state.layout.focus === 'right' &&
      controller.state.layout.right !== null
    ) {
      controller.closePane();
      key.preventDefault();
      return;
    }
    if (chatPaneFocused(controller.state)) {
      if (key.name === 'pageup' || key.name === 'pagedown' || key.name === 'escape') {
        if (key.name === 'escape') controller.focusPane('left');
        else actions.scrollChatPane(key.name === 'pageup' ? -1 : 1);
        key.preventDefault();
        return;
      }
      // The typed-command suggestions take Up/Down/Tab only while they are
      // showing; otherwise the keys fall through to the editor underneath
      // (multiline cursor movement, and Tab's ordinary no-op).
      if (key.name === 'up' || key.name === 'down') {
        if (actions.navigateChatSuggestions(key.name === 'up' ? -1 : 1)) key.preventDefault();
        return;
      }
      if (key.name === 'tab' && !key.shift) {
        if (actions.completeChatInput()) key.preventDefault();
        return;
      }
      return;
    }
    // The experiment surface owns navigation while it is on screen. The index
    // opens a hypothesis summary; that summary selects and opens one round.
    // The input keeps priority over Enter so a typed command is never lost.
    if (experimentLogVisible(controller.state)) {
      const detailOpen = controller.state.hypothesisDetail !== null;
      if (key.name === 'escape' && detailOpen) controller.leaveHypothesisDetail();
      else if (key.name === 'up') {
        if (detailOpen) controller.moveHypothesisRoundSelection(-1);
        else if (!actions.navigateSuggestions(-1)) controller.moveExperimentSelection(-1);
      } else if (key.name === 'down') {
        if (detailOpen) controller.moveHypothesisRoundSelection(1);
        else if (!actions.navigateSuggestions(1)) controller.moveExperimentSelection(1);
      } else if (key.name === 'pageup') {
        if (detailOpen) actions.scrollExperimentDetail(-1);
        else controller.moveExperimentSelection(-10);
      } else if (key.name === 'pagedown') {
        if (detailOpen) actions.scrollExperimentDetail(1);
        else controller.moveExperimentSelection(10);
      } else if (key.name === 'tab' && !key.shift) {
        // The table has no agent strip to cycle through, so Tab belongs to the
        // suggestion it would otherwise complete, or nothing at all.
        if (!actions.completeInput()) return;
      } else if (key.name === 'return' || key.name === 'enter') {
        // A typed command belongs to the input; let its own handler run it so
        // one Enter is enough. An overlay is in front of the table, so Enter
        // behind it must not move the operator somewhere they cannot see.
        if (!actions.inputIsEmpty()) return;
        if (controller.state.overlay === null) controller.enterExperimentDrilldown();
      } else return;
      key.preventDefault();
      return;
    }
    if (key.name === 'escape' && controller.state.hypothesisScope !== null) {
      if (controller.state.selectedEntryId !== null) controller.clearEntrySelection();
      else if (controller.state.selectedAgentKind !== null) controller.clearAgentSelection();
      else controller.leaveExperimentDrilldown();
      key.preventDefault();
      return;
    }
    if ((key.ctrl && key.name === 'p') || key.name === 'f3') {
      actions.toggleLatestPrompt();
      key.preventDefault();
      return;
    }
    if ((key.ctrl && key.name === 't') || key.name === 'f2') {
      actions.toggleTodos();
      key.preventDefault();
      return;
    }
    // The same predicate the todo list's chrome is drawn from, so the marker
    // and the keys can never disagree about where Up and Down land.
    if (todoListFocused(controller.state)) {
      if (key.name === 'up' || key.name === 'down') {
        controller.selectNextTodo(key.name === 'down' ? 1 : -1);
        key.preventDefault();
        return;
      }
      if (key.name === 'escape') {
        controller.toggleTodos();
        key.preventDefault();
        return;
      }
    }
    // Like Enter above, pane focus and round navigation yield to a typed
    // command: cursor keys and brackets belong to a non-empty input.
    if ((key.name === 'left' || key.name === 'right') && actions.inputIsEmpty()) {
      // Left to right, the round view drills rounds -> agents -> transcript, so
      // the arrows step across that order and clamp at the ends. The rail joins
      // the order only when it is on screen; narrower than that the view is just
      // agents and transcript, as before.
      // `agents` is only a step in the order while the graph pane is drawn.
      // With it off the rail lists the agents instead, so stepping into a pane
      // nobody can see would strand the arrow keys on nothing.
      const order: RoundFocus[] = roundRailVisible(controller.state, renderer.terminalWidth)
        ? agentGraphEnabled()
          ? ['rounds', 'agents', 'transcript']
          : ['rounds', 'transcript']
        : ['agents', 'transcript'];
      const current = order.indexOf(controller.state.roundFocus);
      const base = current === -1 ? order.indexOf('agents') : current;
      const next = Math.min(order.length - 1, Math.max(0, base + (key.name === 'left' ? -1 : 1)));
      controller.focusRound(order[next] as RoundFocus);
      key.preventDefault();
      return;
    }
    if (key.name === 'up' || key.name === 'down') {
      if (!actions.navigateSuggestions(key.name === 'up' ? -1 : 1)) {
        // A resize can hide the rail while focus still reads `rounds`; stepping it
        // then would move an invisible selection. Only drive the rail while it is
        // on screen, otherwise `rounds` coerces to the agents pane (the side
        // `focusedPane` already reports it as) so the keys stay on a live surface.
        const railFocused =
          controller.state.roundFocus === 'rounds' &&
          roundRailVisible(controller.state, renderer.terminalWidth);
        if (railFocused) {
          if (key.name === 'down') controller.selectNextRound();
          else controller.selectPreviousRound();
        } else if (controller.state.roundFocus === 'transcript') {
          controller.selectNextEntry(key.name === 'down' ? 1 : -1);
          actions.revealSelectedEntry();
        } else {
          if (key.name === 'down') controller.selectNextAgent();
          else controller.selectPreviousAgent();
        }
      }
      key.preventDefault();
      return;
    }
    // Enter drills into the selected row. The hypothesis view already gives it
    // that meaning one rung up (it opens the selected round), and the rail's
    // agents are the next rung down, so the same key opens them and closes them
    // again. Gated on an empty input like `[`, `]` and the arrows: Enter belongs
    // to a typed command whenever there is one.
    if (
      (key.name === 'return' || key.name === 'enter') &&
      controller.state.roundFocus === 'rounds' &&
      roundRailVisible(controller.state, renderer.terminalWidth) &&
      actions.inputIsEmpty()
    ) {
      const round = visibleRoundNumber(controller.state);
      if (round !== null) controller.toggleRoundAgents(round);
      key.preventDefault();
      return;
    }
    if (
      (key.name === 'return' || key.name === 'enter') &&
      controller.state.roundFocus === 'transcript' &&
      actions.inputIsEmpty() &&
      actions.toggleSelectedTool()
    ) {
      actions.revealSelectedEntry();
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
    if (key.name === ']' && actions.inputIsEmpty()) {
      actions.selectNextRound();
      viewport.scrollTo(viewport.scrollHeight);
      key.preventDefault();
      return;
    }
    if (key.name === '[' && actions.inputIsEmpty()) {
      actions.selectPreviousRound();
      viewport.scrollTo(viewport.scrollHeight);
      key.preventDefault();
      return;
    }
    if (key.name === 'pageup') {
      actions.revealOlderEntries();
      viewport.scrollBy(-1, 'viewport');
    } else if (key.name === 'pagedown') viewport.scrollBy(1, 'viewport');
    else if (key.ctrl && key.name === 'up') {
      actions.revealOlderEntries();
      viewport.scrollBy(-1);
    } else if (key.ctrl && key.name === 'down') viewport.scrollBy(1);
    else if (key.name === 'home') {
      // Home reaches the top of what is rendered. On a windowed transcript that
      // is one further block of history per press, rather than one press
      // building every card a 20k-entry run has.
      actions.revealOlderEntries();
      viewport.scrollTo(0);
    } else if (key.name === 'end') viewport.scrollTo(viewport.scrollHeight);
    else return;
    key.preventDefault();
  };

  renderer.keyInput.on('keypress', onKey);
  return () => renderer.keyInput.off('keypress', onKey);
}

/** One typed character, as opposed to a chord or a control key. */
function isPrintable(key: KeyEvent): boolean {
  return (
    !key.ctrl &&
    !key.meta &&
    typeof key.sequence === 'string' &&
    key.sequence.length === 1 &&
    key.sequence >= ' ' &&
    key.sequence !== ''
  );
}
