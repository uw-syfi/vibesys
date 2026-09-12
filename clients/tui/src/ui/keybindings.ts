import type {CliRenderer, KeyEvent, ScrollBoxRenderable} from '@opentui/core';
import type {SessionController} from '../session-controller.js';
import {
  chatPaneFocused,
  chatPaneVisible,
  experimentLogVisible,
  todoListFocused,
  visiblePhases,
} from '../session-model.js';
import {
  agentGraphMinWidth,
  agentPaneWidthWithOverride,
  agentsPaneVisible,
  clampGraphWidthOverride,
  GRAPH_WIDTH_STEP,
  graphFits,
  STACKED_WIDTH,
} from './agent-map.js';
import type {ClipboardCopyResult, SelectionClipboard} from './clipboard.js';

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
  /** `<`/`>`: the Agents pane's explicit column width, already clamped; `=`: null. */
  setGraphWidthOverride(width: number | null): void;
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
      // The round view is two panes, agents then transcript, so each arrow names
      // its side and holds there at the edge. The round tabs are not a pane.
      controller.focusRound(key.name === 'left' ? 'agents' : 'transcript');
      key.preventDefault();
      return;
    }
    if (key.name === 'up' || key.name === 'down') {
      if (!actions.navigateSuggestions(key.name === 'up' ? -1 : 1)) {
        if (controller.state.roundFocus === 'transcript') {
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
    // Resizes the Agents pane by columns, the way a vim/LazyVim window resize
    // does: `<` shrinks and `>` grows by `GRAPH_WIDTH_STEP` each press, and `=`
    // drops the override so the pane follows the terminal again, which is
    // vim's `<C-w>=`. An override is otherwise sticky for the life of the
    // process, so without `=` a single press would cost the pane its automatic
    // sizing, its no-truncation guarantee, and its growth as agent names get
    // longer, with no way back.
    //
    // The keys belong to the Agents pane, so they are claimed wherever it holds
    // the content row (`agentsPaneVisible` is the same test `app.ts` renders
    // `showAgents` from) and left alone everywhere else, rather than typing a
    // stray character into the command input. While the pane is zoomed they are
    // claimed by nobody: a zoomed pane takes the whole terminal and ignores
    // `graphWidthOverride`, so the key must not mutate a number that would
    // change nothing on screen.
    if (
      (key.name === '>' || key.name === '<' || key.name === '=') &&
      actions.inputIsEmpty() &&
      controller.state.layout.zoomedPane === null &&
      agentsPaneVisible(controller.state, renderer.terminalWidth)
    ) {
      const phases = visiblePhases(controller.state);
      const terminalWidth = renderer.terminalWidth;
      if (key.name === '=') actions.setGraphWidthOverride(null);
      // A terminal too narrow for even the narrowest graph is the stacked list
      // whatever the override says, so a press there would store a width this
      // terminal never drew and then surprise the operator with it on the next
      // resize. Nothing is stored instead.
      else if (graphFits(terminalWidth, phases)) {
        const current = agentPaneWidthWithOverride(
          terminalWidth,
          phases,
          controller.state.graphWidthOverride,
        );
        // `null` is the stacked list, which is both what the pane is drawn at
        // and the narrowest it goes. The gap up to a graph is 26 columns at
        // three stages, 7 at two, and none at one, so it is never a step.
        const width = current ?? STACKED_WIDTH;
        const step = key.name === '>' ? GRAPH_WIDTH_STEP : -GRAPH_WIDTH_STEP;
        // From the stacked list, `>` is the operator asking for the graph that
        // automatic sizing declined to draw, at the only width it has.
        const next =
          current === null
            ? step > 0
              ? agentGraphMinWidth(phases)
              : width
            : clampGraphWidthOverride(current + step, terminalWidth, phases);
        // A press that moves nothing stores nothing, so trying the keys can
        // never arm an override the operator cannot see. That is the whole rule
        // and it covers more than a shrink key at its floor: a round with no
        // phases yet and a round of one short-named stage both have a clamp
        // band one width wide, and a terminal wide enough that automatic sizing
        // already sits at the ceiling has nowhere for `>` to go. Each of those
        // would otherwise convert automatic sizing into a sticky override at
        // the identical width, and pin every later round in the session to it.
        //
        // This compares widths, and it stands for "nothing changes on screen"
        // only because `agentGraphMinWidth(phases) > STACKED_WIDTH` wherever the
        // stacked fallback can appear at all. The two come apart in one
        // transition, list to graph at an unchanged width, which needs the two
        // to be equal while the list is showing: one stage whose kind name runs
        // past 20 characters. The role vocabulary is closed and its longest name
        // is 12 (`src/vibesys/loops/roles.py`), so that state is unreachable.
        // Adding a long role, lowering `NODE_WIDTH_MIN`, or raising
        // `STACKED_WIDTH` is what would make this a presentation test that has
        // to be written as one.
        if (next !== width) actions.setGraphWidthOverride(next);
      }
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
