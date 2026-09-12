import {
  BoxRenderable,
  type CliRenderer,
  ScrollBoxRenderable,
  TextAttributes,
  TextRenderable,
} from '@opentui/core';
import {COMMAND_NAMES} from '../commands.js';
import type {SessionController} from '../session-controller.js';
import {
  experimentLogVisible,
  focusedPane,
  type SessionState,
  stripRounds,
  visibleRoundNumber,
} from '../session-model.js';
import {ActivityBarView} from './activity-bar.js';
import {AgentMapView} from './agent-map.js';
import {fillLayer} from './box-fill.js';
import {createChatDraft} from './chat-composer.js';
import {ChatOverlayView} from './chat-overlay.js';
import {ChatPaneView, chatDockFits, chatPaneWidth} from './chat-pane.js';
import {RendererSelectionClipboard, type SelectionClipboard} from './clipboard.js';
import {createCommandInputPanel} from './command-input.js';
import {ConversationView} from './conversation.js';
import {ErrorBannerView} from './error-banner.js';
import {ExperimentLogView} from './experiment-log.js';
import {applyPaneFocus, paneBorderColor, paneBorderStyle, paneTitle} from './focus.js';
import {
  type HeaderSpan,
  headerBackground,
  headerSpanStyle,
  MAX_HEADER_SPANS,
  renderHeader,
} from './header.js';
import {bindKeybindings} from './keybindings.js';
import {OverlayView} from './overlay.js';
import {RightPaneView, rightPaneWidth, splitFits} from './right-pane.js';
import {RoundTabsView} from './round-tabs.js';
import {createMarkdownStyle} from './styles.js';
import {resolveTheme, type ThemeName} from './theme.js';
import {ThemePickerView} from './theme-picker.js';
import {TodoStripView, todoStripHeight, todoStripWidth} from './todo-strip.js';

export interface OpenTuiApp {
  destroy(): void;
}

/** Which of the client's editors currently holds the cursor. */
type FocusTarget = 'command' | 'chat' | 'modal';

const KEY_HELP = `←→: agents/transcript · ↑↓: within · [/] or click: round · F4: zoom · ${COMMAND_NAMES.todos} · ${COMMAND_NAMES.prompt} · Ctrl+L: live`;
const SCOPED_KEY_HELP = `←→: agents/transcript · ↑↓: within · [/] or click: round · F4: zoom · ${COMMAND_NAMES.todos} · ${COMMAND_NAMES.prompt} · Esc: back`;
const LOG_KEY_HELP = `↑↓ or scroll: select · Enter/click: open hypothesis · F4: zoom · ${COMMAND_NAMES['open-round']} --N`;
const LOG_CHAT_KEY_HELP = `↑↓: select · Enter/click: hypothesis · Ctrl+W: chat · F4: zoom · ${COMMAND_NAMES['open-round']} --N`;
const HYPOTHESIS_KEY_HELP =
  '↑↓: select round · Enter/click: trajectory · PgUp/PgDn: scroll · Esc: hypotheses';
/** Bezel, one content row, bezel. See the header frame below. */
const HEADER_FRAME_HEIGHT = 3;

/** Two border cells plus a cell of padding on each side. */
const HEADER_CHROME = 4;

const SPLIT_KEY_HELP =
  'Ctrl+W: switch pane · F4: zoom focused pane · PgUp/PgDn: scroll · Esc: close pane';

const TRANSCRIPT_TITLE = 'Transcript';

/**
 * A round the run has not reached has no turns and never will until it runs.
 * Saying so beats "waiting for run events", which reads as something broken.
 */
function emptyTranscriptMessage(state: SessionState): string {
  const roundNumber = visibleRoundNumber(state);
  if (roundNumber === null) return 'Waiting for run events…';
  const round = stripRounds(state).find(item => item.number === roundNumber);
  if (round?.status === 'planned') return `Round ${roundNumber} has not run yet.`;
  return 'Waiting for run events…';
}

