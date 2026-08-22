import {BoxRenderable, type CliRenderer, ScrollBoxRenderable, TextRenderable} from '@opentui/core';
import type {SessionController} from '../session-controller.js';
import {experimentLogVisible, type SessionState, statusText} from '../session-model.js';
import {AgentMapView} from './agent-map.js';
import {ChatOverlayView} from './chat-overlay.js';
import {ConversationView} from './conversation.js';
import {ExperimentLogView} from './experiment-log.js';
import {createInputPanel} from './input.js';
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

const KEY_HELP =
  '[/]: round · Tab: agent · PgUp/PgDn · Ctrl+T: todos · Ctrl+P: prompt · Ctrl+L: live';
const SCOPED_KEY_HELP =
  '[/]: round · Tab: agent · PgUp/PgDn · Ctrl+T: todos · Ctrl+P: prompt · Esc: experiments';
const LOG_KEY_HELP =
  '↑↓ or scroll: select · Enter or /open-round: open its rounds · /open-round --N';
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
  const agentMap = new AgentMapView(renderer, controller, theme);
  const overlay = new OverlayView(renderer, theme);
  const experimentLog = new ExperimentLogView(renderer, controller, theme);
  const rightPane = new RightPaneView(renderer, theme);
  const themePicker = new ThemePickerView(renderer, theme);
  const conversation = new ConversationView(renderer, controller, markdownStyle, theme);
  const chat = new ChatOverlayView(renderer, controller, markdownStyle, theme);
  const input = createInputPanel(renderer, value => void controller.submit(value), theme);

  viewport.add(conversation.output);
  main.add(agentMap.output);
  main.add(viewport);
  // The log lives in the main pane rather than floating over it: it is the
  // landing view, not a dialog.
  main.add(experimentLog.output);
  main.add(rightPane.output);
  root.add(header);
  root.add(roundStrip.output);
  root.add(main);
  root.add(todoStrip.output);
  root.add(help);
  root.add(input.suggestions);
  root.add(input.box);
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
    input.applyTheme(theme);
    return () => previousMarkdownStyle.destroy();
  };

  let chatWasOpen = false;
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
    const leftWidth = showSplit
      ? renderer.terminalWidth - rightPaneWidth(renderer.terminalWidth)
      : renderer.terminalWidth;
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
        ? LOG_KEY_HELP
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
    experimentLog.setAvailableWidth(showSplit ? leftWidth : null);
    experimentLog.render(state);
    rightPane.render(state, showSplit);
    overlay.render(
      paneFallback === null
        ? state
        : {...state, overlay: {kind: 'detail' as const, content: paneFallback.content}},
    );
    themePicker.render(state);
    chat.render(state);
    if (state.chatOpen && !chatWasOpen) chat.focus();
    if (!state.chatOpen && chatWasOpen) input.focus();
    chatWasOpen = state.chatOpen;
    releasePreviousStyle?.();
  };
  const unbindKeys = bindKeybindings(renderer, controller, viewport, {
    completeInput: () => input.completeSuggestion(),
    inputIsEmpty: () => input.isEmpty(),
    closeChat: () => controller.closeChat(),
    toggleLatestPrompt: () => conversation.toggleLatestPrompt(),
    selectNextAgent: () => controller.selectNextAgent(),
    selectPreviousAgent: () => controller.selectPreviousAgent(),
    selectNextRound: () => controller.selectNextRound(),
    selectPreviousRound: () => controller.selectPreviousRound(),
    toggleTodos: () => controller.toggleTodos(),
    scrollRightPane: delta => rightPane.scrollBy(delta),
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
      chat.destroy();
      roundStrip.destroy();
      agentMap.destroy();
      root.destroyRecursively();
      markdownStyle.destroy();
    },
  };
}