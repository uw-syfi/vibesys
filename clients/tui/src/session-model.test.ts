import {describe, expect, it, test} from 'bun:test';
import type {RunEvent, RunStatus} from '@vibesys/backend-client';
import {hasRunEnded} from '@vibesys/core-state';
import type {SessionState} from './session-model.js';
import {
  applyActiveExecutionCheckpoint,
  applyEvent,
  applyEventBatch,
  chatDocked,
  chatPaneVisible,
  closePane,
  closeThemePicker,
  cyclePaneFocus,
  dismissErrorBanner,
  enterExperimentDrilldown,
  enterExperimentRound,
  enterUnownedExperimentRound,
  experimentIndexItems,
  failPane,
  focusedPane,
  focusPane,
  focusRound,
  hypothesisPlanningActivity,
  initialSessionState,
  leaveExperimentDrilldown,
  leaveHypothesisDetail,
  markEventStreamUnavailable,
  moveExperimentSelection,
  moveHypothesisRoundSelection,
  moveThemeSelection,
  openChat,
  openPane,
  openThemePicker,
  reportError,
  runStatusLabel,
  selectExperimentActivity,
  selectNextAgent,
  selectNextRound,
  selectRound,
  setChatDockFits,
  setExperiments,
  setPaneContent,
  setTheme,
  showDetail,
  togglePaneZoom,
  toggleTodos,
  unownedExperimentRounds,
  visibleActiveExecutions,
  visibleConversation,
  visiblePhases,
  visibleTodos,
} from './session-model.js';
import {headerSegments, runStateText, usageText} from './ui/header.js';

describe('event batch projection', () => {
  it('keeps the existing banner while resumed history ends in a running session', () => {
    const before = reportError(initialSessionState(), 'Local input problem', {scope: 'input'});

    const state = applyEventBatch(before, [
      {
        ...event(1, 'run_failed'),
        diagnostic: {
          code: 'interrupted',
          summary: 'Previous process was interrupted.',
          scope: 'run',
          severity: 'fatal',
          retryability: 'never',
        },
      },
      event(2, 'run_started', {
        kind: 'run_started',
        outer_loop: 'agent',
        input: '.',
        max_rounds: 3,
      }),
    ]);

    expect(state.core.status).toBe('running');
    expect(hasRunEnded(state.core)).toBe(false);
    expect(state.core.diagnostics).toHaveLength(1);
    expect(state.errorBanner).toEqual(before.errorBanner);
  });

  it('surfaces the final diagnostic when a batch ends in failure', () => {
    const state = applyEventBatch(initialSessionState(), [
      event(1, 'run_started', {
        kind: 'run_started',
        outer_loop: 'agent',
        input: '.',
        max_rounds: 3,
      }),
      {
        ...event(2, 'run_failed'),
        diagnostic: {
          code: 'run_failed',
          summary: 'The current run failed.',
          scope: 'run',
          severity: 'fatal',
          retryability: 'never',
        },
      },
    ]);

    expect(state.core.status).toBe('failed');
    expect(state.errorBanner).toMatchObject({message: 'The current run failed.', scope: 'run'});
  });
});

describe('hypothesis planning activity', () => {
  function stateFor(
    kind: string,
    roundLabel: string,
    entries: Parameters<typeof setExperiments>[1] = [],
  ): SessionState {
    return setExperiments(
      {
        ...initialSessionState(),
        core: {
          ...initialSessionState().core,
          phases: [
            {
              kind,
              status: 'active',
              roundNumber: 3,
              roundLabel,
              startedAt: '2026-01-01T00:00:00Z',
            },
          ],
        },
      },
      entries,
    );
  }

  it("maps the loop's named pre, profiler, and plan phases", () => {
    expect(hypothesisPlanningActivity(stateFor('orchestrator', 'round-3-pre'))).toMatchObject({
      stage: 'pre',
      roundNumber: 3,
    });
    expect(hypothesisPlanningActivity(stateFor('profiler', 'round-3-profiler'))).toMatchObject({
      stage: 'profile',
      roundNumber: 3,
    });
    expect(hypothesisPlanningActivity(stateFor('orchestrator', 'round-3-plan'))).toMatchObject({
      stage: 'plan',
      roundNumber: 3,
    });
  });

  it('still presents a reprompted plan as planning', () => {
    // The framework reprompts the orchestrator once when a plan reuses a
    // hypothesis_id, and labels that attempt `round-N-retry-K-plan`. It is the
    // same stage of the same round, so the operator must not see planning
    // silently stop just because the first attempt was rejected.
    expect(
      hypothesisPlanningActivity(stateFor('orchestrator', 'round-3-retry-1-plan')),
    ).toMatchObject({stage: 'plan', roundNumber: 3});
  });

  it('does not treat an unrelated retry label as planning', () => {
    expect(
      hypothesisPlanningActivity(stateFor('orchestrator', 'round-3-retry-1-judge')),
    ).toBeNull();
  });

  it('does not present a phase as planning after its round has a hypothesis', () => {
    const entries = [
      {
        hypothesis_id: 'H-03',
        identified: true,
        first_round: 3,
        last_round: 3,
        rounds: [],
        kept: false,
        active: true,
      },
    ] as Parameters<typeof setExperiments>[1];

    expect(
      hypothesisPlanningActivity(stateFor('orchestrator', 'round-3-plan', entries)),
    ).toBeNull();
  });

  it('keeps elapsed planning time from the earliest observed planning phase', () => {
    const state = stateFor('orchestrator', 'round-3-plan');
    state.core.phases = [
      {
        kind: 'orchestrator',
        status: 'completed',
        roundNumber: 3,
        roundLabel: 'round-3-pre',
        startedAt: '2026-01-01T00:00:00Z',
      },
      {
        kind: 'orchestrator',
        status: 'active',
        roundNumber: 3,
        roundLabel: 'round-3-plan',
        startedAt: '2026-01-01T00:01:00Z',
      },
    ];

    expect(hypothesisPlanningActivity(state)?.startedAt).toBe('2026-01-01T00:00:00Z');
  });

  it('ignores similarly named phases outside the loop contract', () => {
    expect(hypothesisPlanningActivity(stateFor('profiler', 'round-3-profile-retry'))).toBeNull();
  });

  it('selects planning activity after existing hypotheses and opens its recorded round', () => {
    let state = setExperiments(
      {
        ...stateFor('orchestrator', 'round-3-plan'),
        core: {
          ...stateFor('orchestrator', 'round-3-plan').core,
          rounds: [{number: 3, status: 'active' as const}],
        },
      },
      [
        {
          hypothesis_id: 'H-02',
          identified: true,
          first_round: 2,
          last_round: 2,
          rounds: [{round: 2, passed: true, reviewed: true}],
          kept: true,
          active: false,
        },
      ],
    );

    state = moveExperimentSelection(state, 1);
    expect(state.experimentLog?.selectedActivity).toBe(true);

    const opened = enterExperimentDrilldown(state);
    expect(opened.hypothesisScope).toMatchObject({id: 'round-3', rounds: [3], source: 'round'});
    expect(opened.selectedRound).toBe(3);
  });

  it('opens the first planning activity without a synthetic hypothesis row', () => {
    const state = {
      ...stateFor('orchestrator', 'round-3-pre'),
      core: {
        ...stateFor('orchestrator', 'round-3-pre').core,
        rounds: [{number: 3, status: 'active' as const}],
      },
    };

    const opened = enterExperimentDrilldown(selectExperimentActivity(state));
    expect(opened.hypothesisScope).toMatchObject({id: 'round-3', label: 'Round 3'});
  });

  it('does not duplicate the planning round as an unowned index item', () => {
    const state = {
      ...stateFor('orchestrator', 'round-3-plan'),
      core: {
        ...stateFor('orchestrator', 'round-3-plan').core,
        rounds: [{number: 3, status: 'active' as const}],
      },
    };

    expect(unownedExperimentRounds(state)).toEqual([]);
  });

  it('orders mixed history by round and keeps live planning last', () => {
    const state = setExperiments(
      {
        ...initialSessionState(),
        core: {
          ...initialSessionState().core,
          phases: [
            {
              kind: 'orchestrator',
              status: 'active' as const,
              roundNumber: 5,
              roundLabel: 'round-5-plan',
            },
          ],
          rounds: [
            {number: 4, status: 'completed' as const},
            {number: 2, status: 'completed' as const},
            {number: 5, status: 'active' as const},
          ],
        },
      },
      [
        {
          hypothesis_id: 'H-03',
          identified: true,
          first_round: 3,
          last_round: 3,
          rounds: [],
          kept: false,
          active: false,
        },
        {
          hypothesis_id: 'H-01',
          identified: true,
          first_round: 1,
          last_round: 1,
          rounds: [],
          kept: true,
          active: false,
        },
      ],
    );

    expect(
      experimentIndexItems(state).map(item =>
        item.kind === 'hypothesis'
          ? item.entry.hypothesis_id
          : item.kind === 'round'
            ? `round-${item.roundNumber}`
            : `planning-${item.activity.roundNumber}`,
      ),
    ).toEqual(['H-01', 'round-2', 'H-03', 'round-4', 'planning-5']);
  });

  it('moves a selected planning row onto the hypothesis that materializes from it', () => {
    let state = selectExperimentActivity(
      setExperiments(
        {
          ...stateFor('orchestrator', 'round-3-plan'),
          core: {
            ...stateFor('orchestrator', 'round-3-plan').core,
            rounds: [{number: 3, status: 'active' as const}],
          },
        },
        [],
      ),
    );
    // Phase completion may arrive before the refreshed experiment response.
    state = {
      ...state,
      core: {
        ...state.core,
        phases: state.core.phases.map(phase => ({...phase, status: 'completed' as const})),
      },
    };
    state = setExperiments(state, [
      {
        hypothesis_id: 'H-03',
        identified: true,
        first_round: 3,
        last_round: 3,
        rounds: [],
        kept: false,
        active: true,
      },
    ]);

    expect(state.experimentLog).toMatchObject({
      selectedId: 'H-03',
      selectedActivity: false,
      selectedActivityRound: null,
    });
  });
});

