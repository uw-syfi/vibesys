import {
  type EventSubscription,
  PersistentEventStream,
  type ProtocolResponse,
  type RequestInput,
  type RunEvent,
  ServerError,
  type ServerMessage,
  type StreamConnectionState,
  type SubscribeOptions,
} from '@vibesys/backend-client';
import {DEFAULT_CHAT_THREAD_ID, hasRunEnded} from '@vibesys/core-state';
import type {StartupTrace} from './boot-trace.js';
import {chatHelpText, helpText, type ParsedCommand, parseCommand} from './commands.js';
import {renderPerformanceCurve} from './performance-chart.js';
import {
  activeChatThreadSettings,
  applyEvent,
  applyEventBatch,
  applyEventPrefix,
  applyEventRebootstrap,
  applySnapshot,
  type ChatThreadSettings,
  chatDocked,
  chatMenuCustomModel,
  chatPaneVisible,
  clearAgentSelection,
  clearEntrySelection,
  closeChatMenu,
  closeOverlays,
  closePane,
  closeThemePicker,
  cyclePaneFocus,
  designRoundViews,
  dismissErrorBanner,
  enterExperimentDrilldown,
  enterExperimentRound,
  enterUnownedExperimentRound,
  failChatMenu,
  failExperiments,
  failPane,
  focusPane,
  focusRound,
  initialSessionState,
  leaveExperimentDrilldown,
  leaveHypothesisDetail,
  markEventStreamAvailable,
  markEventStreamUnavailable,
  moveChatMenuSelection,
  moveExperimentSelection,
  moveHypothesisRoundSelection,
  moveThemeSelection,
  normalizeFocus,
  openChat,
  openChatModelMenu,
  openChatResumeMenu,
  openExperimentLog,
  openHypothesisDetail,
  openPane,
  openThemePicker,
  type PaneFocus,
  type PaneView,
  type RoundFocus,
  reportError,
  type SessionState,
  selectAgent,
  selectExperimentActivity,
  selectedChatMenuRow,
  selectNextAgent,
  selectNextEntry,
  selectNextRound,
  selectNextTodo,
  selectPreviousAgent,
  selectPreviousRound,
  selectRound,
  setChatDockFits,
  setChatMenuCustomModel,
  setChatModelMenuOptions,
  setChatThreadPending,
  setChatWidthOverride,
  setDesignLog,
  setExperiments,
  setGraphWidthOverride,
  setPaneContent,
  setTheme,
  showDetail,
  showLive,
  switchChatThread,
  togglePaneZoom,
  toggleTodos,
  updateChatConversation,
} from './session-model.js';
import {renderDesignSummary} from './ui/design-log.js';
import {DEFAULT_THEME_NAME, type ThemeName} from './ui/theme.js';

export interface SessionController {
  readonly state: SessionState;
  start(): Promise<void>;
  stop(): Promise<void>;
  submitCommand(value: string): Promise<void>;
  closeChat(): void;
  sendChat(value: string): Promise<void>;
  submitChat(value: string): Promise<void>;
  /** Makes one thread the chat surfaces' subject. */
  switchChatThread(threadId: string): void;
  /** `/resume`: the thread list, inline beside the composer. */
  openChatResumeMenu(): void;
  /** `/model`: the backend's harness and model options, inline. */
  openChatModelMenu(): Promise<void>;
  /** `/clear`: a fresh thread on this thread's settings, switched to. */
  clearChatThread(): Promise<void>;
  moveChatMenuSelection(delta: number): void;
  /** Enter in the menu: switch threads, or start one on the chosen model. */
  confirmChatMenu(): Promise<void>;
  closeChatMenu(): void;
  typeChatMenuCustomModel(text: string): void;
  backspaceChatMenuCustomModel(): void;
  live(): void;
  selectNextAgent(): void;
  selectPreviousAgent(): void;
  selectNextRound(): void;
  selectPreviousRound(): void;
  selectRound(roundNumber: number): void;
  selectAgent(kind: string): void;
  selectNextEntry(delta: number, id?: string): void;
  clearEntrySelection(): void;
  clearAgentSelection(): void;
  focusRound(focus: RoundFocus): void;
  selectNextTodo(delta: number): void;
  toggleTodos(): void;
  /** `<`/`>`: the Agents pane's explicit column width, already clamped; `=`: null. */
  setGraphWidthOverride(width: number | null): void;
  /** `<`/`>`: the docked chat pane's explicit column width, already clamped; `=`: null. */
  setChatWidthOverride(width: number | null): void;
  /** Expands the latest prompt in view; the view owns what "latest" means. */
  togglePrompt(): void;
  onTogglePrompt(handler: () => void): void;
  setTheme(themeName: ThemeName): void;
  openExperimentLog(): Promise<void>;
  openRound(roundNumber?: number): void;
  openPane(view: PaneView): Promise<void>;
  closePane(): void;
  closeOverlays(): void;
  dismissErrorBanner(): void;
  cyclePaneFocus(): void;
  focusPane(focus: PaneFocus): void;
  togglePaneZoom(): void;
  setChatDockFits(fits: boolean): void;
  moveExperimentSelection(delta: number): void;
  openHypothesisDetail(entryKey?: string): void;
  moveHypothesisRoundSelection(delta: number): void;
  selectExperimentActivity(): void;
  enterExperimentDrilldown(): void;
  leaveExperimentDrilldown(): void;
  leaveHypothesisDetail(): void;
  openThemePicker(): void;
  moveThemeSelection(delta: number): void;
  applySelectedTheme(): void;
  closeThemePicker(): void;
  /** Loads the chunk of history just older than what is folded. Resolves false when history is already complete. */
  loadOlderHistory(): Promise<boolean>;
  subscribe(listener: (state: SessionState) => void): () => void;
}

