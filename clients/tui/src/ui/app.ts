import {BoxRenderable, type CliRenderer, ScrollBoxRenderable, TextRenderable} from '@opentui/core';
import type {SessionController} from '../session-controller.js';
import {experimentLogVisible, type SessionState, statusText} from '../session-model.js';
import {AgentMapView} from './agent-map.js';
import {ChatOverlayView} from './chat-overlay.js';
import {ChatPaneView, chatDockFits, chatPaneWidth} from './chat-pane.js';
import {ConversationView} from './conversation.js';
import {ExperimentLogView} from './experiment-log.js';
import {createChatInputPanel, createInputPanel} from './input.js';
import {bindKeybindings} from './keybindings.js';
import {OverlayView} from './overlay.js';
import {RightPaneView, rightPaneWidth, splitFits} from './right-pane.js';
import {RoundStripView} from './round-strip.js';
import {createMarkdownStyle} from './styles.js';
import {resolveTheme, type ThemeName} from './theme.js';
import {ThemePickerView} from './theme-picker.js';
import {TodoStripView} from './todo-strip.js';

export interface OpenTuiApp {
  destroy(): void;
}

/** Which of the client's inputs currently holds the cursor. */
type FocusTarget = 'command' | 'chat' | 'modal';

const KEY_HELP =
  '[/]: round · Tab: agent · PgUp/PgDn · Ctrl+T: todos · Ctrl+P: prompt · Ctrl+L: live';
const SCOPED_KEY_HELP =
  '[/]: round · Tab: agent · PgUp/PgDn · Ctrl+T: todos · Ctrl+P: prompt · Esc: experiments';
const LOG_KEY_HELP =
  '↑↓ or scroll: select · Enter or /open-round: open its rounds · /open-round --N';
const LOG_CHAT_KEY_HELP =
  '↑↓: select · Enter: open its rounds · Ctrl+W: chat and back · /open-round --N';
/**
 * Shown above the docked chat's own input. Short enough to leave a gap before
 * the key hints beside it even in the narrowest column that docks, and it says
 * how to reach the box while the cursor is in the command input, because two
 * boxes on screen must never leave that a guess.
 */
const CHAT_INPUT_HINT = 'Ask about this run';
const CHAT_INPUT_PENDING_HINT = 'Awaiting the agent';
const CHAT_INPUT_BLURRED_HINT = 'Ctrl+W to type here';
const SPLIT_KEY_HELP =
  'Ctrl+W: switch pane · PgUp/PgDn: scroll focused pane · Esc on the pane: close it';