describe('hypothesis scope label', () => {
  it('prefers the backend-supplied title over the hypothesis id', () => {
    const state = setExperiments(initialSessionState(), [
      {
        hypothesis_id: 'H-01',
        identified: true,
        title: 'Batch decode requests',
        first_round: 1,
        last_round: 1,
        rounds: [{round: 1, passed: true, reviewed: true}],
        kept: false,
        active: false,
      },
    ]);

    const opened = enterExperimentRound(state, 1);

    expect(opened?.hypothesisScope).toMatchObject({
      id: 'H-01',
      label: 'Batch decode requests · r1',
    });
  });

  it('falls back to the hypothesis id when there is no title', () => {
    const state = setExperiments(initialSessionState(), [
      {
        hypothesis_id: 'H-01',
        identified: true,
        first_round: 1,
        last_round: 1,
        rounds: [{round: 1, passed: true, reviewed: true}],
        kept: false,
        active: false,
      },
    ]);

    const opened = enterExperimentRound(state, 1);

    expect(opened?.hypothesisScope).toMatchObject({id: 'H-01', label: 'H-01 · r1'});
  });
});

describe('unowned rounds', () => {
  it('opens an observed round and keeps its turns scoped to that round', () => {
    const state = {
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        rounds: [{number: 9, status: 'completed' as const}],
        transcript: [
          {id: 'r8', kind: 'assistant' as const, content: 'old', roundNumber: 8},
          {id: 'r9', kind: 'assistant' as const, content: 'kept', roundNumber: 9},
        ],
      },
    };
    const opened = enterUnownedExperimentRound(state, 9);

    expect(opened?.hypothesisScope).toMatchObject({id: 'round-9', source: 'round'});
    expect(opened && visibleConversation(opened).map(entry => entry.content)).toEqual(['kept']);
  });

  it('does not turn an announced but unobserved round into historical work', () => {
    const state = {
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        maxRounds: 9,
      },
    };
    expect(enterUnownedExperimentRound(state, 9)).toBeNull();
  });

  it('does not assign invalid zero-based ranges to a hypothesis', () => {
    const state = setExperiments(initialSessionState(), [
      {
        hypothesis_id: 'H-invalid',
        identified: true,
        first_round: 0,
        last_round: 0,
        rounds: [],
        kept: false,
        active: false,
      },
    ]);

    expect(enterExperimentRound(state, 0)).toBeNull();
  });
});