export interface ServerTransport {
  request(input: RequestInput): Promise<ProtocolResponse>;
  subscribe(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
    options?: SubscribeOptions,
  ): Promise<EventSubscription>;
  close(): Promise<void>;
}

/**
 * How much history the boot subscribe asks for. A long-lived run holds tens of
 * thousands of events, and replaying all of them costs seconds of wire, parse,
 * and fold before the first frame. A thousand events covers what an operator
 * opens the client to look at; the rest loads when they scroll back for it, in
 * chunks of the same size so one backfill is one round trip of the same shape.
 */
const BOOTSTRAP_TAIL = 1_000;
const BACKFILL_CHUNK = 1_000;

/**
 * Backoff between reconnect attempts after the event stream drops. The
 * schedule is finite: a server that refuses this many dials in a row is not
 * coming back on its own, and the disconnect banner stays up as the
 * persistent answer. A successful resubscribe resets the count, so the next
 * outage gets the full schedule again.
 */
const RECONNECT_DELAYS_MS: readonly number[] = [500, 1_000, 2_000, 4_000, 8_000];

export class SocketSessionController implements SessionController {
  #state: SessionState;
  readonly #listeners = new Set<(state: SessionState) => void>();
  #stream: PersistentEventStream;
  #chatMessageId = 0;
  readonly #chatQueue: Array<{id: string; text: string; threadId: string}> = [];
  #chatDrain: Promise<void> | null = null;
  /** Single-flight guard for semantic experiment-log invalidations. */
  #experimentFetch: Promise<void> | null = null;
  #experimentRefreshPending = false;
  /**
   * When the client first asked for experiments, and whether the answer has
   * been timed yet. The first request can be answered `experiments_ready:
   * false`, so the elapsed time spans every retry until entries actually land,
   * which is exactly how long the landing view shows "Loading experiments...".
   */
  #experimentsRequestedAt: number | null = null;
  #experimentsLoadTraced = false;
  #paneFetch: Promise<void> | null = null;
  /** Single-flight guard for the supplemental design-log refresh. */
  #designFetch: Promise<void> | null = null;
  /** Single-flight guard for on-demand history backfill. */
  #historyFetch: Promise<boolean> | null = null;
  /**
   * Sequences already folded from below the history floor.
   *
   * A tail subscription's batch is not only the tail: the server also replays
   * the run-level spine from before the floor (`run_started`, `round_finished`,
   * `chat_thread_created`, the terminal events, …) so the ordinary reducer can
   * derive what a suffix cannot carry. A backfill chunk covering that range
   * therefore re-delivers those same events, and `reduceEventPrefix` folds into
   * a fresh state with no `sequence <= state.sequence` guard to catch them. The
   * set is O(rounds), and filtering every chunk through it is what keeps one
   * `round_finished` from becoming two.
   */
  readonly #foldedBelowFloor = new Set<number>();
  /** Lowest history floor seen so far; see `#lowerHistoryFloor`. */
  #historyFloor = Number.POSITIVE_INFINITY;
  /**
   * Highest floor the stream itself has declared, which is not the same as the
   * floor in state: backfill lowers the latter and the stream never sees it.
   * A later batch declaring more than this is a re-bootstrap; see
   * `#raiseHistoryFloor`. Null until the first batch, whose floor is the
   * bootstrap's own and therefore raises nothing.
   */
  #declaredFloor: number | null = null;
  #streamProtocolError = false;

