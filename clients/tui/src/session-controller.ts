import {
  type ExperimentCursor,
  isServerRejection,
  type ProtocolResponse,
  type RunEvent,
  type RunSnapshot,
  ServerError,
} from '@vibesys/contracts';
import {
  hasRunEnded,
  type IterationNavigationState,
  initialIterationNavigation,
  reduceIterationResult,
} from '@vibesys/run-state';
import {recordsBenchmark} from '@vibesys/run-state/performance';
import {DEFAULT_CHAT_THREAD_ID} from '@vibesys/run-state/transcript';
import {
  closeStreamAndTransport,
  DEFAULT_RECONNECT_DELAYS_MS,
  PersistentEventStream,
  type PersistentStreamMessage,
  type ScheduleTimeout,
  type ServerTransport,
  type StreamConnectionState,
  type StreamMessageContext,
  StreamReconciler,
} from '@vibesys/transport';
import type {StartupTrace} from './boot-trace.js';
import {
  chatMenuCustomModel,
  closeChatMenu,
  failChatMenu,
  moveChatMenuSelection,
  openChatModelMenu,
  openChatResumeMenu,
  selectedChatMenuRow,
  setChatMenuCustomModel,
  setChatModelMenuOptions,
} from './chat-menu.js';
import {
  type CommandSurface,
  chatHelpText,
  fuzzyMatchCommands,
  type ParsedCommand,
  parseCommand,
} from './commands.js';
import {DesignQueryOwner} from './design-query-owner.js';
import {
  applyDiffPatch,
  closeDiffViewer,
  currentDiffFile,
  type DiffPatchKey,
  type DiffRoundRange,
  diffRangeExplanation,
  diffRoundRange,
  failDiffPatch,
  markDiffPatchLoading,
  moveDiffFile,
  moveDiffHunk,
  openDiffViewer,
} from './diff-viewer.js';
import {
  enterExperimentDrilldown,
  enterExperimentRound,
  enterUnownedExperimentRound,
  failExperiments,
  leaveExperimentDrilldown,
  leaveHypothesisDetail,
  mergeExperimentEntries,
  moveExperimentSelection,
  moveHypothesisRoundSelection,
  openExperimentLog,
  openHypothesisDetail,
  selectExperimentActivity,
  setDesignLog,
  setExperiments,
} from './experiments.js';
import {LatestQueryOwner} from './latest-query-owner.js';
import {type NoteIdentity, readNote, writeNote} from './notes-store.js';
import {
  activeCommandSurface,
  closePalette,
  movePaletteSelection,
  openPalette,
  setPaletteQuery,
} from './palette-model.js';
import {renderCommandResponse} from './response-renderer.js';
import {
  clearAgentSelection,
  selectAgent,
  selectIteration,
  selectNextAgent,
  selectNextRound,
  selectPreviousAgent,
  selectPreviousRound,
  selectRound,
} from './round-agent-selection.js';
import {
  activeChatThreadSettings,
  applyEvent,
  applyEventBatch,
  applyEventPrefix,
  applyEventRebootstrap,
  applyResponseEvents,
  applySnapshot,
  type ChatThreadSettings,
  chatDocked,
  chatPaneVisible,
  clearEntrySelection,
  clearInputError,
  closeNotepad,
  closeNotepadAfterPromotion,
  closeOverlays,
  closePane,
  cyclePaneFocus,
  dismissErrorBanner,
  failPane,
  focusPane,
  focusRound,
  hydrateNotepad,
  initialSessionState,
  markEventStreamAvailable,
  markEventStreamUnavailable,
  markNotepadConflict,
  normalizeFocus,
  notepadPromotionText,
  openChat,
  openNotepad,
  openPane,
  type PaneFocus,
  type PaneView,
  type RoundFocus,
  reportError,
  type SessionState,
  selectNextEntry,
  selectNextTodo,
  setChatDockFits,
  setChatThreadPending,
  setDesignPane,
  setNotepadPersistenceError,
  setNotepadText,
  setPerformancePane,
  showDetail,
  showLive,
  switchChatThread,
  togglePaneZoom,
  toggleTodos,
  updateChatConversation,
} from './session-model.js';
import {DEFAULT_THEME_NAME, type ThemeName} from './theme.js';
import {
  closeThemePicker,
  moveThemeSelection,
  openThemePicker,
  setTheme,
} from './theme-picker-model.js';

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
  selectIteration(iterationId: string): void;
  selectRound(roundNumber: number): void;
  selectAgent(kind: string): void;
  selectNextEntry(delta: number, id?: string): void;
  clearEntrySelection(): void;
  clearAgentSelection(): void;
  focusRound(focus: RoundFocus): void;
  selectNextTodo(delta: number): void;
  toggleTodos(): void;
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
  clearInputError(): void;
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
  /**
   * `d`: the diff viewer on one round's recorded commit range. Without a
   * round it opens the drill-down's selected round, or the newest round the
   * design log can diff.
   */
  openRoundDiff(roundNumber?: number): void;
  closeDiffViewer(): void;
  /** `←`/`→` in the diff viewer: the previous/next changed file. */
  moveDiffFile(delta: number): void;
  /** `↑`/`↓` in the diff viewer: the previous/next hunk of the patch. */
  moveDiffHunk(delta: number): void;
  openThemePicker(): void;
  moveThemeSelection(delta: number): void;
  applySelectedTheme(): void;
  closeThemePicker(): void;
  /** Opens the command palette, scoped to whichever composer currently holds the keys. */
  openPalette(): void;
  closePalette(): void;
  typePaletteQuery(text: string): void;
  backspacePaletteQuery(): void;
  movePaletteSelection(delta: number): void;
  /**
   * Enter in the palette. A no-argument command runs immediately and this
   * returns null; a command that takes an argument returns the text and the
   * surface for the caller to drop into that composer, rather than guessing
   * blind at what the operator meant.
   */
  executePaletteSelection(): {text: string; surface: CommandSurface} | null;
  /** Opens the private notepad, hydrated from disk if this run already has a saved note. */
  openNotepad(): void;
  closeNotepad(): void;
  /** Every keystroke in the notepad's editor; persists the note as it types. */
  setNoteText(text: string): void;
  /**
   * Closes the notepad and returns its text for the caller to drop, unsent,
   * into the command bar's `/steer` draft. Returns null for an empty note. An
   * ordinary empty note stays open; this action closes an empty conflicted
   * draft so the preserved saved note becomes accessible again.
   */
  promoteNoteToSteerDraft(): {text: string} | null;
  /** As `promoteNoteToSteerDraft`, but a nonblank result opens and fills the chat draft. */
  promoteNoteToChatDraft(): {text: string} | null;
  /** Loads the chunk of history just older than what is folded. Resolves false when history is already complete. */
  loadOlderHistory(): Promise<boolean>;
  subscribe(listener: (state: SessionState) => void): () => void;
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