describe('session event model', () => {
  it('tracks concurrent agent executions independently through activity and finish events', () => {
    let state = initialSessionState();
    state = applyEvent(
      state,
      executionEvent(
        1,
        'agent_execution_started',
        'impl-1',
        {
          kind: 'agent_execution_started',
          stage: 'implementation',
          attempt: 1,
          system_prompt: '',
          user_prompt: 'Implement the queue',
          activity: {
            kind: 'agent_execution_activity_changed',
            mode: 'thinking',
            summary: 'Inspecting the queue',
            tool: null,
          },
        },
        'implementer',
      ),
    );
    state = applyEvent(
      state,
      executionEvent(
        2,
        'agent_execution_started',
        'review-1',
        {
          kind: 'agent_execution_started',
          stage: 'review',
          attempt: null,
          system_prompt: '',
          user_prompt: 'Review the diff',
          activity: {
            kind: 'agent_execution_activity_changed',
            mode: 'waiting',
            summary: 'Waiting for the implementation',
            tool: null,
          },
        },
        'reviewer',
      ),
    );
    state = applyEvent(
      state,
      executionEvent(
        3,
        'agent_execution_activity_changed',
        'impl-1',
        {
          kind: 'agent_execution_activity_changed',
          mode: 'tool',
          summary: 'Running queue tests',
          tool: 'Bash',
        },
        'implementer',
      ),
    );

    expect(Object.keys(state.core.activeExecutions)).toEqual(['impl-1', 'review-1']);
    expect(state.core.activeExecutions['impl-1']?.activity).toEqual({
      mode: 'tool',
      summary: 'Running queue tests',
      tool: 'Bash',
    });
    expect(state.core.activeExecutions['review-1']?.activity.summary).toBe(
      'Waiting for the implementation',
    );

    state = applyEvent(
      state,
      executionEvent(
        4,
        'agent_execution_finished',
        'impl-1',
        {
          kind: 'agent_execution_finished',
          error: null,
        },
        'implementer',
      ),
    );
    expect(Object.keys(state.core.activeExecutions)).toEqual(['review-1']);
  });

  it('keeps last-known executions but stops presenting them after the event stream is lost', () => {
    const active = applyEvent(
      initialSessionState(),
      executionEvent(
        1,
        'agent_execution_started',
        'impl-1',
        {
          kind: 'agent_execution_started',
          stage: 'implementation',
          attempt: 1,
          system_prompt: '',
          user_prompt: 'Implement the queue',
          activity: {
            kind: 'agent_execution_activity_changed',
            mode: 'thinking',
            summary: 'Inspecting the queue',
            tool: null,
          },
        },
        'implementer',
      ),
    );

    const disconnected = markEventStreamUnavailable(active);

    expect(disconnected.core).toBe(active.core);
    expect(disconnected.core.activeExecutions['impl-1']).toBeDefined();
    expect(visibleActiveExecutions(disconnected)).toEqual([]);
  });

  it('reconciles from a checkpoint without advancing the replay cursor', () => {
    const state = applyActiveExecutionCheckpoint(initialSessionState(), [
      {
        execution_id: 'judge-2',
        agent_kind: 'judge',
        round_label: 'round-2-judge',
        stage: 'evaluation',
        attempt: 1,
        assignment: 'Evaluate the candidate',
        started_at: '2026-01-01T00:00:00Z',
        activity: {
          kind: 'agent_execution_activity_changed',
          mode: 'thinking',
          summary: 'Inspecting the diff',
          tool: null,
        },
      },
    ]);

    expect(state.core.sequence).toBe(0);
    expect(state.core.activeExecutions['judge-2']).toMatchObject({
      agentKind: 'judge',
      roundNumber: 2,
      activity: {summary: 'Inspecting the diff'},
    });

    const newer = {
      ...state,
      core: {
        ...state.core,
        sequence: 5,
      },
    };
    expect(applyActiveExecutionCheckpoint(newer, [], 4).core.activeExecutions).toEqual(
      state.core.activeExecutions,
    );
  });

  it('clears every active execution when the run is interrupted', () => {
    const active = applyEvent(
      initialSessionState(),
      executionEvent(
        1,
        'agent_execution_started',
        'impl-1',
        {
          kind: 'agent_execution_started',
          stage: 'implementation',
          attempt: 1,
          system_prompt: '',
          user_prompt: 'Implement the queue',
          activity: {
            kind: 'agent_execution_activity_changed',
            mode: 'thinking',
            summary: 'Inspecting the queue',
            tool: null,
          },
        },
        'implementer',
      ),
    );
    const interrupted = applyEvent(active, {
      sequence: 2,
      timestamp: '2026-01-01T00:00:01Z',
      type: 'run_interrupted',
      data: {kind: 'run_interrupted', reason: 'SIGINT', signal: 'SIGINT'},
    });

    expect(interrupted.core.activeExecutions).toEqual({});
  });

  it('tracks and finishes chat executions before routing their transcript', () => {
    let state = applyEvent(
      initialSessionState(),
      executionEvent(
        1,
        'agent_execution_started',
        'chat-execution',
        {
          kind: 'agent_execution_started',
          stage: 'chat',
          attempt: null,
          system_prompt: '',
          user_prompt: 'What is running?',
          activity: {
            kind: 'agent_execution_activity_changed',
            mode: 'thinking',
            summary: 'Inspecting the run',
            tool: null,
          },
        },
        'chat',
      ),
    );

    expect(state.core.activeExecutions['chat-execution']?.activity.summary).toBe(
      'Inspecting the run',
    );
    state = applyEvent(
      state,
      executionEvent(
        2,
        'agent_execution_finished',
        'chat-execution',
        {kind: 'agent_execution_finished', error: null},
        'chat',
      ),
    );
    expect(state.core.activeExecutions).toEqual({});
  });

  it('reduces semantic events into a presentation-neutral transcript', () => {
    let state = initialSessionState();
    state = applyEvent(
      state,
      event(1, 'phase_started', {
        kind: 'phase',
        phase: 'judge',
        attempt: 2,
      }),
    );
    state = applyEvent(
      state,
      event(2, 'agent_output_chunk', {
        kind: 'agent_output_chunk',
        channel: 'assistant',
        content: 'checking accuracy\n',
      }),
    );
    state = applyEvent(
      state,
      event(3, 'judge_result', {
        kind: 'judge_result',
        verdict: 'pass',
        feedback: '',
        attempt: 2,
      }),
    );

    expect(state.core.transcript.map(entry => entry.kind)).toEqual([
      'status',
      'assistant',
      'result',
    ]);
    expect(state.core.transcript[1]?.content).toBe('checking accuracy\n');
  });

  it('renders a deferred round as provisional rather than failed', () => {
    const state = applyEvent(
      initialSessionState(),
      event(1, 'round_finished', {
        kind: 'round_finished',
        attempts: 1,
        judge_verdict: 'skipped',
        perf_metric: null,
        perf_unit: null,
      }),
    );

    expect(state.core.transcript[0]).toMatchObject({
      label: 'round-1 · SKIPPED',
      tone: 'normal',
    });
  });

  it('ignores replayed events and recognizes an ended run', () => {
    let state = applyEvent(initialSessionState(), event(4, 'run_finished'));
    state = applyEvent(state, event(3, 'run_failed'));

    expect(state.core.status).toBe('completed');
    expect(state.core.sequence).toBe(4);
    expect(hasRunEnded(state.core)).toBe(true);
  });

  it('shows structured configuration failures as terminal conversation entries', () => {
    const state = applyEvent(
      initialSessionState(),
      event(1, 'configuration_failed', {
        kind: 'configuration_failed',
        code: 'resume_limit_exhausted',
        stage: 'resume_resolution',
        message: 'This run has completed 30 rounds.',
        usage: null,
        exit_code: 2,
      }),
    );

    expect(state.core.status).toBe('failed');
    expect(hasRunEnded(state.core)).toBe(true);
    expect(state.overlay).toBeNull();
    expect(state.errorBanner).toMatchObject({
      title: 'Configuration failed',
      severity: 'fatal',
    });
    expect(state.errorBanner?.message).toContain('This run has completed 30 rounds.');
    expect(state.core.transcript[0]?.content).toContain('This run has completed 30 rounds.');
    expect(state.core.transcript[0]?.content).toContain('resume_limit_exhausted');
    expect(state.core.transcript[0]?.tone).toBe('failure');
  });

  it('promotes an invocation failure to one terminal error banner', () => {
    let state = applyEvent(initialSessionState(), {
      ...event(1, 'invocation_finished', {
        kind: 'invocation_finished',
        error: 'RuntimeError: app-server initialization was denied',
      }),
      status: 'failed',
      invocation_id: 'invocation-1',
    });

    expect(state.errorBanner).toMatchObject({
      title: 'Invocation failed',
      severity: 'recoverable',
      invocationId: 'invocation-1',
    });
    state = applyEvent(state, {
      ...event(2, 'run_failed'),
      text: 'RuntimeError: app-server initialization was denied',
    });

    expect(state.errorBanner).toMatchObject({
      title: 'Run failed',
      severity: 'fatal',
      count: 2,
    });
  });

  it('dismisses an error without changing session state or suppressing later errors', () => {
    const state = reportError({...initialSessionState(), selectedRound: 2}, 'The request failed.', {
      scope: 'request',
    });
    const dismissed = dismissErrorBanner(state);

    expect(dismissed.errorBanner).toBeNull();
    expect(dismissed.selectedRound).toBe(2);
    expect(dismissErrorBanner(dismissed)).toBe(dismissed);
    expect(
      reportError(dismissed, 'A later failure.', {scope: 'transport'}).errorBanner,
    ).toMatchObject({message: 'A later failure.', scope: 'transport'});
  });

  it('resurfaces a dismissed diagnostic when core projects its terminal promotion', () => {
    let state = applyEvent(initialSessionState(), {
      ...event(1, 'invocation_finished'),
      invocation_id: 'invocation-1',
      diagnostic: {
        id: 'failure-1',
        code: 'agent_failed',
        summary: 'The worker failed.',
        detail: null,
        scope: 'invocation',
        severity: 'error',
        retryability: 'manual',
      },
    });
    state = dismissErrorBanner(state);
    state = applyEvent(state, {
      ...event(2, 'run_failed'),
      diagnostic: {
        id: 'failure-1',
        code: 'agent_failed',
        summary: 'The worker failed.',
        detail: 'Exit code: 2',
        scope: 'invocation',
        severity: 'fatal',
        retryability: 'manual',
      },
    });

    expect(state.errorBanner).toMatchObject({
      diagnosticId: 'failure-1',
      detail: 'Exit code: 2',
      severity: 'fatal',
      count: 1,
    });
  });

  it('prefers structured diagnostics and deduplicates their ids across terminal events', () => {
    let state = applyEvent(initialSessionState(), {
      ...event(1, 'invocation_finished', {
        kind: 'invocation_finished',
        error: 'legacy invocation error',
      }),
      text: 'legacy invocation text',
      diagnostic: {
        id: 'failure-1',
        code: 'unrecognized_future_code',
        summary: 'The worker could not start.',
        detail: 'PermissionError: sandbox rejected the worker.',
        hint: 'Check the sandbox permissions.',
        scope: 'invocation',
        severity: 'error',
        retryability: 'manual',
      },
    });
    state = applyEvent(state, {
      ...event(2, 'run_failed'),
      text: 'legacy terminal text',
      diagnostic: {
        id: 'failure-1',
        code: 'unrecognized_future_code',
        summary: 'The worker could not start.',
        detail: 'PermissionError: sandbox rejected the worker.\nExit code: 1',
        hint: 'Check the sandbox permissions.',
        scope: 'invocation',
        severity: 'fatal',
        retryability: 'manual',
      },
    });

    expect(state.errorBanner).toMatchObject({
      title: 'Invocation failed',
      message: 'The worker could not start.',
      detail: 'PermissionError: sandbox rejected the worker.\nExit code: 1',
      hint: 'Check the sandbox permissions.',
      diagnosticId: 'failure-1',
      severity: 'fatal',
      count: 2,
    });
  });

  it('keeps the more detailed terminal message when a failure is deduplicated', () => {
    let state = applyEvent(initialSessionState(), {
      ...event(1, 'invocation_finished', {
        kind: 'invocation_finished',
        error: 'RuntimeError: app-server initialization was denied',
      }),
      invocation_id: 'invocation-1',
    });
    const terminalMessage =
      'RuntimeError: app-server initialization was denied\nOperation not permitted (os error 1)';
    state = applyEvent(state, {...event(2, 'run_failed'), text: terminalMessage});

    expect(state.errorBanner).toMatchObject({count: 2, message: terminalMessage});
  });

  it('uses an error-bearing invocation result even when the status is absent', () => {
    const state = applyEvent(initialSessionState(), {
      ...event(1, 'invocation_finished', {
        kind: 'invocation_finished',
        error: 'The agent process could not start.',
      }),
      status: null,
    });

    expect(state.errorBanner).toMatchObject({
      title: 'Invocation failed',
      message: 'The agent process could not start.',
    });
  });

  it('uses the terminal event type for an empty failure message', () => {
    const failed = applyEvent(initialSessionState(), event(1, 'run_failed'));
    const interrupted = applyEvent(initialSessionState(), event(1, 'run_interrupted'));

    expect(failed.errorBanner?.message).toBe('Run failed.');
    expect(interrupted.errorBanner?.message).toBe('Run interrupted.');
  });

  it('shows structured interruption details when no event text is present', () => {
    const state = applyEvent(initialSessionState(), {
      ...event(1, 'run_interrupted', {
        kind: 'run_interrupted',
        reason: 'launcher_terminated',
        signal: 'SIGTERM',
      }),
      diagnostic: {
        id: 'interrupted-1',
        code: 'interrupted',
        summary: 'Run interrupted',
        detail: 'RuntimeError: launcher_terminated (SIGTERM)',
        scope: 'run',
        severity: 'fatal',
        retryability: 'never',
      },
    });

    expect(state.errorBanner).toMatchObject({
      title: 'Run interrupted',
      message: 'Run interrupted',
      detail: 'RuntimeError: launcher_terminated (SIGTERM)',
      severity: 'fatal',
    });
  });

  it('routes chat agent trajectory events away from the experiment transcript', () => {
    let state = initialSessionState();
    state = applyEvent(
      state,
      chatEvent(1, 'agent_output_chunk', {
        kind: 'agent_output_chunk',
        channel: 'analysis',
        content: 'Inspecting the latest round',
      }),
    );
    state = applyEvent(
      state,
      chatEvent(2, 'tool_call', {
        kind: 'tool_call',
        tool: 'read_file',
        args: {path: 'progress.md'},
        status: null,
      }),
    );
    state = applyEvent(
      state,
      chatEvent(3, 'tool_result', {
        kind: 'tool_result',
        tool: 'read_file',
        content: 'Round 2 improved throughput.',
        is_error: false,
      }),
    );
    state = applyEvent(
      state,
      chatEvent(4, 'chat', {
        kind: 'chat',
        answer: 'Round 2 improved throughput.',
      }),
    );

    expect(state.core.transcript).toEqual([]);
    expect(state.core.agentKind).toBeNull();
    // Routed to the chat, and filtered there to the answer itself.
    expect(state.chatConversation.map(entry => entry.kind)).toEqual(['assistant']);
    expect(state.chatConversation.at(-1)?.content).toBe('Round 2 improved throughput.');
  });

  it('coalesces streamed assistant chunks and pairs each tool call with its result', () => {
    let state = initialSessionState();
    state = applyEvent(
      state,
      event(
        1,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'assistant',
          content: 'hello ',
        },
        'invocation-1',
      ),
    );
    state = applyEvent(
      state,
      event(
        2,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'assistant',
          content: 'world',
        },
        'invocation-1',
      ),
    );
    state = applyEvent(
      state,
      event(
        3,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'tool',
          content: '→ Bash(command="first")\n',
        },
        'invocation-1',
      ),
    );
    state = applyEvent(
      state,
      event(
        4,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'tool',
          content: 'first result',
        },
        'invocation-1',
      ),
    );
    state = applyEvent(
      state,
      event(
        5,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'tool',
          content: '→ Bash(command="second")\n',
        },
        'invocation-1',
      ),
    );
    state = applyEvent(
      state,
      event(
        6,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'tool',
          content: 'second result',
        },
        'invocation-1',
      ),
    );

    expect(state.core.transcript.map(entry => entry.content)).toEqual([
      'hello world',
      '→ Bash(command="first")\nfirst result',
      '→ Bash(command="second")\nsecond result',
    ]);
    expect(state.core.transcript[1]).toMatchObject({
      toolCall: '→ Bash(command="first")\n',
      toolResponse: 'first result',
    });
  });

  it('pairs typed tool_call and tool_result events into tool turns', () => {
    let state = initialSessionState();
    state = applyEvent(
      state,
      event(1, 'tool_call', {kind: 'tool_call', tool: 'Bash', args: {command: 'first'}}, 'inv-1'),
    );
    state = applyEvent(
      state,
      event(
        2,
        'tool_result',
        {kind: 'tool_result', tool: 'Bash', content: 'first result'},
        'inv-1',
      ),
    );
    state = applyEvent(
      state,
      event(3, 'tool_call', {kind: 'tool_call', tool: 'Bash', args: {command: 'second'}}, 'inv-1'),
    );
    state = applyEvent(
      state,
      event(
        4,
        'tool_result',
        {kind: 'tool_result', tool: 'Bash', content: 'second result'},
        'inv-1',
      ),
    );

    expect(state.core.transcript.map(entry => entry.content)).toEqual([
      'first result',
      'second result',
    ]);
    expect(state.core.transcript[0]).toMatchObject({
      kind: 'tool',
      toolName: 'Bash',
      toolArguments: {command: 'first'},
      toolResult: {kind: 'tool_result', tool: 'Bash', content: 'first result'},
    });
  });

  it('correlates parallel typed tool results by call ID', () => {
    let state = initialSessionState();
    state = applyEvent(
      state,
      event(
        1,
        'tool_call',
        {kind: 'tool_call', tool: 'Read', call_id: 'call-a', args: {path: 'a'}},
        'inv-1',
      ),
    );
    state = applyEvent(
      state,
      event(
        2,
        'tool_call',
        {kind: 'tool_call', tool: 'Read', call_id: 'call-b', args: {path: 'b'}},
        'inv-1',
      ),
    );
    state = applyEvent(
      state,
      event(
        3,
        'tool_result',
        {kind: 'tool_result', tool: 'Read', call_id: 'call-b', content: 'result b'},
        'inv-1',
      ),
    );
    state = applyEvent(
      state,
      event(
        4,
        'tool_result',
        {kind: 'tool_result', tool: 'Read', call_id: 'call-a', content: 'result a'},
        'inv-1',
      ),
    );

    expect(state.core.transcript.map(entry => entry.toolResult?.content)).toEqual([
      'result a',
      'result b',
    ]);
  });

  it('retains long typed tool-call arguments for presentation', () => {
    const longArg = 'x'.repeat(200);
    const state = applyEvent(
      initialSessionState(),
      event(1, 'tool_call', {kind: 'tool_call', tool: 'Edit', args: {text: longArg, count: 3}}),
    );

    expect(state.core.transcript[0]?.toolArguments).toEqual({text: longArg, count: 3});
    expect(state.core.transcript[0]?.toolCall).toBeUndefined();
  });

  it('prefers typed tool events over legacy tool-channel chunks', () => {
    let state = initialSessionState();
    state = applyEvent(
      state,
      event(1, 'tool_call', {kind: 'tool_call', tool: 'Bash', args: {command: 'ls'}}, 'inv-1'),
    );
    // A legacy duplicate of the same call must not render a second turn.
    state = applyEvent(
      state,
      event(
        2,
        'agent_output_chunk',
        {kind: 'agent_output_chunk', channel: 'tool', content: '→ Bash(command="ls")\n'},
        'inv-1',
      ),
    );

    expect(state.core.typedToolEvents).toBe(true);
    expect(state.core.transcript).toHaveLength(1);
    expect(state.core.transcript[0]?.toolArguments).toEqual({command: 'ls'});
  });

  it('stores todo updates as per-phase data instead of transcript text', () => {
    const state = applyEvent(
      initialSessionState(),
      event(1, 'todo_update', {
        kind: 'todo_update',
        todos: [
          {content: 'Set up project', status: 'completed'},
          {content: 'Add tests', status: 'pending'},
        ],
      }),
    );

    expect(state.core.todos).toEqual([
      {
        executionId: null,
        agentKind: 'judge',
        roundNumber: 1,
        items: [
          {content: 'Set up project', status: 'completed'},
          {content: 'Add tests', status: 'pending'},
        ],
      },
    ]);
    expect(state.core.transcript).toHaveLength(0);
  });

  it('keeps each phase’s todo list separate so agents never clobber each other', () => {
    let state = initialSessionState();
    state = applyEvent(state, {
      sequence: 1,
      timestamp: '2026-01-01T00:00:00Z',
      type: 'todo_update',
      agent_kind: 'implementer',
      round_label: 'round-1',
      data: {kind: 'todo_update', todos: [{content: 'Edit files', status: 'in_progress'}]},
    });
    state = applyEvent(state, {
      sequence: 2,
      timestamp: '2026-01-01T00:01:00Z',
      type: 'todo_update',
      agent_kind: 'judge',
      round_label: 'round-1',
      data: {kind: 'todo_update', todos: [{content: 'Check behavior', status: 'pending'}]},
    });
    state = applyEvent(state, {
      sequence: 3,
      timestamp: '2026-01-01T00:02:00Z',
      type: 'todo_update',
      agent_kind: 'implementer',
      round_label: 'round-2',
      data: {kind: 'todo_update', todos: [{content: 'Fix regression', status: 'pending'}]},
    });

    // Live view follows the currently active agent (round-2 implementer).
    expect(visibleTodos(state)).toEqual([{content: 'Fix regression', status: 'pending'}]);
    // Selecting a past agent shows that phase's final list, not the latest one.
    const withAgent = {...state, selectedRound: 1, selectedAgentKind: 'implementer'};
    expect(visibleTodos(withAgent)).toEqual([{content: 'Edit files', status: 'in_progress'}]);
    // Selecting only a round shows the round's most recently updated list.
    const withRound = {...state, selectedRound: 1};
    expect(visibleTodos(withRound)).toEqual([{content: 'Check behavior', status: 'pending'}]);
  });

  it('keeps todos separate for concurrent executions of the same agent role', () => {
    let state = initialSessionState();
    state = applyEvent(
      state,
      executionEvent(
        1,
        'todo_update',
        'impl-a',
        {
          kind: 'todo_update',
          todos: [{content: 'Edit implementation A', status: 'in_progress'}],
        },
        'implementer',
      ),
    );
    state = applyEvent(
      state,
      executionEvent(
        2,
        'todo_update',
        'impl-b',
        {
          kind: 'todo_update',
          todos: [{content: 'Edit implementation B', status: 'in_progress'}],
        },
        'implementer',
      ),
    );

    expect(state.core.todos).toHaveLength(2);
    expect(state.core.todos.map(phase => phase.executionId)).toEqual(['impl-a', 'impl-b']);
    expect(visibleTodos({...state, selectedRound: 1, selectedAgentKind: 'implementer'})).toEqual([
      {content: 'Edit implementation B', status: 'in_progress'},
    ]);
  });

  it('hides todos when the active phase has not emitted any', () => {
    let state = initialSessionState();
    state = applyEvent(state, {
      sequence: 1,
      timestamp: '2026-01-01T00:00:00Z',
      type: 'todo_update',
      agent_kind: 'implementer',
      round_label: 'round-1',
      data: {kind: 'todo_update', todos: [{content: 'Edit files', status: 'completed'}]},
    });
    // The judge phase starts without emitting todos; the implementer's
    // leftovers must not linger in the live view.
    state = applyEvent(state, {
      sequence: 2,
      timestamp: '2026-01-01T00:01:00Z',
      type: 'phase_started',
      agent_kind: 'judge',
      round_label: 'round-1',
    });

    expect(visibleTodos(state)).toEqual([]);
  });

  it('preserves unknown todo statuses for the renderer to degrade', () => {
    const state = applyEvent(
      initialSessionState(),
      event(1, 'todo_update', {
        kind: 'todo_update',
        todos: [{content: 'Mystery step', status: 'deferred'}],
      }),
    );

    expect(state.core.todos[0]?.items).toEqual([{content: 'Mystery step', status: 'deferred'}]);
  });

  it('toggles the todo strip between collapsed and expanded', () => {
    const state = initialSessionState();
    expect(state.todosExpanded).toBe(false);
    expect(toggleTodos(state).todosExpanded).toBe(true);
    expect(toggleTodos(toggleTodos(state)).todosExpanded).toBe(false);
  });

  // The header carries exactly one status token, read from backend-owned core
  // state. `/pause` is visible as `pausing…` until the backend says the pause
  // landed, and the run's ended status replaces it rather than joining it.
  it('renders one header status token per backend run status', () => {
    expect(runStatusLabel('running')).toBe('running');
    expect(runStatusLabel('pausing')).toBe('pausing…');
    expect(runStatusLabel('paused')).toBe('paused');
    expect(runStatusLabel('completed')).toBe('completed');
  });

  it('reads a pause from the backend and drops it when the run ends', () => {
    const statusChange = (sequence: number, status: RunStatus, previous: RunStatus) =>
      event(sequence, 'run_status_changed', {
        kind: 'run_status_changed',
        status,
        previous,
      });

    let state = applyEvent(initialSessionState(), statusChange(1, 'pausing', 'running'));
    expect(runStateText(state)).toBe('pausing…');
    state = applyEvent(state, statusChange(2, 'paused', 'pausing'));
    expect(runStateText(state)).toBe('paused');
    state = applyEvent(state, statusChange(3, 'completed', 'paused'));

    expect(runStateText(state)).toBe('completed');
  });

  it('feeds usage updates into the header token meter', () => {
    let state = initialSessionState();
    expect(usageText(state)).toBeNull();
    state = applyEvent(
      state,
      event(1, 'usage_update', {
        kind: 'usage_update',
        input_tokens: 20_100,
        context_window: 1_000_000,
        model: 'claude-sonnet-4-6',
      }),
    );

    expect(state.core.usage).toEqual({
      inputTokens: 20_100,
      contextWindow: 1_000_000,
      model: 'claude-sonnet-4-6',
    });
    expect(usageText(state)).toBe('20k/1.0M context');
    expect(state.core.transcript).toHaveLength(0);
  });

  it('classifies prompt events as distinct markdown turns', () => {
    const state = applyEvent(
      initialSessionState(),
      event(
        1,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'prompt',
          content: '# Task\n\nUse `pytest`.',
        },
        'invocation-1',
      ),
    );

    expect(state.core.transcript).toMatchObject([
      {
        kind: 'prompt',
        content: '# Task\n\nUse `pytest`.',
      },
    ]);
  });

  it('derives round-scoped agent flow from run and phase events', () => {
    let state = applyEvent(
      initialSessionState(),
      event(1, 'run_started', {
        kind: 'run_started',
        outer_loop: 'agent',
        input: 'examples/kv-store',
        max_rounds: 5,
      }),
    );
    expect(state.core.phases).toEqual([]);

    state = applyEvent(state, {
      ...event(2, 'phase_started', {kind: 'phase', phase: 'orchestrator', attempt: null}),
      agent_kind: 'orchestrator',
    });
    state = applyEvent(state, {
      ...event(3, 'phase_finished', {kind: 'phase', phase: 'orchestrator', attempt: null}),
      agent_kind: 'orchestrator',
    });

    expect(state.core.rounds).toMatchObject([{number: 1, status: 'active'}]);
    expect(visiblePhases(state).map(phase => `${phase.kind}:${phase.status}`)).toEqual([
      'orchestrator:completed',
      'implementer:pending',
      'judge:pending',
      'profiler:pending',
    ]);
    expect(visiblePhases(state)[0]).toMatchObject({
      kind: 'orchestrator',
      status: 'completed',
      roundNumber: 1,
      roundLabel: 'round-1',
    });
  });

  it('filters conversation entries by selected round and agent', () => {
    let state = initialSessionState();
    state = applyEvent(
      state,
      event(
        1,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'assistant',
          content: 'judge output',
        },
        'judge-1',
      ),
    );
    state = applyEvent(state, {
      ...event(
        2,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'assistant',
          content: 'profiler output',
        },
        'profiler-1',
      ),
      agent_kind: 'profiler',
    });
    state = applyEvent(state, {
      ...event(
        3,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'assistant',
          content: 'round two judge output',
        },
        'judge-2',
      ),
      round_label: 'round-2',
    });

    state = selectNextAgent(state);
    expect(state.selectedAgentKind).toBe('judge');
    expect(visibleConversation(state).map(entry => entry.content)).toEqual([
      'round two judge output',
    ]);
    state = selectNextRound(state);
    state = selectNextAgent(state);
    expect(visibleConversation(state).map(entry => entry.content)).toEqual(['judge output']);

    state = selectRound(state, 2);
    expect(visibleConversation(state).map(entry => entry.content)).toEqual([
      'round two judge output',
    ]);
  });
});