export function createOpenTuiApp(renderer: CliRenderer, controller: SessionController): OpenTuiApp {
  let themeName: ThemeName = controller.state.themeName;
  let theme = resolveTheme(themeName);
  const root = new BoxRenderable(renderer, {
    id: 'app',
    width: '100%',
    height: '100%',
    flexDirection: 'column',
    backgroundColor: theme.canvas,
  });
  const header = new TextRenderable(renderer, {
    id: 'header',
    height: 1,
    fg: theme.accent,
    content: 'VibeSys · connecting',
  });
  const viewport = new ScrollBoxRenderable(renderer, {
    id: 'viewport',
    width: 'auto',
    flexGrow: 1,
    border: true,
    borderStyle: 'rounded',
    borderColor: theme.border,
    stickyScroll: true,
    stickyStart: 'bottom',
    viewportCulling: true,
    verticalScrollbarOptions: {showArrows: true},
  });
  const main = new BoxRenderable(renderer, {
    id: 'main',
    width: '100%',
    flexGrow: 1,
    flexDirection: 'row',
  });
  const help = new TextRenderable(renderer, {
    id: 'key-help',
    height: 1,
    fg: theme.textSubtle,
    content: KEY_HELP,
  });
  let markdownStyle = createMarkdownStyle(theme);
  const roundStrip = new RoundStripView(renderer, controller, theme);
  const todoStrip = new TodoStripView(renderer, controller, theme);
  const agentMap = new AgentMapView(renderer, theme);
  const overlay = new OverlayView(renderer, theme);
  const experimentLog = new ExperimentLogView(renderer, controller, theme);
  const rightPane = new RightPaneView(renderer, theme);
  const themePicker = new ThemePickerView(renderer, theme);
  const conversation = new ConversationView(renderer, controller, markdownStyle, theme);
  const chat = new ChatOverlayView(renderer, controller, markdownStyle, theme);
  const chatPane = new ChatPaneView(renderer, controller, markdownStyle, theme);
  const input = createInputPanel(renderer, value => void controller.submit(value), theme);
  const chatInput = createChatInputPanel(
    renderer,
    value => void controller.submitChat(value),
    theme,
  );
  // The two inputs share a row and split it on the same boundary as the panes
  // above them, so each box sits under the surface it writes to.
  const bottom = new BoxRenderable(renderer, {
    id: 'bottom',
    width: '100%',
    flexShrink: 0,
    flexDirection: 'row',
    alignItems: 'flex-end',
  });
  const chatInputColumn = new BoxRenderable(renderer, {
    id: 'chat-input-column',
    flexDirection: 'column',
    flexShrink: 0,
    flexGrow: 0,
    visible: false,
  });
  const chatInputHint = new TextRenderable(renderer, {
    id: 'chat-input-hint',
    height: 1,
    width: '100%',
    wrapMode: 'none',
    truncate: true,
    fg: theme.textSubtle,
    content: CHAT_INPUT_HINT,
  });
  const commandColumn = new BoxRenderable(renderer, {
    id: 'command-column',
    flexGrow: 1,
    flexShrink: 0,
    flexDirection: 'column',
  });

  viewport.add(conversation.output);
  main.add(agentMap.output);
  // The chat is the leftmost column of the landing view, so it is added before
  // the surfaces it sits beside.
  main.add(chatPane.output);
  main.add(viewport);
  // The log lives in the main pane rather than floating over it: it is the
  // landing view, not a dialog.
  main.add(experimentLog.output);
  main.add(rightPane.output);
  chatInputColumn.add(chatInputHint);
  chatInputColumn.add(chatInput.box);
  commandColumn.add(help);
  // Absolute inside the command column rather than the root, so the list rises
  // out of the input it belongs to instead of across the chat beside it.
  commandColumn.add(input.suggestions);
  commandColumn.add(input.box);
  bottom.add(chatInputColumn);
  bottom.add(commandColumn);
  root.add(header);
  root.add(roundStrip.output);
  root.add(main);
  root.add(todoStrip.output);
  root.add(bottom);
  root.add(overlay.output);
  root.add(themePicker.output);
  root.add(chat.output);
  renderer.root.add(root);
  input.focus();

  const applyTheme = (next: ThemeName): (() => void) => {
    themeName = next;
    theme = resolveTheme(next);
    const previousMarkdownStyle = markdownStyle;
    markdownStyle = createMarkdownStyle(theme);
    root.backgroundColor = theme.canvas;
    header.fg = theme.accent;
    viewport.borderColor = theme.border;
    help.fg = theme.textSubtle;
    roundStrip.applyTheme(theme);
    todoStrip.applyTheme(theme);
    agentMap.applyTheme(theme);
    overlay.applyTheme(theme);
    experimentLog.applyTheme(theme);
    rightPane.applyTheme(theme);
    themePicker.applyTheme(theme);
    conversation.applyTheme(theme, markdownStyle);
    chat.applyTheme(theme, markdownStyle);
    chatPane.applyTheme(theme, markdownStyle);
    chatInput.applyTheme(theme);
    chatInputHint.fg = theme.textSubtle;
    input.applyTheme(theme);
    return () => previousMarkdownStyle.destroy();
  };

  let focusTarget: FocusTarget = 'command';
  let lastState: SessionState = controller.state;
  const render = (state: SessionState): void => {
    lastState = state;
    const releasePreviousStyle =
      state.themeName === themeName ? undefined : applyTheme(state.themeName);
    const showLog = experimentLogVisible(state);
    // A split only happens when the terminal can carry both panes. Narrower
    // than that, a visualization keeps the modal it had before the split
    // existed rather than squeezing two unreadable columns onto the screen.
    const splitOpen = state.layout.right !== null;
    const showSplit = splitOpen && splitFits(renderer.terminalWidth);
    const paneFallback = splitOpen && !showSplit ? state.layout.right : null;
    // Whatever holds the left side, log or transcript, shares the row with the
    // pane rather than being replaced by it.
    const rightWidth = showSplit ? rightPaneWidth(renderer.terminalWidth) : 0;
    const leftWidth = renderer.terminalWidth - rightWidth;
    // Measured here because this is the only place that knows the width, and
    // reported to the controller so a question goes where the operator can see
    // it. The layout below uses the measurement directly rather than waiting
    // for the state to come back, so a resize never draws a stale row.
    const dockFits = chatDockFits(renderer.terminalWidth, rightWidth);
    if (state.chatDockFits !== dockFits) controller.setChatDockFits(dockFits);
    const showChatPane = showLog && dockFits && !state.chatOpen;
    const chatWidth = showChatPane ? chatPaneWidth(renderer.terminalWidth, rightWidth) : 0;
    const dialogOpen = state.chatOpen || state.overlay !== null || state.themePicker !== null;
    const returnHint = dialogOpen ? ' · Esc: close dialog' : '';
    const selection = state.selectedAgentKind ? ` · selected ${state.selectedAgentKind}` : '';
    const scope = state.hypothesisScope === null ? '' : ` · ${state.hypothesisScope.label}`;
    header.content = showLog
      ? `VibeSys · ${statusText(state)} · experiments`
      : `VibeSys · ${statusText(state)}${scope}${selection}${returnHint}`;
    // The log carries its own key hints in its footer, so when it shares the
    // row with a pane the global line is the place for the pane's keys.
    help.content = showSplit
      ? SPLIT_KEY_HELP
      : showLog
        ? showChatPane
          ? LOG_CHAT_KEY_HELP
          : LOG_KEY_HELP
        : state.hypothesisScope === null
          ? KEY_HELP
          : SCOPED_KEY_HELP;
    // The round strip and agent map are per-round detail. They belong to a
    // hypothesis trajectory, not to the list of claims.
    agentMap.output.visible = !showLog;
    viewport.visible = !showLog;
    roundStrip.output.visible = !showLog;
    todoStrip.output.visible = !showLog;
    if (!showLog) {
      roundStrip.render(state);
      todoStrip.render(state);
      agentMap.render(state);
      conversation.render(state);
    }
    // The agent map is the first thing to give up room: it is a summary the
    // visualization largely supersedes while the split is open.
    agentMap.output.visible = !showLog && !showSplit;
    viewport.borderColor =
      showSplit && state.layout.focus === 'left' ? theme.borderFocus : theme.border;
    // Match the chat to the left pane's rectangle so it sits beside the
    // visualization instead of over it. Bounds come from the siblings that
    // actually occupy those rows, so a taller todo strip still fits.
    if (showSplit) {
      const top = header.height + (showLog ? 0 : roundStrip.output.height);
      const below = todoStrip.output.height + help.height + input.box.height;
      chat.setPaneBounds({
        left: 1,
        width: leftWidth - 2,
        top,
        height: renderer.terminalHeight - top - below,
      });
    } else {
      chat.setPaneBounds(null);
    }
    chatPane.render(state, showChatPane, chatWidth);
    // The chat's input tracks the pane above it, so the boundary between the
    // two boxes is the boundary between the two surfaces they write to.
    chatInputColumn.visible = showChatPane;
    chatInputColumn.width = chatWidth;
    const chatInputFocused = showChatPane && state.layout.focus === 'chat';
    chatInputHint.content = state.chatPending
      ? CHAT_INPUT_PENDING_HINT
      : chatInputFocused
        ? CHAT_INPUT_HINT
        : CHAT_INPUT_BLURRED_HINT;
    chatInput.setFocused(chatInputFocused);
    // The command list completes the box it belongs to, and on this view that
    // box cannot open a chat that is already beside it.
    input.setCommandContext({chatDocked: showChatPane});
    experimentLog.setAvailableWidth(showSplit || showChatPane ? leftWidth - chatWidth : null);
    experimentLog.render(state);
    rightPane.render(state, showSplit);
    overlay.render(
      paneFallback === null
        ? state
        : {...state, overlay: {kind: 'detail' as const, content: paneFallback.content}},
    );
    themePicker.render(state);
    chat.render(state);
    // One cursor, three places it can be. The modal owns it while it is open;
    // otherwise it belongs to whichever input the pane focus points at.
    const target: FocusTarget = state.chatOpen ? 'modal' : chatInputFocused ? 'chat' : 'command';
    if (target !== focusTarget) {
      focusTarget = target;
      if (target === 'modal') chat.focus();
      else if (target === 'chat') chatInput.focus();
      else input.focus();
    }
    releasePreviousStyle?.();
  };
  const unbindKeys = bindKeybindings(renderer, controller, viewport, {
    completeInput: () => input.completeSuggestion(),
    // Enter belongs to a pane only when nothing is typed anywhere. Asking which
    // box has the cursor is not enough: a question waiting in the other box is
    // still a question, and Enter must never discard it to open a hypothesis.
    inputIsEmpty: () => input.isEmpty() && chatInput.isEmpty(),
    closeChat: () => controller.closeChat(),
    toggleLatestPrompt: () => conversation.toggleLatestPrompt(),
    selectNextAgent: () => controller.selectNextAgent(),
    selectPreviousAgent: () => controller.selectPreviousAgent(),
    selectNextRound: () => controller.selectNextRound(),
    selectPreviousRound: () => controller.selectPreviousRound(),
    toggleTodos: () => controller.toggleTodos(),
    scrollRightPane: delta => rightPane.scrollBy(delta),
    scrollChatPane: delta => chatPane.scrollBy(delta),
  });
  // Pane widths come from the terminal, so a resize has to redraw even though
  // no state changed.
  const onResize = (): void => render(lastState);
  renderer.on('resize', onResize);
  const unsubscribe = controller.subscribe(render);

  return {
    destroy(): void {
      renderer.off('resize', onResize);
      unsubscribe();
      unbindKeys();
      input.destroy();
      chatInput.destroy();
      chat.destroy();
      roundStrip.destroy();
      agentMap.destroy();
      root.destroyRecursively();
      markdownStyle.destroy();
    },
  };
}