export function createOpenTuiApp(
  renderer: CliRenderer,
  controller: SessionController,
  clipboard: SelectionClipboard = new RendererSelectionClipboard(renderer),
): OpenTuiApp {
  let themeName: ThemeName = controller.state.themeName;
  let theme = resolveTheme(themeName);
  const root = new BoxRenderable(renderer, {
    id: 'app',
    width: '100%',
    height: '100%',
    flexDirection: 'column',
    backgroundColor: theme.canvas,
  });
  // The header is a pane, not a caption. Every other region on screen sits in
  // a bordered box, so a bare line above them reads as floating rather than as
  // part of the same housing. Three rows: bezel, one content row, bezel. The
  // border rows are the breathing room, so there is no internal padding and no
  // blank row beneath: the header's bottom bezel meets the next pane's top one,
  // and two adjacent rules read as a seam between two objects.
  //
  // No background fill. A partial-width fill is what reads as a floating band
  // in a terminal, and a full-width one has nothing to sit against.
  const headerFrame = new BoxRenderable(renderer, {
    id: 'header-frame',
    width: '100%',
    height: HEADER_FRAME_HEIGHT,
    flexDirection: 'column',
    paddingLeft: 1,
    paddingRight: 1,
    border: true,
    // Rounded, like the panes below it. The fill is on an inner layer, so
    // there is nothing in the corner cell to bleed past the arc
    // (tui-conventions.md).
    borderStyle: 'rounded',
    borderColor: theme.border,
  });
  // The same surface every other pane sits on. Without this the frame falls
  // through to the root's `canvas`, which is a different shade, so the header
  // read as a band laid over the UI rather than a pane within it. That is the
  // exact quality the housing exists to remove. Through `headerBackground`
  // because `headerSpanStyle` derives every header tone's contrast against the
  // same call: the fill and the contrast basis are one fact.
  const headerFill = fillLayer(headerFrame, 'header-fill', headerBackground(theme));
  // One renderable per span, because a terminal cell carries one foreground
  // colour and the header's roles do not share one. The row is allocated once
  // at its maximum and repainted in place: the spans change on every frame, and
  // rebuilding thirteen renderables that often is churn the header does not
  // need.
  //
  // `flexShrink: 0` because `renderHeader` has already budgeted the line to this
  // width, so a row that still overruns is a bug rather than something to
  // absorb. Shrinking spans put an ellipsis through every one of them at once
  // (`V...ys·r...ng`) instead of leaving the single cut the budget decided on.
  const headerLine = new BoxRenderable(renderer, {
    id: 'header',
    width: '100%',
    height: 1,
    flexDirection: 'row',
  });
  const headerSpans = Array.from(
    {length: MAX_HEADER_SPANS},
    (_unused, index) =>
      new TextRenderable(renderer, {
        id: `header-span-${index}`,
        content: '',
        fg: theme.textPrimary,
        wrapMode: 'none',
        flexShrink: 0,
      }),
  );
  for (const span of headerSpans) headerLine.add(span);
  headerFrame.add(headerLine);
  /**
   * Paints the budgeted spans onto the row. The tones come from the theme in
   * scope, so a theme change is picked up by the next paint and needs no
   * separate application.
   *
   * A span the current header does not use is hidden rather than emptied: an
   * empty text renderable still measures one cell, and thirteen of those are
   * most of a narrow terminal's header.
   */
  const paintHeader = (spans: HeaderSpan[]): void => {
    for (const [index, cell] of headerSpans.entries()) {
      const span = spans[index];
      cell.visible = span !== undefined;
      if (span === undefined) continue;
      const style = headerSpanStyle(theme, span);
      cell.content = span.text;
      cell.fg = style.fg;
      cell.attributes = style.bold ? TextAttributes.BOLD : TextAttributes.NONE;
    }
  };
  const focusTranscript = (): void => {
    controller.focusPane('left');
    controller.focusRound('transcript');
  };
  const transcriptFrame = new BoxRenderable(renderer, {
    id: 'viewport',
    width: 'auto',
    flexGrow: 1,
    flexDirection: 'column',
    paddingLeft: 1,
    paddingRight: 1,
    border: true,
    borderStyle: paneBorderStyle(false),
    borderColor: paneBorderColor(theme, false),
    title: paneTitle(TRANSCRIPT_TITLE, false),
    onMouseUp: focusTranscript,
  });
  const viewport = new ScrollBoxRenderable(renderer, {
    id: 'transcript-scroll',
    width: '100%',
    flexGrow: 1,
    stickyScroll: true,
    stickyStart: 'bottom',
    viewportCulling: true,
    verticalScrollbarOptions: {showArrows: true},
    onMouseUp: focusTranscript,
  });
  // Every content pane is a column of this row, the chat included. Each of them
  // owns whatever input writes to it, so two panes side by side are the same
  // rectangle and their boxes share a bottom edge because they are siblings
  // rather than because a row was budgeted to line them up.
  const main = new BoxRenderable(renderer, {
    id: 'main',
    width: '100%',
    flexGrow: 1,
    flexDirection: 'row',
  });
  const workspace = new BoxRenderable(renderer, {
    id: 'workspace',
    flexGrow: 1,
    flexDirection: 'column',
  });
  const help = new TextRenderable(renderer, {
    id: 'key-help',
    height: 1,
    fg: theme.textSubtle,
    content: KEY_HELP,
  });
  let renderedKeyHelp = KEY_HELP;
  let transientStatus: string | null = null;
  let markdownStyle = createMarkdownStyle(theme);
  const roundTabs = new RoundTabsView(renderer, controller, theme);
  const todoStrip = new TodoStripView(renderer, controller, theme);
  const errorBanner = new ErrorBannerView(renderer, theme, () => controller.dismissErrorBanner());
  const agentMap = new AgentMapView(renderer, controller, theme);
  const conversationActivityBar = new ActivityBarView(renderer, theme, 'conversation-activity-bar');
  const overlay = new OverlayView(renderer, theme);
  const experimentLog = new ExperimentLogView(renderer, controller, theme);
  const rightPane = new RightPaneView(renderer, theme, () => controller.focusPane('right'));
  const themePicker = new ThemePickerView(renderer, theme);
  // Scrolling back past the rendered window materializes the next block of
  // history. The viewport owns scroll position, so it absorbs the height the
  // revealed cards add and the reader keeps looking at the same content.
  const revealOlderEntries = (): void => {
    const heightBefore = viewport.scrollHeight;
    const top = viewport.scrollTop;
    if (!conversation.revealOlderEntries()) {
      // The window already starts at the oldest entry the client holds, so the
      // next block has to come from the backend. No scroll compensation here:
      // the backfill lands as a state update, and the view follows its window
      // anchor across the prepended entries, so the rendered cards, and with
      // them the scroll height, are unchanged. The reader stays put, and the
      // next gesture reveals the new entries through the branch below.
      void controller.loadOlderHistory();
      return;
    }
    const grew = viewport.scrollHeight - heightBefore;
    if (grew > 0) viewport.scrollTo(top + grew);
  };
  const conversation: ConversationView = new ConversationView(
    renderer,
    controller,
    markdownStyle,
    theme,
    {
      showsSelection: true,
      onFocusRequest: focusTranscript,
      onRevealOlder: revealOlderEntries,
    },
  );
  const chatDraft = createChatDraft();
  const chat = new ChatOverlayView(renderer, controller, markdownStyle, theme, chatDraft);
  const chatPane = new ChatPaneView(renderer, controller, markdownStyle, theme, chatDraft);
  // Composer drafts are per-thread. The shared ChatDraft stays the single
  // authority both chat surfaces read; switching threads swaps its content
  // and parks the outgoing thread's half-typed question for its return.
  const parkedDrafts = new Map<string, string>();
  let draftThreadId = controller.state.activeChatThreadId;
  // Clicking either box moves the pane focus to it, so the border, the hint,
  // and the cursor never disagree about which surface is taking keystrokes.
  const commandInput = createCommandInputPanel(
    renderer,
    value => void controller.submitCommand(value),
    theme,
    () => controller.focusPane('left'),
    () => controller.clearInputError(),
  );
  /**
   * Moves the command box, and the list that completes it, into one pane.
   *
   * Both go straight into the pane rather than into a column of their own, the
   * way the chat pane holds its own composer and menu: the list is positioned
   * against the bottom of whatever contains it, and a wrapper sized to its
   * contents leaves the list no room to open upwards into.
   */
  const hostCommandSurface = (pane: BoxRenderable): void => {
    pane.add(commandInput.suggestions);
    pane.add(commandInput.output);
  };

  // A slash command and a key toggle the same prompt: the controller routes the
  // request, the transcript decides which prompt it applies to.
  controller.onTogglePrompt(() => conversation.toggleLatestPrompt());
  viewport.add(conversation.output);
  transcriptFrame.add(viewport);
  // The frame owns the shared horizontal inset for both turn cards and the
  // fixed activity row. Activity stays outside scrolling content, so a new
  // turn can change scroll height without moving the line.
  transcriptFrame.add(conversationActivityBar.output);
  // The chat is the leftmost column of the same row as the table it discusses,
  // so both panes end on the same line and each keeps its own input inside its
  // own frame.
  main.add(chatPane.output);
  // Drilling deeper into a round reads left to right: agents, then transcript.
  main.add(agentMap.output);
  main.add(transcriptFrame);
  // The log lives in the main pane rather than floating over it: it is the
  // landing view, not a dialog.
  main.add(experimentLog.output);
  main.add(rightPane.output);
  // The pane the command box is currently inside. The first frame is drawn
  // before any experiment log arrives, so the round view's transcript owns it
  // until `render` says otherwise.
  let commandHost: BoxRenderable = transcriptFrame;
  hostCommandSurface(commandHost);
  root.add(headerFrame);
  root.add(errorBanner.output);
  // The round tabs head both panes of the round view, outside every border:
  // the selected tab's fill inside a rounded frame would be #642.
  workspace.add(roundTabs.output);
  workspace.add(main);
  workspace.add(todoStrip.output);
  // The key-help line stays the width of the screen and under every pane. Inside
  // one of them it would be cut to that pane's columns, and a binding that has
  // been truncated away is a binding nobody has.
  workspace.add(help);
  root.add(workspace);
  root.add(overlay.scrim);
  root.add(overlay.output);
  root.add(themePicker.output);
  root.add(chat.output);
  renderer.root.add(root);
  commandInput.focus();

  const applyTheme = (next: ThemeName): (() => void) => {
    themeName = next;
    theme = resolveTheme(next);
    const previousMarkdownStyle = markdownStyle;
    markdownStyle = createMarkdownStyle(theme);
    root.backgroundColor = theme.canvas;
    headerFrame.borderColor = theme.border;
    headerFill.backgroundColor = headerBackground(theme);
    transcriptFrame.borderColor = theme.border;
    help.fg = theme.textSubtle;
    roundTabs.applyTheme(theme);
    todoStrip.applyTheme(theme);
    errorBanner.applyTheme(theme);
    agentMap.applyTheme(theme);
    conversationActivityBar.applyTheme(theme);
    overlay.applyTheme(theme);
    experimentLog.applyTheme(theme);
    rightPane.applyTheme(theme);
    themePicker.applyTheme(theme);
    conversation.applyTheme(theme, markdownStyle);
    chat.applyTheme(theme, markdownStyle);
    chatPane.applyTheme(theme, markdownStyle);
    commandInput.applyTheme(theme);
    return () => previousMarkdownStyle.destroy();
  };

  let focusTarget: FocusTarget = 'command';
  let lastState: SessionState = controller.state;
  const render = (state: SessionState): void => {
    lastState = state;
    const previewName = state.themePicker?.selected ?? state.themeName;
    const releasePreviousStyle = previewName === themeName ? undefined : applyTheme(previewName);
    if (state.activeChatThreadId !== draftThreadId) {
      // Park the outgoing thread's draft and restore the incoming thread's,
      // so switching never sends one thread's question to another's agent.
      parkedDrafts.set(draftThreadId, chatDraft.value);
      chatDraft.value = parkedDrafts.get(state.activeChatThreadId) ?? '';
      draftThreadId = state.activeChatThreadId;
    }
    const showLog = experimentLogVisible(state);
    const paneFocus = focusedPane(state);
    const zoomedPane = state.layout.zoomedPane;
    // A split only happens when the terminal can carry both panes. Narrower
    // than that, a visualization keeps the modal it had before the split
    // existed rather than squeezing two unreadable columns onto the screen.
    const splitOpen = state.layout.right !== null;
    const showSplit = zoomedPane === null && splitOpen && splitFits(renderer.terminalWidth);
    const showRightPane = zoomedPane === 'performance' || (zoomedPane === null && showSplit);
    const paneFallback = zoomedPane === null && splitOpen && !showSplit ? state.layout.right : null;
    // Whatever holds the left side, log or transcript, shares the row with the
    // pane rather than being replaced by it.
    const rightWidth =
      zoomedPane === 'performance'
        ? renderer.terminalWidth
        : showSplit
          ? rightPaneWidth(renderer.terminalWidth)
          : 0;
    const leftWidth = renderer.terminalWidth - rightWidth;
    // Measured here because this is the only place that knows the width, and
    // reported to the controller so a question goes where the operator can see
    // it. The layout below uses the measurement directly rather than waiting
    // for the state to come back, so a resize never draws a stale row.
    const dockFits = chatDockFits(renderer.terminalWidth, rightWidth);
    if (state.chatDockFits !== dockFits) controller.setChatDockFits(dockFits);
    const chatAvailable = showLog && state.hypothesisDetail === null && dockFits && !state.chatOpen;
    const showChatPane = chatAvailable && (zoomedPane === null || zoomedPane === 'chat');
    const chatWidth = showChatPane
      ? zoomedPane === 'chat'
        ? renderer.terminalWidth
        : chatPaneWidth(renderer.terminalWidth, rightWidth)
      : 0;
    const showExperimentLog = showLog && (zoomedPane === null || zoomedPane === 'experiments');
    paintHeader(renderHeader(state, showLog, renderer.terminalWidth - HEADER_CHROME));
    errorBanner.render(state);
    // The log carries its own key hints in its footer, so when it shares the
    // row with a pane the global line is the place for the pane's keys.
    renderedKeyHelp = showSplit
      ? SPLIT_KEY_HELP
      : showLog
        ? state.hypothesisDetail !== null
          ? HYPOTHESIS_KEY_HELP
          : showChatPane
            ? LOG_CHAT_KEY_HELP
            : LOG_KEY_HELP
        : state.hypothesisScope === null
          ? KEY_HELP
          : SCOPED_KEY_HELP;
    help.content = transientStatus ?? renderedKeyHelp;
    // The round tabs and agent map are per-round detail. They belong to a
    // hypothesis trajectory, not to the list of claims.
    const showAgents = !showLog && (zoomedPane === null ? !showSplit : zoomedPane === 'agents');
    const showTranscript = !showLog && (zoomedPane === null || zoomedPane === 'transcript');
    // The tabs head the whole round view, so they give way wherever it is not
    // on screen whole: a zoomed pane, or a split that takes the row's right side.
    const showTabs = !showLog && zoomedPane === null && !showSplit;
    const errorHeight = state.errorBanner === null ? 0 : errorBanner.output.height;
    agentMap.output.visible = showAgents;
    transcriptFrame.visible = showTranscript;
    todoStrip.output.visible = !showLog && zoomedPane === null;
    // Hidden through the view rather than the box, so its live timer stops too
    // instead of redrawing the bar back onto the screen a second later.
    if (!showTabs) roundTabs.hide();
    if (!showLog) {
      const tabRows = showTabs ? roundTabs.render(state, renderer.terminalWidth) : 0;
      // The row budget the agents pane draws from, between the tab row above
      // the main row and the todo strip below it. The strip's share comes from
      // the height the state implies, not from `todoStrip.output.height`: the
      // box height reflects the last committed layout, so reading it back in the
      // same paint that expanded or collapsed the strip bills the pane the
      // previous frame's height and leaves it a row long or short (clipping a
      // graph node or its overflow count) until the next paint.
      //
      // The command box is a column inside one of those panes now rather than a
      // band under them, so it takes no rows off this budget.
      const mainRows = Math.max(
        0,
        renderer.terminalHeight -
          headerFrame.height -
          errorHeight -
          tabRows -
          todoStripHeight(state) -
          help.height,
      );
      agentMap.render(
        state,
        zoomedPane === 'agents' ? renderer.terminalWidth : undefined,
        mainRows,
      );
      // The todo box sits under the agent pane and stops where it stops: the
      // todos belong to an agent, so running them under the transcript would
      // attach them to the wrong thing.
      conversation.setEmptyContent(emptyTranscriptMessage(state));
      const agentWidth = agentMap.output.width;
      todoStrip.render(
        state,
        typeof agentWidth === 'number' ? todoStripWidth(agentWidth, renderer.terminalWidth) : null,
      );
      conversation.render(state);
    }
    // The agent map is the first thing to give up room: it is a summary the
    // visualization largely supersedes while the split is open.
    agentMap.output.visible = showAgents;
    // Inside a round the transcript is one of two navigable panes, so it carries
    // the focus border whenever the round view's keys are on it.
    const transcriptFocused = !showLog && paneFocus === 'transcript';
    applyPaneFocus(transcriptFrame, theme, TRANSCRIPT_TITLE, transcriptFocused);
    // Rows the siblings above and below the main area occupy, measured rather
    // than assumed so a taller todo strip or a wrapped banner still fits. A
    // hidden renderable still reports a row of its own, so each is asked
    // whether it is on screen before its rows are counted.
    const rowsOn = (pane: BoxRenderable): number => (pane.visible ? pane.height : 0);
    // The round tabs give way to a split, so only the header and the banner
    // take rows off the top.
    const above = headerFrame.height + rowsOn(errorBanner.output);
    const belowRows = rowsOn(todoStrip.output) + help.height;
    // Match the chat to the left pane's rectangle so it sits beside the
    // visualization instead of over it.
    if (showSplit) {
      chat.setPaneBounds({
        left: 1,
        width: leftWidth - 2,
        top: above,
        height: renderer.terminalHeight - above - belowRows,
      });
    } else {
      chat.setPaneBounds(null);
    }
    chatPane.render(state, showChatPane, chatWidth);
    const chatInputFocused = showChatPane && state.layout.focus === 'chat';
    // A zoomed chat already has a composer inside it, so the command box stands
    // down rather than following the zoom into a pane that does not want it.
    // The key-help line goes with it, the way it did when the two shared a row.
    const showCommand = zoomedPane !== 'chat';
    commandInput.output.visible = showCommand;
    if (!showCommand) commandInput.suggestions.visible = false;
    help.visible = showCommand;
    // Which pane the command box writes to, and therefore which one it is drawn
    // inside. Clicking it asks for `left` focus, so it belongs to the pane that
    // focus names: the log on the landing view and the transcript inside a
    // round. Zoom is the only thing that can take that pane off screen, and then
    // the box follows the one pane that is left. The narrow-width fallback needs
    // no case of its own: it draws the visualization through the overlay and
    // leaves the pane underneath on screen, still holding the box.
    const nextHost =
      zoomedPane === 'performance'
        ? rightPane.output
        : zoomedPane === 'agents'
          ? agentMap.output
          : showLog
            ? experimentLog.output
            : transcriptFrame;
    if (nextHost !== commandHost) {
      commandHost.remove(commandInput.suggestions);
      commandHost.remove(commandInput.output);
      hostCommandSurface(nextHost);
      commandHost = nextHost;
    }
    // The command list completes the box it belongs to, and on this view that
    // box cannot open a chat that is already beside it.
    commandInput.setCommandContext({chatDocked: showChatPane});
    commandInput.render(state);
    experimentLog.setAvailableWidth(showSplit || showChatPane ? leftWidth - chatWidth : null);
    experimentLog.render(state);
    experimentLog.output.visible = showExperimentLog;
    rightPane.render(state, showRightPane, rightWidth);
    overlay.render(state, paneFallback);
    overlay.renderScrim(state.overlay !== null || state.chatOpen || state.themePicker !== null);
    themePicker.render(state);
    chat.render(state);
    conversationActivityBar.render(state, !showLog);
    // One cursor, three places it can be. The modal owns it while it is open;
    // otherwise it belongs to whichever input the pane focus points at.
    const target: FocusTarget = state.chatOpen ? 'modal' : chatInputFocused ? 'chat' : 'command';
    if (target !== focusTarget) {
      focusTarget = target;
      if (target === 'modal') chat.focus();
      else if (target === 'chat') chatPane.focusComposer();
      else commandInput.focus();
    }
    releasePreviousStyle?.();
  };
  const unbindKeys = bindKeybindings(renderer, controller, viewport, clipboard, {
    completeInput: () => commandInput.completeSuggestion(),
    navigateSuggestions: direction => commandInput.navigateSuggestions(direction),
    // Routed to whichever chat presentation is currently on screen: the
    // modal wins while it is open, otherwise the docked pane.
    navigateChatSuggestions: direction =>
      controller.state.chatOpen
        ? chat.navigateSuggestions(direction)
        : chatPane.navigateSuggestions(direction),
    completeChatInput: () =>
      controller.state.chatOpen ? chat.completeSuggestion() : chatPane.completeSuggestion(),
    // Enter belongs to a pane only when nothing is typed anywhere. Asking which
    // box has the cursor is not enough: a question waiting in the other box is
    // still a question, and Enter must never discard it to open a hypothesis.
    inputIsEmpty: () =>
      commandInput.isEmpty() && chatPane.isComposerEmpty() && chat.isComposerEmpty(),
    closeChat: () => controller.closeChat(),
    toggleLatestPrompt: () => conversation.toggleLatestPrompt(),
    toggleSelectedTool: () => conversation.toggleSelectedTool(),
    revealOlderEntries: revealOlderEntries,
    revealSelectedEntry: () => {
      const card = conversation.selectedCard();
      if (card !== null) viewport.scrollChildIntoView(card.id);
    },
    selectNextAgent: () => controller.selectNextAgent(),
    selectPreviousAgent: () => controller.selectPreviousAgent(),
    selectNextRound: () => controller.selectNextRound(),
    selectPreviousRound: () => controller.selectPreviousRound(),
    toggleTodos: () => controller.toggleTodos(),
    scrollRightPane: delta => rightPane.scrollBy(delta),
    scrollChatPane: delta => chatPane.scrollBy(delta),
    scrollExperimentDetail: delta => experimentLog.scrollBy(delta),
    scrollErrorBanner: delta => errorBanner.scrollBy(delta),
    scrollOverlay: delta => overlay.scrollBy(delta),
    clearTransientStatus: () => {
      if (transientStatus === null) return;
      transientStatus = null;
      help.content = renderedKeyHelp;
    },
    showClipboardStatus: result => {
      transientStatus =
        result === 'copied'
          ? 'Copied selected text · Ctrl+C exits when no text is selected'
          : 'Copy unavailable (OSC52) · selection kept · use your terminal copy command';
      help.content = transientStatus;
    },
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
      commandInput.destroy();
      conversationActivityBar.destroy();
      roundTabs.destroy();
      agentMap.destroy();
      experimentLog.destroy();
      chat.destroy();
      chatPane.destroy();
      root.destroyRecursively();
      markdownStyle.destroy();
    },
  };
}