describe('hypothesis detail navigation', () => {
  const hypothesis = {
    hypothesis_id: 'H-01',
    identified: true,
    claim: 'A complete causal hypothesis that must never be truncated in its detail view.',
    first_round: 1,
    last_round: 2,
    rounds: [
      {round: 1, passed: true, reviewed: true},
      {round: 2, passed: false, reviewed: true},
    ],
    kept: false,
    active: false,
  };

  it('moves from index to hypothesis to round and unwinds one level at a time', () => {
    const landing = setExperiments(initialSessionState(), [hypothesis]);
    const detail = enterExperimentDrilldown(landing);

    expect(detail.hypothesisDetail).toEqual({entryKey: 'H-01', selectedRound: 2});
    expect(detail.hypothesisScope).toBeNull();

    const earlier = moveHypothesisRoundSelection(detail, -1);
    expect(earlier.hypothesisDetail?.selectedRound).toBe(1);

    const round = enterExperimentDrilldown(earlier);
    expect(round.hypothesisScope).toMatchObject({id: 'H-01', rounds: [1, 2]});
    expect(round.selectedRound).toBe(1);

    const backToDetail = leaveExperimentDrilldown(round);
    expect(backToDetail.hypothesisDetail?.selectedRound).toBe(1);
    expect(leaveHypothesisDetail(backToDetail).hypothesisDetail).toBeNull();
  });

  it('closes a detail whose hypothesis disappears during refresh', () => {
    const detail = enterExperimentDrilldown(setExperiments(initialSessionState(), [hypothesis]));

    expect(setExperiments(detail, []).hypothesisDetail).toBeNull();
  });
});