  constructor(
    private readonly client: ServerTransport,
    themeName: ThemeName = DEFAULT_THEME_NAME,
    /**
     * Where boot measurements go. The controller only reports; whether
     * anything is written, and how it is anchored, belongs to the sink
     * (`boot-trace.ts`), which is why the default discards.
     */
    private readonly trace: StartupTrace = () => {},
    /**
     * Backoff schedule for stream reconnects; its length bounds the attempts
     * per outage. Handed to the stream, and injected by tests so a retry is
     * immediate instead of half a second away.
     */
    reconnectDelaysMs: readonly number[] = RECONNECT_DELAYS_MS,
  ) {
    this.#state = initialSessionState(themeName);
    this.#stream = new PersistentEventStream(client, {
      tail: BOOTSTRAP_TAIL,
      reconnectDelaysMs,
    });
  }

  get state(): SessionState {
    return this.#state;
  }

  /**
   * Boots the session: the snapshot, the experiment log, and the event
   * subscription run concurrently.
   *
   * Nothing here orders them. Each applies its own result onto whatever state
   * is current when it lands, and a snapshot older than the replayed event
   * cursor is rejected by `reduceSnapshot`, so a late snapshot cannot undo
   * events that already arrived. Sequencing them only made boot cost the sum
   * of three round trips, the replay being by far the longest.
   */
  async start(): Promise<void> {
    await Promise.all([
      this.#loadSnapshot(),
      // The log is the landing view, so it is populated before the first frame
      // rather than on demand.
      this.#loadExperiments(),
      // The stream owns its own dial/redial loop; the controller supplies where
      // to resume from, whether a drop is worth reconnecting, and where the
      // messages and connection changes land.
      this.#stream.subscribe({
        cursor: () => this.#state.core.sequence,
        shouldReconnect: () => !hasRunEnded(this.#state.core) && !this.#streamProtocolError,
        onMessage: (message, {resumed}) => this.#onMessage(message, resumed),
        onConnectionState: state => this.#onConnectionState(state),
      }),
    ]);
  }

  #onConnectionState(state: StreamConnectionState): void {
    if (state.status === 'connected') {
      this.#setState(markEventStreamAvailable(this.#state));
    } else {
      this.#setState(
        reportCaughtError(markEventStreamUnavailable(this.#state), state.error, 'transport'),
      );
    }
  }

  async #loadSnapshot(): Promise<void> {
    try {
      const response = await this.client.request({type: 'query.snapshot'});
      if (response.snapshot) this.#setState(applySnapshot(this.#state, response.snapshot));
    } catch (error) {
      this.#setState(reportCaughtError(this.#state, error, 'request'));
    }
  }

  /**
   * Loads the chunk of history just older than what is folded. Resolves false
   * when history is already complete.
   *
   * Single-flight: a reader holding the scroll gesture at the top asks
   * repeatedly, and each answer moves the floor, so overlapping requests would
   * fetch the same range twice and fold it twice.
   */
  loadOlderHistory(): Promise<boolean> {
    if (this.#state.core.historyAfterSequence === 0) return Promise.resolve(false);
    if (this.#historyFetch !== null) return this.#historyFetch;
    const fetch = this.#requestOlderHistory().finally(() => {
      this.#historyFetch = null;
    });
    this.#historyFetch = fetch;
    return fetch;
  }

  async #requestOlderHistory(): Promise<boolean> {
    const floor = this.#state.core.historyAfterSequence;
    const nextFloor = Math.max(0, floor - BACKFILL_CHUNK);
    try {
      const response = await this.client.request({
        type: 'query.events',
        after_sequence: nextFloor,
        // Every folded event has `sequence > floor`, so the range has to
        // include the floor itself and stops one above it.
        before_sequence: floor + 1,
      });
      // Spine events replayed with the tail fall inside this range; folding
      // them a second time would duplicate their transcript entries.
      const events = (response.events ?? []).filter(
        event => event.sequence === undefined || !this.#foldedBelowFloor.has(event.sequence),
      );
      this.#setState(applyEventPrefix(this.#state, events, this.#lowerHistoryFloor(nextFloor)));
      return true;
    } catch (error) {
      // The floor stays where it was, so the same range is retried the next
      // time the reader asks for it.
      this.#setState(reportCaughtError(this.#state, error, 'request'));
      return false;
    }
  }

  async stop(): Promise<void> {
    await this.#stream.close();
    await this.client.close();
  }

  subscribe(listener: (state: SessionState) => void): () => void {
    this.#listeners.add(listener);
    listener(this.#state);
    return () => this.#listeners.delete(listener);
  }

  live(): void {
    this.#setState(showLive(this.#state));
  }

  selectAgent(kind: string): void {
    this.#setState(selectAgent(this.#state, kind));
  }

  selectNextEntry(delta: number, id?: string): void {
    this.#setState(selectNextEntry(this.#state, delta, id));
  }

  clearEntrySelection(): void {
    this.#setState(clearEntrySelection(this.#state));
  }

  clearAgentSelection(): void {
    this.#setState(clearAgentSelection(this.#state));
  }

  focusRound(focus: RoundFocus): void {
    this.#setState(focusRound(this.#state, focus));
  }

  selectNextTodo(delta: number): void {
    this.#setState(selectNextTodo(this.#state, delta));
  }

  selectNextAgent(): void {
    this.#setState(selectNextAgent(this.#state));
  }

  selectPreviousAgent(): void {
    this.#setState(selectPreviousAgent(this.#state));
  }

  selectNextRound(): void {
    this.#setState(selectNextRound(this.#state));
  }

  selectPreviousRound(): void {
    this.#setState(selectPreviousRound(this.#state));
  }

  selectRound(roundNumber: number): void {
    this.#setState(selectRound(this.#state, roundNumber));
  }

  #promptToggle: (() => void) | null = null;

  /**
   * The prompt lives in the transcript, so the view performs the toggle; the
   * controller only routes the request, which is what lets a slash command and
   * a key do the same thing.
   */
  onTogglePrompt(handler: () => void): void {
    this.#promptToggle = handler;
  }

  togglePrompt(): void {
    this.#promptToggle?.();
  }

  toggleTodos(): void {
    this.#setState(toggleTodos(this.#state));
  }

  setGraphWidthOverride(width: number | null): void {
    this.#setState(setGraphWidthOverride(this.#state, width));
  }

  setChatWidthOverride(width: number | null): void {
    this.#setState(setChatWidthOverride(this.#state, width));
  }

  setTheme(themeName: ThemeName): void {
    this.#setState(setTheme(this.#state, themeName));
  }

  openThemePicker(): void {
    this.#setState(openThemePicker(this.#state));
  }

  moveThemeSelection(delta: number): void {
    this.#setState(moveThemeSelection(this.#state, delta));
  }

  /** Enter in the picker: the highlighted theme becomes the session's. */
  applySelectedTheme(): void {
    const picker = this.#state.themePicker;
    if (picker === null) return;
    this.setTheme(picker.selected);
  }

  closeThemePicker(): void {
    this.#setState(closeThemePicker(this.#state));
  }

  closeChat(): void {
    this.#setState({...this.#state, chatOpen: false});
  }

  switchChatThread(threadId: string): void {
    this.#setState(switchChatThread(this.#state, threadId));
  }

  openChatResumeMenu(): void {
    this.#setState(openChatResumeMenu(this.#state));
  }

  async openChatModelMenu(): Promise<void> {
    this.#setState(openChatModelMenu(this.#state));
    try {
      const response = await this.client.request({type: 'query.chat_options'});
      const options = response.chat_options;
      this.#setState(
        options === null || options === undefined
          ? failChatMenu(this.#state, 'This run has not reported its chat options yet.')
          : setChatModelMenuOptions(this.#state, options),
      );
    } catch (error) {
      this.#setState(
        reportCaughtError(failChatMenu(this.#state, errorMessage(error)), error, 'request'),
      );
    }
  }

  /**
   * `/clear` keeps the operator on the same agent: the new thread inherits the
   * current thread's settings, and the old thread stays resumable through
   * `/resume`. Threads are immutable in their agent and model by design, so a
   * fresh conversation is a fresh thread.
   */
  async clearChatThread(): Promise<void> {
    await this.#createChatThread(activeChatThreadSettings(this.#state));
  }

  moveChatMenuSelection(delta: number): void {
    this.#setState(moveChatMenuSelection(this.#state, delta));
  }

  async confirmChatMenu(): Promise<void> {
    const row = selectedChatMenuRow(this.#state);
    if (row === null) return;
    if (row.kind === 'thread') {
      this.switchChatThread(row.threadId);
      return;
    }
    if (row.kind !== 'model' && row.kind !== 'custom') return;
    const model = row.kind === 'custom' ? chatMenuCustomModel(this.#state).trim() : row.model;
    // A custom entry with nothing typed is not a choice yet; the menu stays
    // open rather than silently starting a thread on the run's default.
    if (model === '') return;
    this.#setState(closeChatMenu(this.#state));
    await this.#createChatThread({provider: row.provider, model});
  }

  closeChatMenu(): void {
    this.#setState(closeChatMenu(this.#state));
  }

  typeChatMenuCustomModel(text: string): void {
    this.#setState(setChatMenuCustomModel(this.#state, chatMenuCustomModel(this.#state) + text));
  }

  backspaceChatMenuCustomModel(): void {
    this.#setState(
      setChatMenuCustomModel(this.#state, chatMenuCustomModel(this.#state).slice(0, -1)),
    );
  }

  /**
   * Asks the backend for a new thread and switches to it. No driver is sent:
   * the run's driver is a deployment detail the backend owns. The response's
   * replayed events carry the authoritative thread record.
   */
  async #createChatThread(settings: ChatThreadSettings | null): Promise<void> {
    try {
      const response = await this.client.request({
        type: 'query.chat_thread_create',
        ...(settings === null ? {} : {provider: settings.provider, model: settings.model}),
      });
      let state = closeChatMenu(this.#state);
      for (const event of response.events ?? []) state = applyEvent(state, event);
      const threadId = response.chat_thread?.thread_id;
      this.#setState(threadId === undefined ? state : switchChatThread(state, threadId));
    } catch (error) {
      this.#setState(reportCaughtError(this.#state, error, 'request'));
    }
  }

  async openExperimentLog(): Promise<void> {
    this.#setState(openExperimentLog(this.#state));
    await this.#loadExperiments();
  }

  openRound(roundNumber?: number): void {
    this.#setState(this.#openRoundState(roundNumber));
  }

  #openRoundState(roundNumber: number | undefined): SessionState {
    if (roundNumber !== undefined) {
      return (
        enterExperimentRound(this.#state, roundNumber) ??
        enterUnownedExperimentRound(this.#state, roundNumber) ??
        showDetail(this.#state, `Round ${roundNumber} has not been recorded.`)
      );
    }
    const scope = this.#state.hypothesisScope;
    if (scope !== null) {
      return showDetail(
        this.#state,
        `Already inside ${scope.id}. Esc returns to the experiment log.`,
      );
    }
    const firstStep = enterExperimentDrilldown(this.#state);
    // The ordinary UI stops at the hypothesis summary. `/open-round` names a
    // round-level action explicitly, so it advances through that summary to
    // the currently selected (latest by default) round.
    const opened =
      firstStep.hypothesisDetail !== null && firstStep.hypothesisScope === null
        ? enterExperimentDrilldown(firstStep)
        : firstStep;
    return opened === this.#state
      ? showDetail(this.#state, 'Select a hypothesis first, or use /open-round --N.')
      : opened;
  }

  async openPane(view: PaneView): Promise<void> {
    this.#setState(openPane(this.#state, view));
    await this.#loadPane(view);
  }

  closePane(): void {
    this.#setState(closePane(this.#state));
  }

  closeOverlays(): void {
    this.#setState(closeOverlays(this.#state));
  }

  dismissErrorBanner(): void {
    this.#setState(dismissErrorBanner(this.#state));
  }

  cyclePaneFocus(): void {
    this.#setState(cyclePaneFocus(this.#state));
  }

  focusPane(focus: PaneFocus): void {
    this.#setState(focusPane(this.#state, focus));
  }

  togglePaneZoom(): void {
    this.#setState(togglePaneZoom(this.#state));
  }

  /**
   * Reported by the renderer, which is the only part that knows how many
   * columns there are. It decides whether a question opens the modal or lands
   * in the pane beside the log.
   */
  setChatDockFits(fits: boolean): void {
    this.#setState(setChatDockFits(this.#state, fits));
  }

  /**
   * Re-runs the query behind whichever visualization is on screen. The pane
   * holds rendered text, so refreshing it is the same path as opening it.
   */
  async #loadPane(view: PaneView): Promise<void> {
    if (this.#paneFetch !== null) return this.#paneFetch;
    const fetch = this.#requestPane(view).finally(() => {
      this.#paneFetch = null;
    });
    this.#paneFetch = fetch;
    return fetch;
  }

  async #requestPane(view: PaneView): Promise<void> {
    try {
      const content = await this.#renderPane(view);
      this.#setState(setPaneContent(this.#state, view, content));
    } catch (error) {
      const message = errorMessage(error);
      this.#setState(reportCaughtError(failPane(this.#state, view, message), error, 'request'));
    }
  }

  /** Exhaustive over PaneView, so a new visualization is a compile error. */
  #renderPane(view: PaneView): Promise<string> {
    switch (view) {
      case 'perf':
        return this.#renderPerfPane();
      case 'design':
        return this.#renderDesignPane();
      default: {
        const unreachable: never = view;
        throw new Error(`Unknown pane view: ${String(unreachable)}`);
      }
    }
  }

  async #renderPerfPane(): Promise<string> {
    const response = await this.client.request({type: 'query.performance'});
    return renderPerformanceCurve(
      response.performance ?? [],
      response.events ?? [],
      response.performance_context,
    );
  }

  /**
   * The design query carries only each round's file list. Every stage fact on
   * the same row comes from the experiment log already in state, joined by
   * round, so the pane and the drill-down cannot disagree about a round.
   */
  async #renderDesignPane(): Promise<string> {
    const response = await this.client.request({type: 'query.design'});
    if (response.design_ready === false) {
      return 'The design log is not available until a run is attached.';
    }
    const rounds = response.design ?? [];
    this.#setState(setDesignLog(this.#state, rounds));
    return renderDesignSummary(designRoundViews(rounds, this.#state.experimentLog?.entries ?? []));
  }

  /**
   * Both visualizations are functions of completed rounds and recorded
   * metrics, so those two events bound every change either can show.
   */
  #refreshPaneFor(events: readonly RunEvent[]): void {
    const right = this.#state.layout.right;
    if (right === null) return;
    const relevant = events.some(
      event =>
        event.type === 'round_finished' ||
        event.type === 'benchmark_result' ||
        event.data?.kind === 'benchmark_result',
    );
    if (relevant) void this.#loadPane(right.view);
  }

  moveExperimentSelection(delta: number): void {
    this.#setState(moveExperimentSelection(this.#state, delta));
  }

  openHypothesisDetail(entryKey?: string): void {
    this.#setState(openHypothesisDetail(this.#state, entryKey));
  }

  moveHypothesisRoundSelection(delta: number): void {
    this.#setState(moveHypothesisRoundSelection(this.#state, delta));
  }

  selectExperimentActivity(): void {
    this.#setState(selectExperimentActivity(this.#state));
  }

  enterExperimentDrilldown(): void {
    this.#setState(enterExperimentDrilldown(this.#state));
  }

  leaveExperimentDrilldown(): void {
    this.#setState(leaveExperimentDrilldown(this.#state));
  }

  leaveHypothesisDetail(): void {
    this.#setState(leaveHypothesisDetail(this.#state));
  }

  async #loadExperiments(): Promise<void> {
    this.#experimentsRequestedAt ??= performance.now();
    if (this.#experimentFetch !== null) return this.#experimentFetch;
    const fetch = this.#requestExperiments().finally(() => {
      this.#experimentFetch = null;
      if (this.#experimentRefreshPending) {
        this.#experimentRefreshPending = false;
        void this.#loadExperiments();
      }
    });
    this.#experimentFetch = fetch;
    return fetch;
  }

  async #requestExperiments(): Promise<void> {
    try {
      const response = await this.client.request({type: 'query.experiments'});
      if (response.experiments_ready === false) return;
      const entries = response.experiments ?? [];
      this.#setState(setExperiments(this.#state, entries));
      this.#traceExperimentsLoaded(entries.length);
    } catch (error) {
      const message = errorMessage(error);
      this.#setState(reportCaughtError(failExperiments(this.#state, message), error, 'request'));
    }
    void this.#refreshDesignLog();
  }

  /**
   * The design log annotates the hypothesis drill-down, so it rides the same
   * triggers as the experiment fetch but never its promise: the query runs git
   * over the run's history, and the landing view must not wait on that to
   * report boot. Single-flighted on its own so overlapping refreshes collapse.
   * Best effort: on failure the drill-down keeps whatever it last showed and
   * stays quiet, because `/design` reports errors through the pane.
   */
  #refreshDesignLog(): Promise<void> {
    if (this.#designFetch !== null) return this.#designFetch;
    const fetch = this.#requestDesignLog().finally(() => {
      this.#designFetch = null;
    });
    this.#designFetch = fetch;
    return fetch;
  }

  async #requestDesignLog(): Promise<void> {
    try {
      const response = await this.client.request({type: 'query.design'});
      if (response.design_ready === false) return;
      this.#setState(setDesignLog(this.#state, response.design ?? []));
    } catch {
      // Supplemental data: no banner, no experiment-log error state.
    }
  }

  /** Reports the first delivery only: later refreshes are not a boot cost. */
  #traceExperimentsLoaded(entryCount: number): void {
    if (this.#experimentsLoadTraced || this.#experimentsRequestedAt === null) return;
    this.#experimentsLoadTraced = true;
    const elapsed = Math.round(performance.now() - this.#experimentsRequestedAt);
    this.trace(`experiments loaded in ${elapsed}ms (${entryCount} entries)`);
  }

  /**
   * What the chat composer submits. Chat is controlled from the chat, so its
   * own commands resolve here first; a command that belongs to the global
   * surface (`/pause`, `/perf`, …) still runs through exactly the same path as
   * the main input, and anything else is a question for the chat agent.
   */
  submitChat(value: string): Promise<void> {
    const text = value.trim();
    if (!text.startsWith('/')) return this.sendChat(value);
    const action = parseCommand(text, {surface: 'chat'});
    switch (action.kind) {
      case 'chatClear':
        return this.clearChatThread();
      case 'chatModel':
        return this.openChatModelMenu();
      case 'chatSwitch':
        this.openChatResumeMenu();
        return Promise.resolve();
      case 'help':
      case 'unknown':
        // /help and any unrecognized slash input answer with the chat's own
        // help rather than the global one or a global "unknown command" error.
        this.#showChatHelp();
        return Promise.resolve();
      default:
        // Everything else, including a usage error, goes to the one command
        // executor as the action the chat's own registry lookup produced. The
        // text is not re-parsed on the command surface, so a chat-only
        // command's usage error stays a usage error instead of becoming
        // "unknown command" for a name that surface does not offer.
        return this.#dispatchCommand(action);
    }
  }

  #showChatHelp(): void {
    this.#setState(
      updateChatConversation(this.#state, this.#state.activeChatThreadId, entries => [
        ...entries,
        {
          id: `chat-help-${++this.#chatMessageId}`,
          kind: 'status',
          label: 'Chat commands',
          content: chatHelpText(),
        },
      ]),
    );
  }

  sendChat(value: string): Promise<void> {
    const text = value.trim();
    if (!text) return Promise.resolve();
    const id = `chat-user-${++this.#chatMessageId}`;
    // The message belongs to the thread on screen when it was typed, even if
    // the operator switches threads before the agent gets to it.
    const threadId = this.#state.activeChatThreadId;
    const queued = this.#state.chatPending || this.#chatQueue.length > 0;
    this.#chatQueue.push({id, text, threadId});
    this.#setState(
      updateChatConversation(
        {
          ...this.#state,
          // Docked, the answer lands in the pane the operator is already
          // looking at, so nothing has to open over the log to show it.
          ...(chatDocked(this.#state) ? {} : {chatOpen: true}),
        },
        threadId,
        entries => [
          ...entries,
          {id, kind: 'user', label: queued ? 'You · queued' : 'You', content: text},
        ],
      ),
    );
    if (this.#chatDrain === null) {
      const drain = this.#drainChatQueue();
      this.#chatDrain = drain.finally(() => {
        this.#chatDrain = null;
      });
    }
    return this.#chatDrain;
  }

  async #drainChatQueue(): Promise<void> {
    const pendingThreads = new Set<string>();
    try {
      while (this.#chatQueue.length > 0) {
        // One request per thread: batching across threads would hand one
        // agent another thread's question.
        const threadId = this.#chatQueue[0]?.threadId ?? DEFAULT_CHAT_THREAD_ID;
        const messages = this.#chatQueue.filter(message => message.threadId === threadId);
        for (const message of messages) {
          this.#chatQueue.splice(this.#chatQueue.indexOf(message), 1);
        }
        const messageIds = new Set(messages.map(message => message.id));
        pendingThreads.add(threadId);
        this.#setState(
          updateChatConversation(
            setChatThreadPending(this.#state, threadId, true),
            threadId,
            entries =>
              entries.map(entry => (messageIds.has(entry.id) ? {...entry, label: 'You'} : entry)),
          ),
        );
        await this.#requestChat(messages.map(message => message.text).join('\n\n'), threadId);
        pendingThreads.delete(threadId);
        this.#setState(setChatThreadPending(this.#state, threadId, false));
      }
    } finally {
      let state = this.#state;
      for (const threadId of pendingThreads) {
        state = setChatThreadPending(state, threadId, false);
      }
      this.#setState(state);
    }
  }

  async #requestChat(text: string, threadId: string): Promise<void> {
    try {
      const response = await this.client.request({
        type: 'query.chat',
        text,
        ...(threadId === DEFAULT_CHAT_THREAD_ID ? {} : {thread_id: threadId}),
      });
      const answer = response.chat?.answer ?? 'No chat answer was returned.';
      let state = this.#state;
      for (const event of response.events ?? []) state = applyEvent(state, event);
      if (!(response.events ?? []).some(event => event.data?.kind === 'chat')) {
        state = updateChatConversation(state, threadId, entries => [
          ...entries,
          {
            id: `chat-answer-${++this.#chatMessageId}`,
            kind: 'assistant',
            label: 'Answer',
            content: answer,
          },
        ]);
      }
      this.#setState(state);
    } catch (error) {
      const message = errorMessage(error);
      this.#setState(
        updateChatConversation(
          reportCaughtError(this.#state, error, 'request'),
          threadId,
          entries => [
            ...entries,
            {
              id: `chat-error-${++this.#chatMessageId}`,
              kind: 'result',
              label: 'Chat failed',
              tone: 'failure',
              content: message,
            },
          ],
        ),
      );
    }
  }

  submitCommand(value: string): Promise<void> {
    return this.#dispatchCommand(
      parseCommand(value.trim(), {
        surface: 'command',
        chatDocked: chatPaneVisible(this.#state),
      }),
    );
  }

  /**
   * Applies one resolved command action. Both input surfaces end here, so the
   * behavior of a shared command cannot depend on where it was typed. The
   * switch is exhaustive, so registering a new action kind without handling it
   * is a compile error.
   */
  async #dispatchCommand(action: ParsedCommand): Promise<void> {
    switch (action.kind) {
      case 'unknown':
        return this.#setState(
          reportError(this.#state, `Unknown command: ${action.text}. Use /help.`, {scope: 'input'}),
        );
      case 'error':
        return this.#setState(reportError(this.#state, action.error, {scope: 'input'}));
      case 'help':
        return this.#setState(
          showDetail(this.#state, helpText({chatDocked: chatPaneVisible(this.#state)}), 'help'),
        );
      case 'openChat':
        this.#setState(openChat(this.#state));
        if (action.chatMessage) await this.sendChat(action.chatMessage);
        return;
      case 'toggle':
        if (action.toggle === 'todos') this.toggleTodos();
        else this.togglePrompt();
        return;
      case 'openRound':
        this.openRound(action.round);
        return;
      case 'theme':
        if (action.themeName === undefined) return this.openThemePicker();
        return this.setTheme(action.themeName);
      case 'chatClear':
        return this.clearChatThread();
      case 'chatModel':
        return this.openChatModelMenu();
      case 'chatSwitch':
        this.openChatResumeMenu();
        return;
      case 'request': {
        if (action.paneView !== undefined) return this.openPane(action.paneView);
        try {
          const response = await this.client.request(action.request);
          const rendered = renderResponse(action.request, response, action.responseView);
          if (rendered !== null) this.#setState(showDetail(this.#state, rendered));
        } catch (error) {
          this.#setState(reportCaughtError(this.#state, error, 'request'));
        }
        return;
      }
      default:
        return assertNever(action);
    }
  }

  #onMessage(message: ServerMessage, resumed: boolean): void {
    if (message.type === 'event') {
      this.#setState(applyEvent(this.#state, message.event));
      this.#refreshExperimentsFor([message.event]);
      this.#refreshPaneFor([message.event]);
    }
    if (message.type === 'event_batch') {
      if (resumed) {
        // A resumed subscription replays exactly the events after the
        // client's own cursor and declares no history floor of its own
        // (`history_after_sequence` 0 on every batch). Taking that literally
        // would mark history complete and quietly break scroll-back, so the
        // floor bookkeeping keeps the boot subscription's answers and the
        // batch folds at the floor already in state.
        this.#setState(
          applyEventBatch(
            this.#state,
            message.events,
            message.active_executions,
            message.through_sequence,
            this.#state.core.historyAfterSequence,
          ),
        );
      } else {
        const declared = message.history_after_sequence ?? 0;
        const rebootstrap = this.#declaredFloor !== null && declared > this.#declaredFloor;
        this.#declaredFloor = declared;
        const floor = rebootstrap
          ? this.#raiseHistoryFloor(declared)
          : this.#lowerHistoryFloor(declared);
        const apply = rebootstrap ? applyEventRebootstrap : applyEventBatch;
        this.#setState(
          apply(
            this.#state,
            message.events,
            message.active_executions,
            message.through_sequence,
            floor,
          ),
        );
        this.#recordSpine(message.events, declared);
      }
      this.#refreshExperimentsFor(message.events);
      this.#refreshPaneFor(message.events);
    }
    if (message.type === 'protocol_error') {
      this.#streamProtocolError = true;
      this.#setState(
        reportError(markEventStreamUnavailable(this.#state), message.message, {
          scope: 'protocol',
          diagnostic: message.diagnostic ?? null,
        }),
      );
    }
  }

  /**
   * The floor only ever descends.
   *
   * A subscription reports the floor it bootstrapped with on every batch it
   * sends, including live ones. Once a backfill has lowered the floor, taking
   * a later batch's value literally would raise it again and send the client
   * back for history it already holds.
   */
  #lowerHistoryFloor(floor: number): number {
    this.#historyFloor = Math.min(this.#historyFloor, floor);
    return this.#historyFloor;
  }

  /**
   * Adopts a floor the stream raised, which only a re-bootstrap does.
   *
   * The run's durable event log is attached after the client subscribes, so a
   * subscription that bootstrapped against the server's own short log is
   * re-bootstrapped at a tail of the run log. Everything below that tail is
   * unread history, whatever the client held before, and the spine set
   * described a log this one replaces.
   */
  #raiseHistoryFloor(floor: number): number {
    this.#historyFloor = floor;
    this.#foldedBelowFloor.clear();
    return floor;
  }

  /** Remembers the events a batch delivered from below its own history floor. */
  #recordSpine(events: readonly RunEvent[], historyAfterSequence: number): void {
    if (historyAfterSequence === 0) return;
    for (const {sequence} of events) {
      // An unsequenced event cannot be recognized in a later chunk anyway.
      if (sequence !== undefined && sequence <= historyAfterSequence) {
        this.#foldedBelowFloor.add(sequence);
      }
    }
  }

  /**
   * The backend publishes this semantic event after the canonical project is
   * attached and whenever persisted hypothesis state changes. The client does
   * not infer storage readiness or mutations from execution phases.
   */
  #refreshExperimentsFor(events: readonly RunEvent[]): void {
    if (this.#state.experimentLog === null) return;
    const relevant = events.some(event => event.type === 'experiments_changed');
    if (!relevant) return;
    if (this.#experimentFetch !== null) this.#experimentRefreshPending = true;
    void this.#loadExperiments();
  }

  #setState(state: SessionState): void {
    // Every state change goes through here, which makes it the one place that
    // can guarantee focus still names a column that exists. A pane that closes
    // while it holds the keys would otherwise swallow every keystroke.
    this.#state = normalizeFocus(state);
    for (const listener of this.#listeners) listener(this.#state);
  }
}

function reportCaughtError(
  state: SessionState,
  error: unknown,
  scope: 'request' | 'transport',
): SessionState {
  return reportError(state, errorMessage(error), {
    scope,
    diagnostic: error instanceof ServerError ? error.diagnostic : null,
  });
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Proves a switch over a discriminated union handles every case at compile time. */
function assertNever(value: never): never {
  throw new Error(`Unhandled command action: ${JSON.stringify(value)}`);
}

function renderResponse(
  request: RequestInput,
  response: ProtocolResponse,
  responseView?: 'perf',
): string | null {
  if (response.ack) return `${response.ack.action}: ${response.ack.status}`;
  if (request.type === 'query.performance' || responseView === 'perf') {
    return renderPerformanceCurve(
      response.performance ?? [],
      response.events ?? [],
      response.performance_context,
    );
  }
  return null;
}