type IterationQueryOutcome =
  | {readonly kind: 'unsupported'}
  | {readonly kind: 'store-replaced'}
  | {
      readonly kind: 'supported';
      readonly storeId: string;
      readonly navigation: IterationNavigationState | null;
    };

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
  /** Last server projection applied completely to the local experiment log. */
  #experimentCursor: ExperimentCursor | null = null;
  /** Highest semantic invalidation observed while connected. */
  #experimentTarget: Pick<ExperimentCursor, 'run_id' | 'revision'> | null = null;
  #experimentForceRefresh = false;
  /** Changes whenever an unknown invalidation makes an in-flight answer stale. */
  #experimentRefreshGeneration = 0;
  #streamConnected = false;
  /**
   * When the client first asked for experiments, and whether the answer has
   * been timed yet. The first request can be answered `experiments_ready:
   * false`, so the elapsed time spans every retry until entries actually land,
   * which is exactly how long the landing view shows "Loading experiments...".
   */
  #experimentsRequestedAt: number | null = null;
  #experimentsLoadTraced = false;
  #paneFetch: Promise<void> | null = null;
  /** The view to fetch once the in-flight pane query settles; see `#loadPane`. */
  #paneRefreshWanted: PaneView | null = null;
  /** Owns both design consumers, retaining one follow-up without delaying boot. */
  readonly #designQuery = new DesignQueryOwner({
    request: async () => {
      const response = await this.client.request({type: 'query.design'});
      return {rounds: response.design ?? [], available: response.design_ready !== false};
    },
    publish: ({rounds, available}) => {
      const withLog = available ? setDesignLog(this.#state, rounds) : this.#state;
      this.#setState(setDesignPane(withLog, rounds, available));
    },
    report: error => {
      if (this.#state.layout.right?.view !== 'design') return;
      const message = errorMessage(error);
      this.#setState(reportCaughtError(failPane(this.#state, 'design', message), error, 'request'));
    },
  });
  #iterationCapability: 'unknown' | 'supported' | 'unsupported' = 'unknown';
  /** Owns the optional policy-navigation capability probe and latest refresh. */
  readonly #iterationQuery = new LatestQueryOwner<IterationQueryOutcome>({
    request: () => this.#requestIterations(),
    publish: outcome => {
      if (outcome.kind === 'store-replaced') return;
      this.#iterationCapability = outcome.kind;
      if (outcome.kind !== 'supported' || outcome.navigation === null) return;
      if (outcome.storeId !== this.#reconciler.storeId()) {
        void this.#loadIterations();
        return;
      }
      this.#setState({...this.#state, iterations: outcome.navigation});
    },
    report: error => this.#reportIterationError(error),
  });
  readonly #reconciler = new StreamReconciler({backfillChunk: BACKFILL_CHUNK});
  /** Serializes the consumer effect of applying one shared backfill outcome. */
  #historyFetch: Promise<boolean> | null = null;
  #streamProtocolError = false;
  #chatThreadCreateGeneration = 0;
  /** Opaque server-owned namespace for project-local client state. */
  #noteProjectScope: string | null = null;
  /**
   * The newest accepted snapshot's durable tail, scoped to its run. A snapshot
   * can update lifecycle without advancing the event cursor; until the stream
   * reaches this sequence, terminal status is not evidence that the replay is
   * complete enough to make an ordinary disconnect harmless.
   */
  #snapshotWatermark: {readonly runId: string; readonly sequence: number} | null = null;
  /** Sequence space the current stream fold belongs to; rebootstrap starts another. */
  #streamGeneration = 0;
  /** The one terminal release every stop caller joins. */
  #closing: Promise<void> | null = null;

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
    reconnectDelaysMs: readonly number[] = DEFAULT_RECONNECT_DELAYS_MS,
    scheduleTimeout?: ScheduleTimeout,
  ) {
    this.#state = initialSessionState(themeName);
    this.#stream = new PersistentEventStream(client, {
      tail: BOOTSTRAP_TAIL,
      reconnectDelaysMs,
      ...(scheduleTimeout === undefined ? {} : {scheduleTimeout}),
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
      this.#loadIterations(),
      // The log is the landing view, so it is populated before the first frame
      // rather than on demand.
      this.#loadExperiments(),
      // The stream owns its own dial/redial loop; the controller supplies where
      // to resume from, whether a drop is worth reconnecting, and where the
      // messages and connection changes land.
      this.#stream.subscribe({
        cursor: () => this.#state.core.sequence,
        // The store the folded cursor belongs to, so a resume across an outage
        // can be dropped if the run swapped its durable log while we were gone.
        storeId: () => this.#reconciler.storeId(),
        shouldReconnect: () => this.#shouldReconnectStream(),
        onMessage: (message, context) => this.#onMessage(message, context),
        onConnectionState: state => this.#onConnectionState(state),
      }),
    ]);
    // A quiet close and the snapshot can settle in the same turn while the
    // initial dial still owns the stream's recovery slot. Retry once that slot
    // is released so the later completeness evidence cannot be lost merely
    // because its first attempt raced the handshake's promise continuation.
    if (this.#snapshotIsAhead()) this.#stream.retryIfDisconnected();
    this.#streamConnected = this.#state.eventStreamAvailable;
  }

  /**
   * The event stream changed state. A drop raises the transport banner
   * whatever the run's status: the status says nothing about whether the
   * transcript on screen reaches the run's tail, and a drop before the first
   * batch leaves it empty.
   *
   * A stream the server already failed by `protocol_error` is the exception,
   * and only because it has already said more. That path names the failure and
   * marks the stream unavailable, and the close that follows is its
   * consequence rather than a second fault, so overwriting it would trade a
   * specific diagnostic for `Connection lost`. Reachable rather than
   * hypothetical: the gateway sends `stream_failed` before `subscribed` when
   * the bootstrap raises, so the close lands with no batch folded.
   */
  #onConnectionState(state: StreamConnectionState): void {
    if (state.status === 'connected') {
      const reconnected = this.#streamConnected;
      this.#streamConnected = true;
      this.#setState(markEventStreamAvailable(this.#state));
      if (reconnected && this.#state.experimentLog !== null) {
        this.#experimentRefreshGeneration += 1;
        this.#experimentForceRefresh = true;
        if (this.#experimentFetch !== null) this.#experimentRefreshPending = true;
        void this.#loadExperiments();
        void this.#loadIterations();
      }
    } else if (!this.#streamProtocolError) {
      this.#setState(
        reportCaughtError(markEventStreamUnavailable(this.#state), state.error, 'transport'),
      );
    }
  }

  async #loadSnapshot(): Promise<void> {
    const streamGeneration = this.#streamGeneration;
    try {
      const response = await this.client.request({type: 'query.snapshot'});
      if (response.snapshot) {
        const next = applySnapshot(this.#state, response.snapshot);
        if (next.core.runId === response.snapshot.run_id) {
          this.#noteProjectScope = response.snapshot.project_scope ?? null;
        }
        this.#rememberSnapshotWatermark(response.snapshot, next, streamGeneration);
        this.#setState(next);
        this.#reconcileNotepadIdentity();
        // The stream may have closed quietly before this concurrent answer
        // established that its fold was incomplete. The conditional retry is
        // inert while healthy and promotes that prior close when it is not.
        if (this.#snapshotIsAhead()) this.#stream.retryIfDisconnected();
      }
    } catch (error) {
      this.#setState(reportCaughtError(this.#state, error, 'request'));
    }
  }

  /** Redial live runs, and terminal runs whose snapshot proves their fold is incomplete. */
  #shouldReconnectStream(): boolean {
    if (this.#streamProtocolError) return false;
    if (!hasRunEnded(this.#state.core)) return true;
    return this.#snapshotIsAhead();
  }

  #snapshotIsAhead(): boolean {
    const watermark = this.#snapshotWatermark;
    return (
      watermark !== null &&
      watermark.runId === this.#state.core.runId &&
      this.#state.core.sequence < watermark.sequence
    );
  }

  /** Retain only a snapshot accepted for the run this controller currently projects. */
  #rememberSnapshotWatermark(
    snapshot: RunSnapshot,
    next: SessionState,
    streamGeneration: number,
  ): void {
    // A rebootstrap while the request was in flight replaced the sequence
    // space the response could have described. Its lifecycle projection may
    // still be useful, but its numeric cursor cannot bound the new store.
    if (streamGeneration !== this.#streamGeneration) return;
    if (next.core.runId !== snapshot.run_id) return;
    const current = this.#snapshotWatermark;
    if (
      current === null ||
      current.runId !== snapshot.run_id ||
      snapshot.sequence > current.sequence
    ) {
      this.#snapshotWatermark = {runId: snapshot.run_id, sequence: snapshot.sequence};
    }
  }

  #loadIterations(): Promise<void> {
    if (this.#iterationCapability === 'unsupported') return Promise.resolve();
    return this.#iterationQuery.refresh();
  }

  async #requestIterations(): Promise<IterationQueryOutcome> {
    const expectedStoreId = this.#reconciler.storeId();
    let response: ProtocolResponse;
    try {
      response = await this.client.request({
        type: 'query.iterations',
        expected_store_id: expectedStoreId,
      });
    } catch (error) {
      if (isIterationStoreReplaced(error)) return {kind: 'store-replaced'};
      if (this.#iterationCapability === 'unknown' && isServerRejection(error)) {
        return {kind: 'unsupported'};
      }
      throw error;
    }
    try {
      return {
        kind: 'supported',
        storeId: expectedStoreId,
        navigation: await this.#collectIterationPages(response, expectedStoreId),
      };
    } catch (error) {
      if (isIterationStoreReplaced(error)) return {kind: 'store-replaced'};
      throw error;
    }
  }

  async #collectIterationPages(
    response: ProtocolResponse,
    expectedStoreId: string,
  ): Promise<IterationNavigationState | null> {
    const seen = new Set<string>();
    let staged = initialIterationNavigation();
    while (response.iterations_ready !== false && response.iterations != null) {
      staged = reduceIterationResult(staged, response.iterations);
      if (!('items' in response.iterations)) return staged;
      const cursor = response.iterations.next_cursor ?? null;
      if (cursor === null) return staged;
      if (seen.has(cursor)) throw new Error('iteration query repeated a paging cursor');
      seen.add(cursor);
      response = await this.client.request({
        type: 'query.iterations',
        expected_store_id: expectedStoreId,
        cursor,
      });
    }
    return null;
  }

  #reportIterationError(error: unknown): void {
    this.#setState(reportCaughtError(this.#state, error, 'request'));
  }

  /** Loads the chunk of history just older than what is folded. */
  loadOlderHistory(): Promise<boolean> {
    if (this.#historyFetch !== null) return this.#historyFetch;
    const running = this.#requestOlderHistory().finally(() => {
      this.#historyFetch = null;
    });
    this.#historyFetch = running;
    return running;
  }

  async #requestOlderHistory(): Promise<boolean> {
    try {
      const outcome = await this.#reconciler.backfill(
        async ({expectedStoreId, afterSequence, beforeSequence}) => {
          try {
            const response = await this.client.request({
              type: 'query.events',
              ...(expectedStoreId === undefined ? {} : {expected_store_id: expectedStoreId}),
              after_sequence: afterSequence,
              before_sequence: beforeSequence,
            });
            return {kind: 'events', events: response.events ?? []};
          } catch (error) {
            if (isEventsStoreReplaced(error)) return {kind: 'store-replaced'};
            throw error;
          }
        },
      );
      if (outcome.kind !== 'prepend') return false;
      const next = applyEventPrefix(this.#state, outcome.events, outcome.historyFloor);
      this.#setState(next);
      if (next.core.historyAfterSequence !== outcome.historyFloor) return false;
      return outcome.accept();
    } catch (error) {
      this.#setState(reportCaughtError(this.#state, error, 'request'));
      return false;
    }
  }

  stop(): Promise<void> {
    if (this.#closing !== null) return this.#closing;
    this.#designQuery.stop();
    this.#iterationQuery.stop();
    this.#chatThreadCreateGeneration += 1;
    const closing = closeStreamAndTransport(this.#stream, this.client);
    this.#closing = closing;
    return closing;
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

  selectIteration(iterationId: string): void {
    this.#setState(selectIteration(this.#state, iterationId));
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

  openPalette(): void {
    this.#setState(openPalette(this.#state));
  }

  closePalette(): void {
    this.#setState(closePalette(this.#state));
  }

  typePaletteQuery(text: string): void {
    const palette = this.#state.palette;
    if (palette === null) return;
    this.#setState(setPaletteQuery(this.#state, palette.query + text));
  }

  backspacePaletteQuery(): void {
    const palette = this.#state.palette;
    if (palette === null) return;
    this.#setState(setPaletteQuery(this.#state, palette.query.slice(0, -1)));
  }

  movePaletteSelection(delta: number): void {
    const palette = this.#state.palette;
    if (palette === null) return;
    const matchCount = fuzzyMatchCommands(palette.query, this.#paletteContext()).length;
    this.#setState(movePaletteSelection(this.#state, delta, matchCount));
  }

  executePaletteSelection(): {text: string; surface: CommandSurface} | null {
    const palette = this.#state.palette;
    if (palette === null) return null;
    const context = this.#paletteContext();
    const command = fuzzyMatchCommands(palette.query, context)[palette.selected];
    this.#setState(closePalette(this.#state));
    if (command === undefined) return null;
    if (command.args === 'none') {
      void this.#dispatchCommand(parseCommand(command.name, context));
      return null;
    }
    return {text: `${command.name} `, surface: context.surface};
  }

  #paletteContext(): {surface: CommandSurface; chatDocked: boolean} {
    return {surface: activeCommandSurface(this.#state), chatDocked: chatPaneVisible(this.#state)};
  }

  /**
   * Opens the notepad, hydrated from `notes-store.ts` once its run/project
   * identity is known. A conflicted startup draft stays in memory across
   * closes instead of being replaced by the older durable note.
   */
  openNotepad(): void {
    if (this.#state.notepad.persistence === 'conflict') {
      this.#setState(openNotepad(this.#state));
      return;
    }
    const identity = this.#noteIdentity();
    const hydrated =
      identity === null ? this.#state : hydrateNotepad(this.#state, readNote(identity));
    this.#setState(openNotepad(hydrated));
  }

  closeNotepad(): void {
    this.#setState(closeNotepad(this.#state));
  }

  /**
   * Persists on every keystroke rather than only on close: an operator who
   * quits the TUI outright (not just `Esc`) should not lose a note, and a
   * sidecar write is cheap enough that debouncing it is not worth the extra
   * state. Nothing is written for a run the client has no id for yet (early
   * in boot, before the first snapshot lands).
   */
  setNoteText(text: string): void {
    const timestamp = new Date().toISOString();
    const next = setNotepadText(this.#state, text, timestamp);
    const identity = this.#noteIdentity();
    if (
      identity === null ||
      next.notepad.persistence !== 'ready' ||
      next.notepad.createdAt === null
    ) {
      this.#setState(next);
      return;
    }
    try {
      writeNote({
        ...identity,
        text: next.notepad.text,
        createdAt: next.notepad.createdAt,
        updatedAt: next.notepad.updatedAt ?? timestamp,
      });
      this.#setState(setNotepadPersistenceError(next, null));
    } catch (error) {
      // The private note is convenience state, not part of the run. Keep the
      // operator's newest draft in memory and show that it is unsaved; a later
      // successful edit clears this diagnostic. Filesystem failure must never
      // escape the editor callback or put note text on the backend transport.
      this.#setState(setNotepadPersistenceError(next, errorMessage(error)));
    }
  }

  #noteIdentity(): NoteIdentity | null {
    const runId = this.#state.core.runId;
    const projectScope = this.#noteProjectScope;
    return runId === null || projectScope === null ? null : {projectScope, runId};
  }

  /**
   * Bind a startup notepad to the identity learned from the first accepted
   * snapshot. An untouched pad adopts the durable record. A touched pad may
   * claim an empty slot, but it never overwrites a record that predates its
   * identity: that crossing becomes an explicit in-memory conflict instead.
   */
  #reconcileNotepadIdentity(): void {
    if (this.#state.notepad.persistence !== 'pending_identity') return;
    const identity = this.#noteIdentity();
    if (identity === null) return;
    const record = readNote(identity);
    if (this.#state.notepad.createdAt === null) {
      this.#setState(hydrateNotepad(this.#state, record));
      return;
    }
    if (record !== null) {
      this.#setState(markNotepadConflict(this.#state));
      return;
    }
    const {text, createdAt, updatedAt} = this.#state.notepad;
    const bound = hydrateNotepad(this.#state, null);
    try {
      writeNote({
        ...identity,
        text,
        createdAt,
        updatedAt: updatedAt ?? createdAt,
      });
      this.#setState(setNotepadPersistenceError(bound, null));
    } catch (error) {
      this.#setState(setNotepadPersistenceError(bound, errorMessage(error)));
    }
  }

  /**
   * Hands the note's text to the caller and closes the notepad; the caller
   * (`app.ts`) drops it into the command bar's buffer, unsent, the same way
   * the palette's own text-taking commands are pre-filled. Never submits on
   * the operator's behalf: this is the one path a note is allowed to reach an
   * agent by, and it still requires the operator to review and press Enter.
   */
  promoteNoteToSteerDraft(): {text: string} | null {
    const text = notepadPromotionText(this.#state);
    if (text === null) {
      if (this.#state.notepad.persistence === 'conflict') {
        this.#setState(closeNotepadAfterPromotion(this.#state));
      }
      return null;
    }
    this.#setState(closeNotepadAfterPromotion(this.#state));
    return {text};
  }

  /** As `promoteNoteToSteerDraft`, but a nonblank result opens and fills the chat draft. */
  promoteNoteToChatDraft(): {text: string} | null {
    const text = notepadPromotionText(this.#state);
    if (text === null) {
      if (this.#state.notepad.persistence === 'conflict') {
        this.#setState(closeNotepadAfterPromotion(this.#state));
      }
      return null;
    }
    this.#setState(openChat(closeNotepadAfterPromotion(this.#state)));
    return {text};
  }

  closeChat(): void {
    this.#setState({...this.#state, chatOpen: false});
  }

  switchChatThread(threadId: string): void {
    this.#chatThreadCreateGeneration += 1;
    this.#setState(switchChatThread(this.#state, threadId));
  }

  openChatResumeMenu(): void {
    this.#chatThreadCreateGeneration += 1;
    this.#setState(openChatResumeMenu(this.#state));
  }

  async openChatModelMenu(): Promise<void> {
    this.#chatThreadCreateGeneration += 1;
    this.#setState(openChatModelMenu(this.#state));
    const ownedMenu = this.#state.chatMenu;
    try {
      const response = await this.client.request({type: 'query.chat_options'});
      if (this.#state.chatMenu !== ownedMenu) return;
      const options = response.chat_options;
      this.#setState(
        options === null || options === undefined
          ? failChatMenu(this.#state, 'This run has not reported its chat options yet.')
          : setChatModelMenuOptions(this.#state, options),
      );
    } catch (error) {
      if (this.#state.chatMenu !== ownedMenu) return;
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
    this.#chatThreadCreateGeneration += 1;
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
   * Asks the backend for a new thread and switches to it. The backend
   * resolves anything omitted from the run's own settings. The response's
   * replayed events carry the authoritative thread record.
   */
  async #createChatThread(settings: ChatThreadSettings | null): Promise<void> {
    const generation = ++this.#chatThreadCreateGeneration;
    try {
      const response = await this.client.request({
        type: 'query.chat_thread_create',
        ...(settings === null ? {} : {provider: settings.provider, model: settings.model}),
      });
      const state = applyResponseEvents(this.#state, response.events ?? []);
      if (this.#chatThreadCreateGeneration !== generation) {
        if (state !== this.#state) this.#setState(state);
        return;
      }
      const navigated = closeChatMenu(state);
      const threadId = response.chat_thread?.thread_id;
      this.#setState(threadId === undefined ? navigated : switchChatThread(navigated, threadId));
    } catch (error) {
      if (this.#chatThreadCreateGeneration === generation) {
        this.#setState(reportCaughtError(this.#state, error, 'request'));
      }
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

  clearInputError(): void {
    this.#setState(clearInputError(this.#state));
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
   *
   * Single-flight with a remembered want. A request that lands mid-flight
   * cannot ride the in-flight query: a different view's answer is discarded
   * by the model's view guard, and a same-view answer may predate the change
   * that prompted the refresh. The want is one view, latest wins, so a burst
   * of requests during one flight collapses into a single follow-up fetch,
   * like `#experimentRefreshPending`.
   */
  async #loadPane(view: PaneView): Promise<void> {
    if (this.#paneFetch !== null) {
      this.#paneRefreshWanted = view;
      return this.#paneFetch;
    }
    const fetch = this.#requestPane(view).finally(() => {
      this.#paneFetch = null;
      const wanted = this.#paneRefreshWanted;
      this.#paneRefreshWanted = null;
      // A pane closed while the query ran wants nothing anymore.
      if (wanted !== null && this.#state.layout.right?.view === wanted) {
        void this.#loadPane(wanted);
      }
    });
    this.#paneFetch = fetch;
    return fetch;
  }

  async #requestPane(view: PaneView): Promise<void> {
    try {
      switch (view) {
        case 'perf': {
          const response = await this.client.request({type: 'query.performance'});
          this.#setState(
            setPerformancePane(
              this.#state,
              {performance: response.performance, events: response.events ?? []},
              response.performance_context ?? null,
            ),
          );
          return;
        }
        case 'design': {
          await this.#designQuery.refresh(true);
          return;
        }
        default: {
          const unreachable: never = view;
          throw new Error(`Unknown pane view: ${String(unreachable)}`);
        }
      }
    } catch (error) {
      const message = errorMessage(error);
      this.#setState(reportCaughtError(failPane(this.#state, view, message), error, 'request'));
    }
  }

  /**
   * Both visualizations refresh only when core-state says an event records a
   * benchmark. The controller does not carry a second list of event spellings.
   */
  #refreshPaneFor(events: readonly RunEvent[]): void {
    const right = this.#state.layout.right;
    if (right === null) return;
    const relevant = events.some(recordsBenchmark);
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

  openRoundDiff(roundNumber?: number): void {
    const resolved = this.#resolveDiffRange(roundNumber);
    if (typeof resolved === 'string') {
      // Nothing to diff is an answer, not an error: the explanation goes in
      // the same detail overlay a command's answer would.
      this.#setState(showDetail(this.#state, resolved));
      return;
    }
    this.#setState(openDiffViewer(this.#state, resolved));
    void this.#ensureDiffPatch();
  }

  closeDiffViewer(): void {
    this.#setState(closeDiffViewer(this.#state));
  }

  moveDiffFile(delta: number): void {
    this.#setState(moveDiffFile(this.#state, delta));
    void this.#ensureDiffPatch();
  }

  moveDiffHunk(delta: number): void {
    this.#setState(moveDiffHunk(this.#state, delta));
  }

  /**
   * The round the viewer opens: the named round, else the drill-down's
   * selected round, else the newest round the design log can diff. Returns
   * the explanation to show instead when no range can be diffed.
   */
  #resolveDiffRange(roundNumber: number | undefined): DiffRoundRange | string {
    const rounds = this.#state.designLog;
    if (rounds === null || rounds.length === 0) {
      return 'No design rounds have loaded yet, so there is no diff to open.';
    }
    const selected = roundNumber ?? this.#state.hypothesisDetail?.selectedRound ?? null;
    if (selected !== null) {
      const round = rounds.find(candidate => candidate.round === selected);
      if (round === undefined) return `Round ${selected} has no recorded design changes.`;
      return diffRoundRange(round) ?? diffRangeExplanation(round);
    }
    for (let index = rounds.length - 1; index >= 0; index -= 1) {
      const round = rounds[index];
      if (round === undefined) continue;
      const range = diffRoundRange(round);
      if (range !== null) return range;
    }
    return 'No recorded round has a commit range to diff.';
  }

  /**
   * Fetches the patch for the file the viewer is on, at most once per file:
   * the slot `markDiffPatchLoading` claims doubles as the in-flight guard, so
   * revisiting a file rerenders what it already holds and only an unvisited
   * file costs a query. A failure lands in the slot as an explanation rather
   * than the error banner: the viewer is modal, so its own body is where the
   * operator is looking.
   */
  async #ensureDiffPatch(): Promise<void> {
    const viewer = this.#state.diffViewer;
    const file = viewer === null ? null : currentDiffFile(viewer);
    if (viewer === null || file === null || viewer.patches[file.path] !== undefined) return;
    const key: DiffPatchKey = {base: viewer.base, head: viewer.head, path: file.path};
    this.#setState(markDiffPatchLoading(this.#state, file.path));
    try {
      const response = await this.client.request({type: 'query.design_patch', ...key});
      const patch = response.design_patch ?? null;
      this.#setState(
        patch === null
          ? failDiffPatch(this.#state, key, 'The run has not attached yet.')
          : applyDiffPatch(this.#state, patch),
      );
    } catch (error) {
      this.#setState(failDiffPatch(this.#state, key, errorMessage(error)));
    }
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
    const generation = this.#experimentRefreshGeneration;
    try {
      const response = await this.client.request({
        type: 'query.experiments',
        ...(this.#experimentCursor === null ? {} : {after: this.#experimentCursor}),
      });
      if (response.experiments_ready === false) return;
      const entries = response.experiments ?? [];
      const update = response.experiment_update;
      if (this.#experimentResponseStale(generation, update)) {
        this.#experimentRefreshPending = true;
        return;
      }
      if (update === undefined || update === null) {
        this.#experimentCursor = null;
        this.#experimentTarget = null;
        this.#experimentForceRefresh = false;
        this.#setState(setExperiments(this.#state, entries));
      } else if (update.reset) {
        this.#experimentCursor = {
          run_id: update.run_id,
          projection_id: update.projection_id,
          revision: update.through_revision,
        };
        this.#clearSatisfiedExperimentTarget();
        this.#setState(setExperiments(this.#state, entries));
      } else if (
        this.#experimentCursor === null ||
        this.#experimentCursor.run_id !== update.run_id ||
        this.#experimentCursor.projection_id !== update.projection_id ||
        this.#experimentCursor.revision !== update.from_revision
      ) {
        // Never apply a delta to an unproven base. The queued cursor-free
        // request deterministically recovers with a full server snapshot.
        this.#experimentCursor = null;
        this.#experimentForceRefresh = true;
        this.#experimentRefreshPending = true;
        return;
      } else {
        this.#experimentCursor = {
          run_id: update.run_id,
          projection_id: update.projection_id,
          revision: update.through_revision,
        };
        this.#clearSatisfiedExperimentTarget();
        const merged = mergeExperimentEntries(
          this.#state.experimentLog?.entries ?? [],
          entries,
          update.removed_hypothesis_ids ?? [],
        );
        this.#setState(setExperiments(this.#state, merged));
      }
      this.#traceExperimentsLoaded(this.#state.experimentLog?.entries.length ?? entries.length);
    } catch (error) {
      const message = errorMessage(error);
      this.#setState(reportCaughtError(failExperiments(this.#state, message), error, 'request'));
    }
    void this.#designQuery.refresh();
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
        // The palette is the one /help surface, opened over whichever
        // composer is focused; it lists exactly what this surface offers.
        this.openPalette();
        return Promise.resolve();
      case 'unknown':
        // Any other unrecognized slash input answers with the chat's own
        // help rather than a global "unknown command" error.
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
          labelKind: 'chat_commands',
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
          {id, kind: 'user', labelKind: queued ? 'user_queued' : 'user', content: text},
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
              entries.map(entry =>
                messageIds.has(entry.id) ? {...entry, labelKind: 'user'} : entry,
              ),
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
      let state = applyResponseEvents(this.#state, response.events ?? []);
      if (!(response.events ?? []).some(event => event.data?.kind === 'chat')) {
        state = updateChatConversation(state, threadId, entries => [
          ...entries,
          {
            id: `chat-answer-${++this.#chatMessageId}`,
            kind: 'assistant',
            labelKind: 'answer',
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
              labelKind: 'chat_failed',
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
          reportError(this.#state, `Unknown command ${action.text}: try /help for the list.`, {
            scope: 'input',
          }),
        );
      case 'error':
        return this.#setState(reportError(this.#state, action.error, {scope: 'input'}));
      case 'help':
        this.openPalette();
        return;
      case 'note':
        this.openNotepad();
        return;
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
          const rendered = renderCommandResponse(action.request, response, action.responseView);
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

  #onMessage(message: PersistentStreamMessage, context: StreamMessageContext): void {
    const dispatched = this.#dispatchMessage(message, context);
    if (dispatched === null) return;
    const {events, iterationStoreReplaced} = dispatched;
    this.#refreshExperimentsFor(events);
    if (iterationStoreReplaced || events.some(event => event.type === 'iterations_changed')) {
      void this.#loadIterations();
    }
    this.#refreshPaneFor(events);
  }

  #dispatchMessage(
    message: PersistentStreamMessage,
    context: StreamMessageContext,
  ): {readonly events: readonly RunEvent[]; readonly iterationStoreReplaced: boolean} | null {
    switch (message.type) {
      case 'event':
        this.#setState(applyEvent(this.#state, message.event));
        return {events: [message.event], iterationStoreReplaced: false};
      case 'event_batch': {
        const previousIterationStore = this.#reconciler.storeId();
        const reconciliation = this.#reconciler.reconcileBatch(message, context);
        if (reconciliation.kind === 'rebootstrap') {
          this.#streamGeneration += 1;
          this.#snapshotWatermark = null;
        }
        this.#setState(
          (reconciliation.kind === 'rebootstrap' ? applyEventRebootstrap : applyEventBatch)(
            this.#state,
            message.events,
            message.active_executions,
            message.through_sequence,
            reconciliation.historyFloor,
            context.activeExecutionCheckpoint,
          ),
        );
        return {
          events: message.events,
          iterationStoreReplaced:
            reconciliation.kind === 'rebootstrap' ||
            previousIterationStore !== this.#reconciler.storeId(),
        };
      }
      case 'protocol_error':
        this.#streamProtocolError = true;
        this.#setState(
          reportError(markEventStreamUnavailable(this.#state), message.message, {
            scope: 'protocol',
            diagnostic: message.diagnostic ?? null,
          }),
        );
        return null;
      case 'subscribed':
        return null;
      default:
        return assertNever(message);
    }
  }

  /**
   * The backend publishes this semantic event after the canonical project is
   * attached and whenever persisted hypothesis state changes. The client does
   * not infer storage readiness or mutations from execution phases.
   */
  #refreshExperimentsFor(events: readonly RunEvent[]): void {
    if (this.#state.experimentLog === null) return;
    let relevant = false;
    for (const event of events) {
      if (event.type !== 'experiments_changed') continue;
      relevant = true;
      this.#noteExperimentsChanged(event);
    }
    if (!relevant || !this.#experimentRefreshNeeded()) return;
    if (this.#experimentFetch !== null) this.#experimentRefreshPending = true;
    void this.#loadExperiments();
  }

  /** True when a response was fetched for an older refresh or for another run's cursor. */
  #experimentResponseStale(
    generation: number,
    update: {run_id: string} | null | undefined,
  ): boolean {
    return (
      generation !== this.#experimentRefreshGeneration ||
      (update !== undefined &&
        update !== null &&
        this.#experimentTarget !== null &&
        this.#experimentTarget.run_id !== update.run_id)
    );
  }

  #noteExperimentsChanged(event: RunEvent): void {
    const revision = event.data?.kind === 'experiments_changed' ? event.data.revision : null;
    const runId = event.run_id ?? this.#experimentCursor?.run_id;
    if (revision === undefined || revision === null || runId === undefined) {
      this.#experimentRefreshGeneration += 1;
      this.#experimentForceRefresh = true;
    } else if (
      this.#experimentTarget === null ||
      this.#experimentTarget.run_id !== runId ||
      revision > this.#experimentTarget.revision
    ) {
      this.#experimentTarget = {run_id: runId, revision};
    }
  }

  #experimentRefreshNeeded(): boolean {
    if (this.#experimentForceRefresh) return true;
    if (this.#experimentTarget === null || this.#experimentCursor === null) return true;
    return (
      this.#experimentTarget.run_id !== this.#experimentCursor.run_id ||
      this.#experimentTarget.revision > this.#experimentCursor.revision
    );
  }

  #clearSatisfiedExperimentTarget(): void {
    const cursor = this.#experimentCursor;
    const target = this.#experimentTarget;
    if (
      cursor !== null &&
      target !== null &&
      cursor.run_id === target.run_id &&
      cursor.revision >= target.revision
    ) {
      this.#experimentTarget = null;
    }
    this.#experimentForceRefresh = false;
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

function isIterationStoreReplaced(error: unknown): error is ServerError {
  return error instanceof ServerError && error.diagnostic?.code === 'iteration_store_replaced';
}

function isEventsStoreReplaced(error: unknown): error is ServerError {
  return error instanceof ServerError && error.diagnostic?.code === 'events_store_replaced';
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Proves a switch over a discriminated union handles every case at compile time. */
function assertNever(value: never): never {
  throw new Error(`Unhandled command action: ${JSON.stringify(value)}`);
}