describe('docked experiment chat', () => {
  const entry = {
    hypothesis_id: 'H-01',
    identified: true,
    first_round: 1,
    last_round: 1,
    rounds: [{round: 1, passed: true, reviewed: true}],
    kept: false,
    active: false,
  };

  it('docks beside the log and nowhere else', () => {
    const landing = setExperiments(initialSessionState(), [entry]);
    expect(chatDocked(landing)).toBe(true);
    expect(chatPaneVisible(landing)).toBe(true);

    // The hypothesis summary and its trajectories both use the full content
    // column rather than competing with experiment chat.
    const detail = enterExperimentDrilldown(landing);
    expect(detail.hypothesisDetail).not.toBeNull();
    expect(chatDocked(detail)).toBe(false);
    const scoped = enterExperimentDrilldown(detail);
    expect(scoped.hypothesisScope).not.toBeNull();
    expect(chatDocked(scoped)).toBe(false);
  });

  it('stops docking in a terminal too narrow for two columns', () => {
    const narrow = setChatDockFits(initialSessionState(), false);

    expect(chatDocked(narrow)).toBe(false);
    expect(chatPaneVisible(narrow)).toBe(false);
  });

  it('gives the pane the keys instead of opening a modal over the table', () => {
    const opened = openChat(initialSessionState());

    expect(opened.chatOpen).toBe(false);
    expect(opened.layout.focus).toBe('chat');
  });

  it('still opens the modal where the chat cannot dock', () => {
    const narrow = setChatDockFits(initialSessionState(), false);

    expect(openChat(narrow).chatOpen).toBe(true);
  });

  it('yields to the modal while one is open', () => {
    const modal = {...initialSessionState(), chatOpen: true};

    expect(chatDocked(modal)).toBe(true);
    expect(chatPaneVisible(modal)).toBe(false);
  });
});

