import {
  BrowserBackendClient,
  type HypothesisEntry,
  type HypothesisRound,
} from '@vibesys/backend-client/browser';
import {hasRunEnded, type TranscriptEntry} from '@vibesys/core-state';
import {
  type FormEvent,
  type ReactNode,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import {type QueryState, WorkspaceSession, type WorkspaceState} from './session.js';

type View = 'activity' | 'performance' | 'evidence';
const number = new Intl.NumberFormat(undefined, {maximumFractionDigits: 3});
const label = (value: string): string => value.replaceAll('_', ' ');
const metric = (value: number | null | undefined, unit?: string | null): string =>
  value == null ? 'Not recorded' : `${number.format(value)}${unit ? ` ${unit}` : ''}`;

export function App({session: providedSession}: {session?: WorkspaceSession}) {
  const [session] = useState(
    () => providedSession ?? new WorkspaceSession(new BrowserBackendClient()),
  );
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot, session.getSnapshot);
  const [view, setView] = useState<View>('activity');
  const [hypothesisId, setHypothesisId] = useState<string | null>(null);
  const [roundNumber, setRoundNumber] = useState<number | null>(null);
  const [navigationOpen, setNavigationOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const experiments = state.queries.experiments.response?.experiments ?? [];
  const hypothesis = experiments.find(entry => entry.hypothesis_id === hypothesisId);
  const round = experiments
    .flatMap(entry => entry.rounds ?? [])
    .find(entry => entry.round === roundNumber);
  const context = state.queries.performance.response?.performance_context;
  const executions = Object.values(state.core.activeExecutions);
  const currentAction = executions[0];
  const controlsEnabled = state.connection === 'connected' && !state.command.sending;
  const ack = state.command.ack;
  const pausePending = ack?.action === 'pause' && state.core.status === 'running';
  const resumePending = ack?.action === 'resume' && state.core.status === 'paused';

  useEffect(() => {
    void session.start();
    return () => {
      void session.close();
    };
  }, [session]);

  useEffect(() => {
    setHypothesisId(null);
    setRoundNumber(null);
  }, [state.runId]);

  function selectHypothesis(id: string | null) {
    setHypothesisId(id);
    setRoundNumber(null);
    setNavigationOpen(false);
    if (navigationOpen) document.getElementById('main-content')?.focus();
  }

  return (
    <div className="workspace">
      <a className="skip-link" href="#main-content">
        Skip to workspace
      </a>
      <header className="topbar">
        <a className="brand" href="/" aria-label="VibeSys workspace">
          <svg aria-hidden="true" width="28" height="28" viewBox="0 0 28 28">
            <rect width="28" height="28" rx="7" fill="currentColor" />
            <path
              d="m6 8 5 12 3-7 3 7 5-12"
              fill="none"
              stroke="white"
              strokeWidth="2"
              strokeLinejoin="round"
            />
          </svg>
          VibeSys <span className="brand-divider">/</span>
          <span className="brand-section">Workspace</span>
        </a>
        <div className="topbar-right">
          <span className={`connection ${state.connection}`} role="status">
            <span className="status-dot" />
            {connectionLabel(state.connection)}
          </span>
          <button
            className="button quiet"
            type="button"
            onClick={() => {
              void session.refresh();
            }}
          >
            Refresh data
          </button>
        </div>
      </header>
      <section className="run-header" aria-labelledby="run-title">
        <div className="run-heading">
          <div className="eyebrow">
            RUN WORKSPACE{' '}
            <span className="run-id" title={state.runId ?? undefined}>
              {state.runId ?? 'Awaiting backend'}
            </span>
          </div>
          <h1 id="run-title">{context?.objective_description || 'Optimization run'}</h1>
          <div className="run-subtitle">
            <Badge tone={statusTone(state.core.status)}>{label(state.core.status)}</Badge>
            {state.core.roundLabel && <span>{state.core.roundLabel}</span>}
            {state.core.outerLoop && <span>{state.core.outerLoop} loop</span>}
            {state.core.maxRounds !== null && (
              <span>
                {state.core.rounds.length} / {state.core.maxRounds} rounds
              </span>
            )}
          </div>
        </div>
        <div className="run-actions">
          <button
            className="button"
            type="button"
            disabled={!controlsEnabled || state.core.status !== 'running' || pausePending}
            onClick={() => {
              void session.command({type: 'command.pause', mode: 'after_current_agent_call'});
            }}
          >
            <span aria-hidden="true">Ⅱ</span>{' '}
            {state.core.status === 'pausing' || pausePending ? 'Pause queued' : 'Pause run'}
          </button>
          <button
            className="button primary"
            type="button"
            disabled={!controlsEnabled || state.core.status !== 'paused' || resumePending}
            onClick={() => {
              void session.command({type: 'command.resume'});
            }}
          >
            <span aria-hidden="true">▷</span> {resumePending ? 'Resume queued' : 'Resume'}
          </button>
        </div>
      </section>
      {(state.connectionError || state.snapshotError) && (
        <div className="banner error" role="alert">
          <div>
            <strong>
              {state.connection === 'error'
                ? 'Event protocol error'
                : 'Backend connection needs attention'}
            </strong>
            <p>{state.connectionError ?? state.snapshotError}</p>
            <p>Displayed data may be stale. The backend run continues independently.</p>
          </div>
          <button
            className="button"
            type="button"
            onClick={() => {
              void session.reconnect();
              void session.refresh();
            }}
          >
            Reconnect
          </button>
        </div>
      )}
      <div className="mobile-toolbar">
        <button
          className="button"
          type="button"
          aria-expanded={navigationOpen}
          aria-controls="run-navigation"
          onClick={() => {
            setNavigationOpen(!navigationOpen);
            setInspectorOpen(false);
          }}
        >
          Hypotheses & rounds
        </button>
        <button
          className="button"
          type="button"
          aria-expanded={inspectorOpen}
          aria-controls="evidence-inspector"
          onClick={() => {
            setInspectorOpen(!inspectorOpen);
            setNavigationOpen(false);
          }}
        >
          Inspector
        </button>
      </div>
      <div className="workspace-grid">
        <aside
          id="run-navigation"
          className={`sidebar ${navigationOpen ? 'mobile-open' : ''}`}
          aria-label="Run navigation"
        >
          <div className="section-heading">
            <h2>Investigation</h2>
            <span className="count">{experiments.length}</span>
          </div>
          <button
            className={`nav-item all-activity ${hypothesisId === null && roundNumber === null ? 'selected' : ''}`}
            type="button"
            aria-pressed={hypothesisId === null && roundNumber === null}
            onClick={() => selectHypothesis(null)}
          >
            <span className="nav-symbol" aria-hidden="true">
              ◎
            </span>
            <span>
              All activity<small>The complete run</small>
            </span>
          </button>
          <div className="sidebar-label">HYPOTHESES</div>
          <QueryNotice
            query={state.queries.experiments}
            unavailable={state.queries.experiments.response?.experiments_ready === false}
            name="hypothesis log"
            onRetry={() => {
              void session.load('experiments');
            }}
          />
          {state.queries.experiments.response?.experiments_ready !== false &&
            experiments.map((entry, index) => (
              <button
                className={`nav-item ${hypothesisId === entry.hypothesis_id ? 'selected' : ''}`}
                key={entry.hypothesis_id}
                type="button"
                aria-pressed={hypothesisId === entry.hypothesis_id}
                onClick={() => selectHypothesis(entry.hypothesis_id)}
              >
                <span className="hypothesis-index">{String(index + 1).padStart(2, '0')}</span>
                <span className="nav-copy">
                  <strong>{entry.title || entry.hypothesis_id}</strong>
                  <small>
                    {entry.active
                      ? 'Active hypothesis'
                      : entry.resolved_outcome
                        ? label(entry.resolved_outcome)
                        : 'Under investigation'}
                  </small>
                </span>
                {entry.active && <span className="status-dot active" aria-hidden="true" />}
              </button>
            ))}
          {state.queries.experiments.response?.experiments_ready !== false &&
            state.queries.experiments.response &&
            experiments.length === 0 && (
              <p className="sidebar-empty">
                No hypotheses recorded yet. They will appear as the run investigates.
              </p>
            )}
          <div className="sidebar-label">ROUNDS</div>
          <div className="round-list">
            {Array.from(
              new Set([
                ...state.core.rounds.map(item => item.number),
                ...experiments.flatMap(entry => (entry.rounds ?? []).map(item => item.round)),
              ]),
            )
              .sort((a, b) => a - b)
              .filter(
                value =>
                  !hypothesis ||
                  includesHypothesisRound(state, hypothesis, value),
              )
              .map(value => {
                const summary = state.core.rounds.find(item => item.number === value);
                return (
                  <button
                    className={`round-nav ${value === roundNumber ? 'selected' : ''}`}
                    type="button"
                    key={value}
                    aria-pressed={value === roundNumber}
                    onClick={() => {
                      setRoundNumber(value === roundNumber ? null : value);
                      setNavigationOpen(false);
                      if (navigationOpen) document.getElementById('main-content')?.focus();
                    }}
                  >
                    <span>
                      <span className="round-symbol" aria-hidden="true">
                        ↳
                      </span>{' '}
                      Round {value}
                    </span>
                    <span className="round-status">{summary?.status ?? 'recorded'}</span>
                  </button>
                );
              })}
            {state.core.rounds.length === 0 && experiments.length === 0 && (
              <p className="sidebar-empty">Waiting for the first round.</p>
            )}
          </div>
          <div className="sidebar-footer">
            <span className="status-dot" />
            <span>
              Backend is the source of truth
              <small>
                Replay position <span className="mono">{state.core.sequence}</span>
              </small>
            </span>
          </div>
        </aside>
        <main id="main-content" className="main-stage" tabIndex={-1}>
          <div className="stage-heading">
            <div>
              <div className="eyebrow">
                {roundNumber !== null
                  ? `ROUND ${roundNumber}`
                  : hypothesis
                    ? 'HYPOTHESIS'
                    : 'RUN OVERVIEW'}
              </div>
              <h2>{hypothesis?.title || hypothesis?.hypothesis_id || 'Live workspace'}</h2>
            </div>
            <span className="activity-count">
              {executions.length} active {executions.length === 1 ? 'agent' : 'agents'}
            </span>
          </div>
          {hypothesis?.claim && <p className="hypothesis-claim">{hypothesis.claim}</p>}
          <nav className="view-tabs" aria-label="Workspace views">
            {(['activity', 'performance', 'evidence'] as const).map(item => (
              <button
                key={item}
                type="button"
                className={view === item ? 'selected' : ''}
                aria-pressed={view === item}
                onClick={() => setView(item)}
              >
                {item === 'activity'
                  ? 'Activity'
                  : item === 'performance'
                    ? 'Performance'
                    : 'Evidence'}
                {item === 'activity' && (
                  <span className="count">{state.core.transcript.length}</span>
                )}
              </button>
            ))}
          </nav>
          {view === 'activity' && (
            <>
              <div className="current-action">
                <span
                  className={`activity-indicator ${currentAction ? 'active' : ''}`}
                  aria-hidden="true"
                />
                <div>
                  <strong>
                    {currentAction
                      ? `${currentAction.agentKind} · ${label(currentAction.activity.mode)}`
                      : state.core.status === 'paused'
                        ? 'Run paused'
                        : hasRunEnded(state.core)
                          ? 'Run ended'
                          : 'Waiting for agent activity'}
                  </strong>
                  <span>
                    {currentAction?.activity.summary ||
                      (state.core.status === 'paused'
                        ? 'Resume when you are ready to continue.'
                        : hasRunEnded(state.core)
                          ? 'Recorded activity and evidence remain available for inspection.'
                          : 'Agent actions and tool output appear here as they arrive.')}
                  </span>
                </div>
                {currentAction?.model && <span className="model-label">{currentAction.model}</span>}
              </div>
              <Transcript
                key={state.runId}
                state={state}
                session={session}
                hypothesis={hypothesis}
                roundNumber={roundNumber}
              />
            </>
          )}
          {view === 'performance' && (
            <Performance
              state={state}
              onRetry={() => {
                void session.load('performance');
              }}
            />
          )}
          {view === 'evidence' && (
            <Evidence
              state={state}
              roundNumber={roundNumber}
              hypothesis={hypothesis}
              onRetry={() => {
                void session.load('design');
              }}
            />
          )}
          <Steer key={state.runId} state={state} session={session} />
        </main>
        <aside
          id="evidence-inspector"
          className={`inspector ${inspectorOpen ? 'mobile-open' : ''}`}
          aria-label="Evidence inspector"
        >
          <div className="section-heading">
            <h2>Inspector</h2>
            <span className="inspector-mark" aria-hidden="true">
              ⌘
            </span>
          </div>
          <section className="inspector-section">
            <div className="eyebrow">OBJECTIVE</div>
            <h3>{context?.objective_metric || 'Run objective'}</h3>
            <p>
              {context?.objective_description ||
                (state.queries.performance.loading
                  ? 'Loading objective…'
                  : 'The backend has not published an objective description.')}
            </p>
            <dl className="facts">
              <div>
                <dt>Direction</dt>
                <dd>
                  {context?.objective_direction === 'min'
                    ? 'Lower is better'
                    : context?.objective_direction === 'max'
                      ? 'Higher is better'
                      : 'Not recorded'}
                </dd>
              </div>
              <div>
                <dt>Baseline</dt>
                <dd>{metric(context?.objective_baseline_value, context?.objective_unit)}</dd>
              </div>
              {context?.objective_baseline_round != null && (
                <div>
                  <dt>Baseline round</dt>
                  <dd>{context.objective_baseline_round}</dd>
                </div>
              )}
            </dl>
          </section>
          <section className="inspector-section">
            <div className="eyebrow">
              {roundNumber === null ? 'SELECTION' : `ROUND ${roundNumber}`}
            </div>
            {round ? (
              <RoundFacts round={round} />
            ) : (
              <>
                <h3>{hypothesis ? 'Hypothesis details' : 'Explore the evidence'}</h3>
                <p>
                  {hypothesis?.action ||
                    'Select a hypothesis or round to inspect its evaluation, measurements, and changed files.'}
                </p>
                {hypothesis?.strategy_disposition && (
                  <Badge>{label(hypothesis.strategy_disposition)}</Badge>
                )}
                {hypothesis?.strategy_reason && <p>{hypothesis.strategy_reason}</p>}
                {roundNumber !== null && <p>This round has no persisted evaluation yet.</p>}
              </>
            )}
          </section>
          <section className="inspector-section">
            <div className="eyebrow">EXECUTION</div>
            {executions.length ? (
              executions.map(execution => (
                <div className="execution" key={execution.executionId}>
                  <h3>
                    {execution.agentKind}
                    <Badge tone="teal">{label(execution.activity.mode)}</Badge>
                  </h3>
                  <p>{execution.assignment}</p>
                  <dl className="facts">
                    <div>
                      <dt>Model</dt>
                      <dd>{execution.model || 'Not reported'}</dd>
                    </div>
                    <div>
                      <dt>Stage</dt>
                      <dd>{execution.stage}</dd>
                    </div>
                  </dl>
                </div>
              ))
            ) : (
              <p>No active agent executions.</p>
            )}
            {state.core.usage && (
              <dl className="facts">
                <div>
                  <dt>Input tokens</dt>
                  <dd>{number.format(state.core.usage.inputTokens)}</dd>
                </div>
                {state.core.usage.contextWindow !== null && (
                  <div>
                    <dt>Context window</dt>
                    <dd>{number.format(state.core.usage.contextWindow)}</dd>
                  </div>
                )}
              </dl>
            )}
          </section>
          {state.core.diagnostics.length > 0 && (
            <section className="inspector-section">
              <div className="eyebrow">DIAGNOSTICS</div>
              {state.core.diagnostics.slice(-3).map(diagnostic => (
                <div className="diagnostic" key={diagnostic.id ?? diagnostic.sequence}>
                  <Badge tone="red">{diagnostic.severity}</Badge>
                  <p>{diagnostic.summary}</p>
                  {diagnostic.hint && <p>{diagnostic.hint}</p>}
                  {diagnostic.detail && (
                    <details>
                      <summary>Technical detail</summary>
                      <pre>{diagnostic.detail}</pre>
                    </details>
                  )}
                </div>
              ))}
            </section>
          )}
        </aside>
      </div>
      <footer className="workspace-footer">
        <span>
          VibeSys <span className="muted">/ Single-run workspace</span>
        </span>
        <span>Changes and measurements are reported by the backend.</span>
      </footer>
    </div>
  );
}

function includesHypothesisRound(
  state: WorkspaceState,
  hypothesis: HypothesisEntry,
  round: number,
): boolean {
  // last_round covers persisted rounds only. The active hypothesis also owns
  // its live continuation, but must not absorb unrelated completed rounds.
  return (
    round >= hypothesis.first_round &&
    (round <= hypothesis.last_round ||
      (hypothesis.active === true &&
        state.core.rounds.some(item => item.number === round && item.status === 'active')))
  );
}

function Transcript({
  state,
  session,
  hypothesis,
  roundNumber,
}: {
  state: WorkspaceState;
  session: WorkspaceSession;
  hypothesis: HypothesisEntry | undefined;
  roundNumber: number | null;
}) {
  const [search, setSearch] = useState('');
  const [kind, setKind] = useState('all');
  const [follow, setFollow] = useState(true);
  const [limit, setLimit] = useState(150);
  const viewport = useRef<HTMLElement>(null);
  const entries = state.core.transcript.filter(
    entry =>
      (roundNumber === null || entry.roundNumber === roundNumber) &&
      (!hypothesis ||
        (entry.roundNumber !== undefined &&
          includesHypothesisRound(state, hypothesis, entry.roundNumber))) &&
      (kind === 'all' ||
        (kind === 'tool'
          ? entry.kind === 'tool' || entry.kind === 'subprocess'
          : entry.kind === kind)) &&
      (!search ||
        `${entry.content} ${entry.label ?? ''} ${entry.toolName ?? ''} ${entry.toolResponse ?? ''}`
          .toLowerCase()
          .includes(search.toLowerCase())),
  );
  const transcript = state.core.transcript;
  useEffect(() => {
    if (follow && viewport.current && transcript.length > 0)
      viewport.current.scrollTop = viewport.current.scrollHeight;
  }, [transcript, follow]);
  return (
    <>
      <div className="transcript-toolbar">
        <label className="search-field">
          <span aria-hidden="true">⌕</span>
          <input
            aria-label="Search activity"
            placeholder="Search activity…"
            value={search}
            onChange={event => setSearch(event.target.value)}
          />
        </label>
        <select
          aria-label="Activity type"
          value={kind}
          onChange={event => setKind(event.target.value)}
        >
          <option value="all">All activity</option>
          <option value="assistant">Agent messages</option>
          <option value="analysis">Reasoning</option>
          <option value="tool">Tools & processes</option>
          <option value="diagnostic">Diagnostics</option>
        </select>
        <button
          type="button"
          className={`button follow ${follow ? 'following' : ''}`}
          aria-pressed={follow}
          onClick={() => setFollow(!follow)}
        >
          {follow ? '↓ Following' : 'Follow live'}
        </button>
      </div>
      <section
        className="transcript-scroll"
        ref={viewport}
        // biome-ignore lint/a11y/noNoninteractiveTabindex: The scrollable transcript needs keyboard scrolling.
        tabIndex={0}
        aria-label="Run activity transcript"
        onScroll={event => {
          const element = event.currentTarget;
          if (follow && element.scrollHeight - element.scrollTop - element.clientHeight > 64)
            setFollow(false);
        }}
      >
        {state.core.historyAfterSequence > 0 && (
          <button
            className="button load-history"
            type="button"
            disabled={state.historyLoading}
            onClick={() => {
              setFollow(false);
              void session.loadOlder();
            }}
          >
            {state.historyLoading ? 'Loading history…' : 'Load earlier activity'}
          </button>
        )}
        {state.historyError && (
          <p className="inline-error" role="alert">
            {state.historyError}
          </p>
        )}
        {entries.length > limit && (
          <button
            className="button load-history"
            type="button"
            onClick={() => {
              setFollow(false);
              setLimit(limit + 150);
            }}
          >
            Show {Math.min(entries.length - limit, 150)} earlier entries
          </button>
        )}
        {entries.slice(-limit).map(entry => (
          <TranscriptItem key={entry.id} entry={entry} />
        ))}
        {entries.length === 0 && (
          <Empty
            title={
              search || kind !== 'all' || roundNumber !== null || hypothesis
                ? 'No matching activity'
                : state.connection === 'connecting'
                  ? 'Connecting to your run'
                  : 'The next step starts here'
            }
          >
            {search || kind !== 'all'
              ? 'Try another search or activity type.'
              : 'Agent messages, tool calls, and results will appear here. The workspace reconnects to the existing backend run.'}
          </Empty>
        )}
      </section>
    </>
  );
}

function TranscriptItem({entry}: {entry: TranscriptEntry}) {
  const isTool = entry.kind === 'tool' || entry.kind === 'subprocess';
  return (
    <article className={`transcript-entry ${isTool ? 'tool-entry' : ''}`}>
      <span className={`entry-icon ${entry.kind}`} aria-hidden="true">
        {isTool ? '⌁' : entry.kind === 'analysis' ? '◇' : entry.kind === 'result' ? '✓' : '·'}
      </span>
      <div className="entry-body">
        <div className="entry-meta">
          <strong>{entry.agentKind || 'System'}</strong>
          <span>{entry.label || label(entry.kind)}</span>
          {entry.roundLabel && <span className="entry-round">{entry.roundLabel}</span>}
        </div>
        {isTool ? (
          <details>
            <summary>
              <span>{entry.toolName || entry.label || 'Process output'}</span>
              <Badge
                tone={
                  entry.tone === 'failure'
                    ? 'red'
                    : entry.toolResponse !== undefined
                      ? 'teal'
                      : 'neutral'
                }
              >
                {entry.tone === 'failure'
                  ? 'Error'
                  : entry.toolResponse !== undefined
                    ? 'Result received'
                    : 'Activity'}
              </Badge>
            </summary>
            {entry.toolCall && <pre>{entry.toolCall}</pre>}
            {entry.toolArguments && <pre>{JSON.stringify(entry.toolArguments, null, 2)}</pre>}
            <pre>{entry.toolResponse ?? entry.content}</pre>
          </details>
        ) : (
          <div className={`entry-content ${entry.kind === 'analysis' ? 'reasoning' : ''}`}>
            {entry.content}
          </div>
        )}
      </div>
    </article>
  );
}

function Performance({state, onRetry}: {state: WorkspaceState; onRetry: () => void}) {
  const query = state.queries.performance;
  if (state.queries.experiments.response?.experiments_ready === false)
    return (
      <div className="data-view">
        <QueryNotice query={query} name="performance data" unavailable onRetry={onRetry} />
      </div>
    );
  const context = query.response?.performance_context;
  const rows = [...(query.response?.performance ?? [])].sort(
    (left, right) => left.round - right.round,
  );
  const measured = rows.filter(row => !row.profile_skipped && Number.isFinite(row.perf_metric));
  const latest = measured.at(-1);
  const baseline = context?.objective_baseline_value;
  const units = new Set(measured.map(row => row.perf_unit));
  return (
    <div className="data-view">
      <QueryNotice query={query} name="performance data" onRetry={onRetry} />
      <div className="data-view-heading">
        <div className="eyebrow">MEASUREMENT HISTORY</div>
        <h3>{context?.objective_metric || 'Recorded performance'}</h3>
        <p>
          {context?.objective_direction === 'min'
            ? 'Lower values are better.'
            : context?.objective_direction === 'max'
              ? 'Higher values are better.'
              : 'The backend has not published an optimization direction.'}{' '}
          Only recorded measurements are shown.
        </p>
      </div>
      <div className="metric-strip">
        <div>
          <span>Latest measurement</span>
          <strong>
            {latest ? metric(latest.perf_metric, latest.perf_unit) : 'Awaiting result'}
          </strong>
        </div>
        <div>
          <span>Objective baseline</span>
          <strong>{metric(baseline, context?.objective_unit)}</strong>
        </div>
        <div>
          <span>Measured rounds</span>
          <strong>{measured.length}</strong>
        </div>
      </div>
      {measured.length > 1 && units.size === 1 && (
        <PerformancePlot
          values={measured.map(row => ({round: row.round, value: row.perf_metric}))}
        />
      )}
      {rows.length > 0 ? (
        <div className="table-scroll">
          <table>
            <caption>Backend performance results by round</caption>
            <thead>
              <tr>
                <th>Round</th>
                <th>Measurement</th>
                <th>Check</th>
                <th>Evaluation</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(row => {
                const evaluation = state.queries.experiments.response?.experiments
                  ?.flatMap(entry => entry.rounds ?? [])
                  .find(entry => entry.round === row.round);
                return (
                  <tr key={row.round}>
                    <th scope="row">{row.round}</th>
                    <td className="mono">
                      {row.profile_skipped
                        ? 'Profile skipped'
                        : metric(row.perf_metric, row.perf_unit)}
                    </td>
                    <td>
                      <Badge tone={row.passed ? 'teal' : 'red'}>
                        {row.passed ? 'Passed' : 'Failed'}
                      </Badge>
                    </td>
                    <td>{evaluationLabel(evaluation)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        !query.loading &&
        !query.error && (
          <Empty title="No measurements yet">
            Performance results appear when a round records a measurement.
          </Empty>
        )
      )}
    </div>
  );
}

function PerformancePlot({values}: {values: {round: number; value: number}[]}) {
  const min = Math.min(...values.map(point => point.value));
  const max = Math.max(...values.map(point => point.value));
  const firstRound = values[0]?.round ?? 0;
  const lastRound = values.at(-1)?.round ?? firstRound;
  const points = values.map(point => ({
    x: 24 + ((point.round - firstRound) / (lastRound - firstRound || 1)) * 552,
    y: 150 - ((point.value - min) / (max - min || 1)) * 116,
  }));
  return (
    <figure className="performance-plot">
      <svg viewBox="0 0 600 180" role="img" aria-labelledby="plot-title">
        <title id="plot-title">
          Recorded performance from round {firstRound} to {lastRound}. Values are listed in the
          table below.
        </title>
        {[34, 92, 150].map(y => (
          <line className="plot-grid" key={y} x1="24" x2="576" y1={y} y2={y} />
        ))}
        <polyline
          className="plot-line"
          points={points.map(point => `${point.x},${point.y}`).join(' ')}
        />
        {points.map((point, index) => (
          <circle
            className="plot-point"
            key={values[index]?.round}
            cx={point.x}
            cy={point.y}
            r="4"
          />
        ))}
      </svg>
      <figcaption>
        <span>Round {firstRound}</span>
        <span>
          Range {metric(min)} to {metric(max)}
        </span>
        <span>Round {lastRound}</span>
      </figcaption>
    </figure>
  );
}

function Evidence({
  state,
  roundNumber,
  hypothesis,
  onRetry,
}: {
  state: WorkspaceState;
  roundNumber: number | null;
  hypothesis: HypothesisEntry | undefined;
  onRetry: () => void;
}) {
  const query = state.queries.design;
  if (query.response?.design_ready === false)
    return (
      <div className="data-view">
        <QueryNotice query={query} name="changed-file evidence" unavailable onRetry={onRetry} />
      </div>
    );
  const evaluations = (state.queries.experiments.response?.experiments ?? []).flatMap(
    entry => entry.rounds ?? [],
  );
  const rounds = [
    ...new Set([
      ...evaluations.map(round => round.round),
      ...(query.response?.design ?? []).map(round => round.round),
    ]),
  ]
    .sort((left, right) => left - right)
    .filter(
      round =>
        (roundNumber === null || round === roundNumber) &&
        (!hypothesis || (round >= hypothesis.first_round && round <= hypothesis.last_round)),
    );
  return (
    <div className="data-view">
      <QueryNotice query={query} name="changed-file evidence" onRetry={onRetry} />
      <div className="data-view-heading">
        <div className="eyebrow">PERSISTED EVIDENCE</div>
        <h3>Evaluations & changes</h3>
        <p>
          Evaluation status comes from the experiment log. File lists come from recorded workspace
          history.
        </p>
      </div>
      {rounds.map(number => {
        const round = evaluations.find(item => item.round === number);
        const design = query.response?.design?.find(item => item.round === number);
        return (
          <section className="evidence-round" key={number}>
            <div className="section-heading">
              <h3>Round {number}</h3>
              <Badge tone={round?.official_evaluation ? 'teal' : 'amber'}>
                {evaluationLabel(round)}
              </Badge>
            </div>
            {round ? (
              <RoundFacts round={round} />
            ) : (
              <p className="muted">Evaluation data is not available for this round.</p>
            )}
            <h4>Changed files</h4>
            {design?.files == null ? (
              <p className="muted">
                {query.loading
                  ? 'Loading file evidence…'
                  : query.error
                    ? 'File evidence could not be loaded.'
                    : 'No resolvable commit range is available for this round.'}
              </p>
            ) : design.files.length === 0 ? (
              <p className="muted">No candidate files changed in the recorded range.</p>
            ) : (
              <ul className="file-list">
                {design.files.map(file => (
                  <li key={file.path}>
                    <span className={`file-change ${file.change}`}>{file.change}</span>
                    <code>
                      {file.path}
                      {file.renamed_from && <small>from {file.renamed_from}</small>}
                    </code>
                  </li>
                ))}
              </ul>
            )}
          </section>
        );
      })}
      {rounds.length === 0 && (
        <Empty title="No evaluation selected">
          Recorded rounds and their evidence will appear here as they become available.
        </Empty>
      )}
    </div>
  );
}

function RoundFacts({round}: {round: HypothesisRound}) {
  return (
    <>
      <h3>
        Evaluation{' '}
        <Badge tone={round.official_evaluation ? 'teal' : 'amber'}>{evaluationLabel(round)}</Badge>
      </h3>
      <dl className="facts">
        <div>
          <dt>Judge</dt>
          <dd>{round.judge_verdict ? label(round.judge_verdict) : 'Not recorded'}</dd>
        </div>
        <div>
          <dt>Review</dt>
          <dd>{round.reviewed ? 'Reviewed' : 'Not reviewed'}</dd>
        </div>
        <div>
          <dt>Outcome</dt>
          <dd>{round.hypothesis_outcome ? label(round.hypothesis_outcome) : 'Unresolved'}</dd>
        </div>
        <div>
          <dt>Measurement</dt>
          <dd>{metric(round.perf_metric, round.perf_unit)}</dd>
        </div>
        {round.perf_delta_pct != null && (
          <div>
            <dt>Reported delta</dt>
            <dd>{number.format(round.perf_delta_pct)}%</dd>
          </div>
        )}
        <div>
          <dt>Disposition</dt>
          <dd>
            {round.candidate_disposition ? label(round.candidate_disposition) : 'Not recorded'}
          </dd>
        </div>
        <div>
          <dt>Commit</dt>
          <dd className="mono" title={round.commit ?? undefined}>
            {round.commit?.slice(0, 12) || 'Not recorded'}
          </dd>
        </div>
      </dl>
    </>
  );
}

function Steer({state, session}: {state: WorkspaceState; session: WorkspaceSession}) {
  const [text, setText] = useState('');
  const enabled =
    state.connection === 'connected' &&
    !hasRunEnded(state.core) &&
    state.core.status !== 'connecting' &&
    !state.command.sending;
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!text.trim() || !enabled) return;
    if (await session.command({type: 'command.steer', text: text.trim()})) setText('');
  }
  return (
    <form
      className="steer-composer"
      onSubmit={event => {
        void submit(event);
      }}
    >
      <div className="composer-heading">
        <label htmlFor="steer-input">Steer the run</label>
        <span>Applied by the backend at its next control point</span>
      </div>
      <div className="composer-input">
        <textarea
          id="steer-input"
          rows={2}
          value={text}
          onChange={event => setText(event.target.value)}
          placeholder="Suggest a direction or add a constraint…"
          disabled={!enabled}
        />
        <button className="button primary" type="submit" disabled={!enabled || !text.trim()}>
          {state.command.sending ? 'Sending…' : 'Send guidance'}
          <span aria-hidden="true">↗</span>
        </button>
      </div>
      <div className={`command-message ${state.command.error ? 'inline-error' : ''}`} role="status">
        {state.command.error || commandMessage(state)}
      </div>
    </form>
  );
}

function commandMessage(state: WorkspaceState): string {
  const ack = state.command.ack;
  if (!ack)
    return state.connection !== 'connected'
      ? 'Connect to the backend to send guidance.'
      : hasRunEnded(state.core)
        ? 'This run has ended.'
        : 'Guidance is sent to this run only.';
  if (ack.action === 'pause' && state.core.status === 'paused')
    return 'Backend confirmed: run paused.';
  if (ack.action === 'resume' && state.core.status === 'running')
    return 'Backend confirmed: run running.';
  if (ack.action === 'steer')
    return ack.status === 'pending'
      ? 'Guidance queued. Waiting for the backend to consume it.'
      : 'Backend consumed the guidance.';
  return `${ack.action === 'pause' ? 'Pause' : 'Resume'} ${ack.status === 'pending' ? 'queued' : 'accepted'}. Waiting for the backend lifecycle event.`;
}

function evaluationLabel(round: HypothesisRound | undefined): string {
  return round?.official_evaluation === true
    ? 'Official'
    : round?.official_evaluation === false || round?.judge_verdict === 'deferred'
      ? 'Provisional'
      : 'Not recorded';
}

function connectionLabel(connection: WorkspaceState['connection']): string {
  return {
    connecting: 'Connecting',
    connected: 'Live connection',
    disconnected: 'Disconnected',
    error: 'Protocol error',
  }[connection];
}

function statusTone(
  status: WorkspaceState['core']['status'],
): 'teal' | 'red' | 'amber' | 'neutral' {
  switch (status) {
    case 'running':
    case 'completed':
      return 'teal';
    case 'failed':
      return 'red';
    case 'paused':
    case 'pausing':
      return 'amber';
    case 'connecting':
    case 'starting':
      return 'neutral';
  }
}

function Badge({
  children,
  tone = 'neutral',
}: {
  children: ReactNode;
  tone?: 'teal' | 'red' | 'amber' | 'neutral';
}) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

function Empty({title, children}: {title: string; children: ReactNode}) {
  return (
    <div className="empty-state">
      <span className="empty-symbol" aria-hidden="true">
        ⌁
      </span>
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}

function QueryNotice({
  query,
  unavailable = false,
  name,
  onRetry,
}: {
  query: QueryState;
  unavailable?: boolean;
  name: string;
  onRetry: () => void;
}) {
  if (query.error)
    return (
      <div className="query-notice inline-error" role="alert">
        <p>
          Could not load {name}: {query.error}
        </p>
        <button className="button" type="button" onClick={onRetry}>
          Retry
        </button>
      </div>
    );
  if (unavailable)
    return (
      <p className="query-notice" role="status">
        Waiting for a run to attach. The {name} is not available yet.
      </p>
    );
  if (query.loading)
    return (
      <p className="query-notice" role="status">
        {query.response ? 'Refreshing' : 'Loading'} {name}…
      </p>
    );
  return null;
}