describe('right pane layout', () => {
  it('starts single-pane with focus on the transcript', () => {
    const state = initialSessionState();

    expect(state.layout.right).toBeNull();
    expect(state.layout.focus).toBe('left');
  });

  it('opens a pane pending, titled, and focused', () => {
    const state = openPane(initialSessionState(), 'perf');

    expect(state.layout.right).toMatchObject({view: 'perf', title: 'Performance', pending: true});
    expect(state.layout.focus).toBe('right');
  });

  it('keeps the old chart on screen while the same view refreshes', () => {
    const perf = setPaneContent(openPane(initialSessionState(), 'perf'), 'perf', 'chart');

    expect(openPane(perf, 'perf').layout.right?.content).toBe('chart');
  });

  it('ignores a response for a pane the operator has since closed', () => {
    const closed = closePane(openPane(initialSessionState(), 'perf'));

    const stale = setPaneContent(closed, 'perf', 'late chart');

    expect(stale).toBe(closed);
    expect(failPane(closed, 'perf', 'late failure')).toBe(closed);
  });

  it('reports a failed query in the pane without closing it', () => {
    const failed = failPane(openPane(initialSessionState(), 'perf'), 'perf', 'socket closed');

    expect(failed.layout.right?.error).toBe('socket closed');
    expect(failed.layout.right?.pending).toBe(false);
  });

  it('cycles focus left to right through the columns on screen', () => {
    // The landing view carries the docked chat, so opening a visualization
    // makes three columns: chat, log, pane.
    const open = openPane(initialSessionState(), 'perf');
    expect(open.layout.focus).toBe('right');
    const first = cyclePaneFocus(open);
    expect(first.layout.focus).toBe('chat');
    expect(cyclePaneFocus(first).layout.focus).toBe('left');
    expect(cyclePaneFocus(cyclePaneFocus(first)).layout.focus).toBe('right');

    const closed = closePane(open);
    expect(closed.layout.right).toBeNull();
    expect(closed.layout.focus).toBe('left');
  });

  it('cycles between the chat and the log while no visualization is open', () => {
    const docked = initialSessionState();

    expect(cyclePaneFocus(docked).layout.focus).toBe('chat');
    expect(cyclePaneFocus(cyclePaneFocus(docked)).layout.focus).toBe('left');
  });

  it('does nothing to focus when only one column is on screen', () => {
    // Too narrow for the dock and no visualization: the log has the row.
    const single = setChatDockFits(initialSessionState(), false);

    expect(cyclePaneFocus(single)).toBe(single);
    expect(closePane(single)).toBe(single);
  });

  it('refuses focus for a column that is not on screen', () => {
    const narrow = setChatDockFits(initialSessionState(), false);

    expect(focusPane(narrow, 'chat')).toBe(narrow);
    expect(focusPane(initialSessionState(), 'right').layout.focus).toBe('left');
    expect(focusPane(initialSessionState(), 'chat').layout.focus).toBe('chat');
  });

  it('hands the keys back to the log when the dock stops fitting', () => {
    const focused = focusPane(initialSessionState(), 'chat');

    expect(setChatDockFits(focused, false).layout.focus).toBe('left');
  });

  it('leaves the chat transcript untouched when the pane closes', () => {
    const withChat = {
      ...openPane(initialSessionState(), 'perf'),
      chatOpen: true,
      chatConversation: [{id: 'c1', kind: 'user' as const, content: 'why is r3 slower?'}],
    };

    const closed = closePane(withChat);

    expect(closed.chatOpen).toBe(true);
    expect(closed.chatConversation).toEqual(withChat.chatConversation);
  });

  it('zooms the semantic focused pane and restores the unchanged split state', () => {
    const split = setPaneContent(openPane(initialSessionState(), 'perf'), 'perf', 'chart');
    const chat = focusPane(split, 'chat');
    const zoomed = togglePaneZoom(chat);

    expect(focusedPane(zoomed)).toBe('chat');
    expect(zoomed.layout.zoomedPane).toBe('chat');
    expect(zoomed.layout.right).toBe(split.layout.right);
    expect(zoomed.chatConversation).toBe(split.chatConversation);
    expect(cyclePaneFocus(zoomed)).toBe(zoomed);

    const restored = togglePaneZoom(zoomed);
    expect(restored.layout).toEqual(chat.layout);
  });

  it('uses round focus to zoom agents and transcript', () => {
    const round = {...initialSessionState(), experimentLog: null};
    const agents = focusRound(round, 'agents');

    expect(togglePaneZoom(agents).layout.zoomedPane).toBe('agents');
    expect(togglePaneZoom(round).layout.zoomedPane).toBe('transcript');
  });

  it('never reports the hidden agents pane as focused beside a visualization', () => {
    const agents = focusRound({...initialSessionState(), experimentLog: null}, 'agents');
    const split = focusPane(openPane(agents, 'perf'), 'left');

    expect(split.roundFocus).toBe('agents');
    expect(focusedPane(split)).toBe('transcript');
    expect(togglePaneZoom(split).layout.zoomedPane).toBe('transcript');
    expect(focusedPane(closePane(split))).toBe('agents');
  });
});

function event(
  sequence: number,
  type: RunEvent['type'],
  data?: RunEvent['data'],
  invocationId?: string,
): RunEvent {
  return {
    sequence,
    timestamp: '2026-01-01T00:00:00Z',
    type,
    round_label: 'round-1',
    ...(type === 'run_started' ? {} : {agent_kind: 'judge'}),
    ...(invocationId === undefined ? {} : {invocation_id: invocationId}),
    ...(data === undefined ? {} : {data}),
  };
}

function executionEvent(
  sequence: number,
  type: RunEvent['type'],
  executionId: string,
  data: NonNullable<RunEvent['data']>,
  agentKind: string,
): RunEvent {
  return {
    sequence,
    timestamp: '2026-01-01T00:00:00Z',
    type,
    execution_id: executionId,
    round_label: 'round-1',
    agent_kind: agentKind,
    data,
  };
}

function chatEvent(
  sequence: number,
  type: RunEvent['type'],
  data: NonNullable<RunEvent['data']>,
): RunEvent {
  return {
    ...event(sequence, type, data, 'chat-1'),
    agent_kind: 'chat',
    round_label: 'experiment-chat',
  };
}

describe('theme picker', () => {
  it('opens on the active theme', () => {
    const state = openThemePicker(initialSessionState('catppuccin-latte'));

    expect(state.themePicker?.selected).toBe('catppuccin-latte');
    expect(state.themeName).toBe('catppuccin-latte');
  });

  it('moves the selection without changing the theme in use', () => {
    const opened = openThemePicker(initialSessionState());

    const moved = moveThemeSelection(opened, 1);

    expect(moved.themePicker?.selected).toBe('light');
    expect(moved.themeName).toBe('dark');
  });

  it('clamps at both ends of the list rather than wrapping', () => {
    const opened = openThemePicker(initialSessionState());

    expect(moveThemeSelection(opened, -1)).toBe(opened);
    expect(moveThemeSelection(opened, 99).themePicker?.selected).toBe('high-contrast-light');
  });

  it('ignores movement when the picker is closed', () => {
    const state = initialSessionState();

    expect(moveThemeSelection(state, 1)).toBe(state);
  });

  it('closes on dismissal and on applying a theme', () => {
    const opened = moveThemeSelection(openThemePicker(initialSessionState()), 1);

    expect(closeThemePicker(opened).themePicker).toBeNull();
    expect(closeThemePicker(opened).themeName).toBe('dark');
    expect(setTheme(opened, 'light').themePicker).toBeNull();
    expect(setTheme(opened, 'light').themeName).toBe('light');
    // Re-applying the active theme is still an answer to the picker.
    expect(setTheme(opened, 'dark').themePicker).toBeNull();
  });
});

describe('re-entering a round after leaving it', () => {
  const hypothesis = {
    hypothesis_id: 'H1',
    first_round: 1,
    last_round: 2,
    rounds: [{round: 1}, {round: 2}],
  } as never;

  function scoped() {
    const base: SessionState = {
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        rounds: [
          {number: 1, status: 'completed'},
          {number: 2, status: 'active'},
        ],
        phases: [
          {
            kind: 'implementer',
            status: 'completed',
            roundNumber: 1,
            roundLabel: 'round-1-implementer',
          },
          {kind: 'judge', status: 'completed', roundNumber: 1, roundLabel: 'round-1-judge'},
        ],
        transcript: [
          {id: 'a', kind: 'assistant', label: 'implementer', content: 'patched', roundNumber: 1},
        ],
      },
    };
    return setExperiments(base, [hypothesis]);
  }

  test('shows the round again after leaving and coming back through an error', () => {
    const withLog = scoped();
    const first = enterExperimentRound(withLog, 1);
    expect(first).not.toBeNull();
    expect(visiblePhases(first as SessionState)).toHaveLength(2);
    expect(visibleConversation(first as SessionState)).toHaveLength(1);

    const left = leaveExperimentDrilldown(first as SessionState);
    const errored = showDetail(left, 'Unknown command: /nope. Use /help.');
    const again = enterExperimentRound(errored, 1);
    expect(again).not.toBeNull();
    expect(visiblePhases(again as SessionState)).toHaveLength(2);
    expect(visibleConversation(again as SessionState)).toHaveLength(1);
  });
});

describe('a long run', () => {
  test('keeps an earlier round openable after thousands of entries', () => {
    let state: SessionState = initialSessionState();
    // Two hundred rounds of chatter: far past any per-entry cap.
    for (let round = 1; round <= 200; round += 1) {
      for (let turn = 0; turn < 30; turn += 1) {
        state = applyEvent(state, {
          sequence: round * 100 + turn,
          timestamp: '2026-01-01T00:00:00Z',
          type: 'agent_output_chunk',
          agent_kind: 'implementer',
          round_label: `round-${round}-implementer`,
          invocation_id: `impl-${round}-${turn}`,
          data: {
            kind: 'agent_output_chunk',
            channel: 'assistant',
            content: `round ${round} turn ${turn}`,
          },
        } as RunEvent);
      }
    }

    // The round the operator opens still has its turns, whole.
    const late = {...state, selectedRound: 200};
    expect(visibleConversation(late).length).toBeGreaterThan(0);

    // And a round from early in the run is either fully there or fully gone,
    // never a fragment that reads as a round which barely ran.
    const early = {...state, selectedRound: 150};
    const entries = visibleConversation(early);
    expect(entries.length === 0 || entries.length >= 30).toBe(true);
  });
});

describe('scope follows round navigation', () => {
  const hypothesisA = {
    hypothesis_id: 'H-A',
    identified: true,
    title: 'Batch decode requests',
    first_round: 1,
    last_round: 2,
    rounds: [
      {round: 1, passed: true, reviewed: true},
      {round: 2, passed: true, reviewed: true},
    ],
    kept: true,
    active: false,
  };
  const hypothesisB = {
    hypothesis_id: 'H-B',
    identified: true,
    title: 'Cache the tokenizer',
    first_round: 3,
    last_round: 3,
    rounds: [{round: 3, passed: false, reviewed: true}],
    kept: false,
    active: true,
  };

  function runWith(entries: Parameters<typeof setExperiments>[1]): SessionState {
    return setExperiments(
      {
        ...initialSessionState(),
        core: {
          ...initialSessionState().core,
          rounds: [
            {number: 1, status: 'completed' as const},
            {number: 2, status: 'completed' as const},
            {number: 3, status: 'active' as const},
          ],
        },
      },
      entries,
    );
  }

  it('re-derives the scope when ] crosses into another hypothesis', () => {
    const scoped = enterExperimentRound(runWith([hypothesisA, hypothesisB]), 2);
    expect(scoped?.hypothesisScope?.id).toBe('H-A');

    const crossed = selectNextRound(scoped as SessionState);

    expect(crossed.selectedRound).toBe(3);
    expect(crossed.hypothesisScope).toMatchObject({
      id: 'H-B',
      title: 'Cache the tokenizer',
      rounds: [3],
    });
    // The header names the hypothesis that owns the round on screen.
    const segments = headerSegments(crossed, false).map(segment => segment.text);
    expect(segments).toContain('Cache the tokenizer');
    expect(segments).not.toContain('Batch decode requests');
    // Esc unwinds to the owner of the visible round, not to the entry point.
    const unwound = leaveExperimentDrilldown(crossed);
    expect(unwound.hypothesisDetail).toEqual({entryKey: 'H-B', selectedRound: 3});
    expect(unwound.experimentLog?.selectedId).toBe('H-B');
  });

  it('scopes a round no hypothesis owns as the round itself', () => {
    const scoped = enterExperimentRound(runWith([hypothesisA]), 2);

    const crossed = selectNextRound(scoped as SessionState);

    expect(crossed.selectedRound).toBe(3);
    expect(crossed.hypothesisScope).toMatchObject({id: 'round-3', source: 'round'});
    expect(crossed.hypothesisDetail).toBeNull();
  });

  it('keeps the scope object while navigating within one hypothesis', () => {
    const scoped = enterExperimentRound(runWith([hypothesisA, hypothesisB]), 1) as SessionState;

    const moved = selectNextRound(scoped);

    expect(moved.selectedRound).toBe(2);
    expect(moved.hypothesisScope).toBe(scoped.hypothesisScope);
    // The Esc cursor still follows the round within the hypothesis.
    expect(moved.hypothesisDetail).toEqual({entryKey: 'H-A', selectedRound: 2});
  });

  it('adds a continuation round to a live scope on refresh', () => {
    const scoped = enterExperimentRound(runWith([hypothesisA, hypothesisB]), 3) as SessionState;
    expect(scoped.hypothesisScope?.rounds).toEqual([3]);

    const grown = setExperiments(scoped, [
      hypothesisA,
      {
        ...hypothesisB,
        last_round: 4,
        rounds: [...hypothesisB.rounds, {round: 4, passed: true, reviewed: false}],
      },
    ]);

    expect(grown.hypothesisScope).toMatchObject({id: 'H-B', rounds: [3, 4]});
    expect(grown.selectedRound).toBe(3);
  });

  it('degrades a scope whose hypothesis vanished to the round on screen', () => {
    const scoped = enterExperimentRound(runWith([hypothesisA, hypothesisB]), 3) as SessionState;

    const refreshed = setExperiments(scoped, [hypothesisA]);

    expect(refreshed.hypothesisScope).toMatchObject({id: 'round-3', source: 'round'});
    expect(refreshed.hypothesisDetail).toBeNull();
  });
});
