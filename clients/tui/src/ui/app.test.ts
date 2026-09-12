import {afterEach, describe, expect, it} from 'bun:test';
import {
  BoxRenderable,
  CliRenderEvents,
  getBorderSides,
  InputRenderable,
  type Renderable,
  rgbToHex,
  ScrollBoxRenderable,
  TextareaRenderable,
} from '@opentui/core';
import {createTestRenderer, type TestRendererSetup} from '@opentui/core/testing';
import type {ChatOptions, HypothesisEntry} from '@vibesys/backend-client';
import type {CoreRunStatus} from '@vibesys/core-state';
import {chatHelpText, parseCommand} from '../commands.js';
import type {SessionController} from '../session-controller.js';
import {
  activeChatThreadSettings,
  type ChatThreadSettings,
  chatMenuCustomModel,
  clearAgentSelection,
  clearEntrySelection,
  closeChatMenu,
  closeOverlays,
  closePane,
  closeThemePicker,
  cyclePaneFocus,
  dismissErrorBanner,
  enterExperimentDrilldown,
  enterExperimentRound,
  enterUnownedExperimentRound,
  focusPane,
  focusRound,
  initialSessionState,
  leaveExperimentDrilldown,
  leaveHypothesisDetail,
  moveChatMenuSelection,
  moveExperimentSelection,
  moveHypothesisRoundSelection,
  moveThemeSelection,
  normalizeFocus,
  openChatModelMenu,
  openChatResumeMenu,
  openExperimentLog,
  openHypothesisDetail,
  openPane,
  type PaneFocus,
  type PaneView,
  type RoundFocus,
  reportError,
  type SessionState,
  selectAgent,
  selectExperimentActivity,
  selectedChatMenuRow,
  selectNextEntry,
  selectNextRound,
  selectNextTodo,
  selectPreviousRound,
  setChatDockFits,
  setChatMenuCustomModel,
  setChatModelMenuOptions,
  setExperiments,
  setPaneContent,
  setTheme,
  switchChatThread,
  togglePaneZoom,
} from '../session-model.js';
import {TRANSCRIPT_MIN} from './agent-map.js';
import {createOpenTuiApp, type OpenTuiApp} from './app.js';
import {MIN_DOCK_WIDTH} from './chat-pane.js';
import type {ClipboardCopyResult, SelectionClipboard} from './clipboard.js';
import {renderDesignSummary} from './design-log.js';
import {paneTitle} from './focus.js';
import {headerBackground} from './header.js';
import {MIN_SPLIT_WIDTH} from './right-pane.js';
import {
  contrastRatio,
  ensureContrast,
  listThemes,
  mix,
  resolveTheme,
  SUBTLE_TEXT_MIN_CONTRAST,
  scrim,
  THEME_NAMES,
  type ThemeName,
} from './theme.js';

const cleanup: Array<() => void> = [];

afterEach(() => {
  for (const destroy of cleanup.splice(0).reverse()) destroy();
});

describe('OpenTUI presentation', () => {
  it('renders model state with a persistent input panel', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        status: 'running',
        agentKind: 'optimizer',
        roundLabel: 'round 2',
        phases: [
          {
            kind: 'optimizer',
            status: 'active',
            roundNumber: null,
            roundLabel: 'round 2',
          },
        ],
        transcript: [
          {
            id: '1',
            kind: 'assistant',
            label: 'optimizer · round 2',
            agentKind: 'optimizer',
            content: '## Result\n\nUse `fast_path()`.',
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('fast_path()'));
    // The header names the run state and the activity, never the backend's
    // round label. `round 2` is not a label this loop emits, so the agent kind
    // supplies the word.
    expect(frame).toContain('VibeSys · running · optimizer');
    expect(frame).not.toContain('running · optimizer · round 2');
    // No round is selected, so the agent strip is headed by the run. The run has
    // no rounds yet, so there is no tab row to draw.
    expect(frame).toContain('Run flow');
    expect(tabsVisible(testRenderer)).toBe(false);
    expect(frame).toContain('● optimizer');
    expect(frame).toContain('Result');
    expect(frame).toContain('Command');
    expect(frame).toContain('Type /help for commands');
  });

  // A command ack for /pause or /steer submitted from the modal chat has to
  // render over that modal, and under the theme picker.
  it('stacks the command overlay above the chat modal and below the theme picker', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('Command'));

    const root = testRenderer.renderer.root;
    const overlay = root.findDescendantById('overlay')?.zIndex;
    const chatModal = root.findDescendantById('chat-overlay')?.zIndex;
    const themePicker = root.findDescendantById('theme-picker')?.zIndex;

    expect(overlay).toBeGreaterThan(chatModal ?? Number.POSITIVE_INFINITY);
    expect(overlay).toBeLessThan(themePicker ?? Number.NEGATIVE_INFINITY);
  });

  it('shows and dismisses a fatal error above the empty experiment log', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openPane('perf');
    const fatalBanner: NonNullable<SessionState['errorBanner']> = {
      title: 'Run failed',
      message: 'RuntimeError: app-server initialization was denied\nOperation not permitted',
      detail: 'The run server exited before accepting a client.',
      hint: 'Check the startup log and retry.',
      diagnosticId: 'diagnostic-1',
      severity: 'fatal',
      scope: 'run',
      agentKind: 'orchestrator',
      roundLabel: 'round-1-pre',
      invocationId: null,
      count: 1,
    };
    controller.publish({
      ...controller.state,
      experimentLog: {entries: [], selectedId: null, pending: false, error: null},
      errorBanner: fatalBanner,
    });

    let frame = await testRenderer.waitForFrame(value =>
      value.includes('app-server initialization was denied'),
    );
    expect(frame).toContain('Run failed · orchestrator · round-1-pre');
    expect(frame).toContain('Operation not permitted');
    expect(frame).toContain('Detail: The run server exited before accepting a client.');
    expect(frame).toContain('Hint: Check the startup log and retry.');
    expect(frame).toContain('Experiments');

    expect(frame).toContain('[× Dismiss] · Esc: dismiss · Ctrl+PgUp/PgDn: scroll');

    const lines = frame.split('\n');
    const row = lines.findIndex(line => line.includes('[× Dismiss]'));
    const column = (lines[row]?.indexOf('[× Dismiss]') ?? 0) + 2;
    await testRenderer.mockMouse.click(column, row);
    frame = await frameAfter(testRenderer);
    expect(controller.state.errorBanner).toBeNull();
    expect(frame).not.toContain('app-server initialization was denied');
    expect(frame).toContain('Experiments');

    controller.publish({
      ...controller.state,
      errorBanner: {...fatalBanner, title: 'Request failed', message: 'A later failure.'},
    });
    frame = await testRenderer.waitForFrame(value => value.includes('A later failure.'));
    expect(frame).toContain('Esc: dismiss');

    testRenderer.mockInput.pressKey('ESCAPE');
    frame = await frameAfterEscape(testRenderer);
    expect(controller.state.errorBanner).toBeNull();
    expect(controller.state.layout.right?.view).toBe('perf');
    expect(frame).not.toContain('A later failure.');
  });

  it('draws the rounds as one tab row between the header and the round view', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 18});
    const activeStartedAt = new Date(Date.now() - 65_000).toISOString();
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        rounds: [
          {number: 1, status: 'completed'},
          {
            number: 2,
            status: 'active',
            startedAt: activeStartedAt,
            activeAgentStarts: {'judge:judge-1': activeStartedAt},
          },
          {number: 3, status: 'failed'},
        ],
        transcript: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('r2'));
    const root = testRenderer.renderer.root;
    const header = root.findDescendantById('header-frame');
    const tabs = root.findDescendantById('round-tabs');
    const agents = root.findDescendantById('agent-map');
    const transcript = root.findDescendantById('viewport');
    if (!header || !tabs || !agents || !transcript) throw new Error('round view was missing');

    // One row straight under the header, and both panes start under it.
    expect(tabs.visible).toBe(true);
    expect(tabs.y).toBe(header.y + header.height);
    expect([agents.y, transcript.y]).toEqual([tabs.y + 1, tabs.y + 1]);
    expect(root.findDescendantById('round-rail')).toBeUndefined();
    // Each tab carries its number and outcome glyph; the running round, open
    // by default, carries its time measured from its agent start.
    expect(frameRows(frame)[tabs.y]).toMatch(/r1 ✓.*▎ r2 ⟳ 1m \d+s.*r3 ✗ fail/);
    // A glyph, never a spinner that reads as motion frozen.
    expect(frame).not.toMatch(/[◐◓◑◒]/);
  });

  it('heads the agent strip with the elapsed time of the running round', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 18});
    const activeStartedAt = new Date(Date.now() - 65_000).toISOString();
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        rounds: [
          {
            number: 2,
            status: 'active',
            startedAt: activeStartedAt,
            activeAgentStarts: {'judge:judge-1': activeStartedAt},
          },
        ],
        phases: [{kind: 'judge', status: 'active', roundNumber: 2, roundLabel: 'round-2-judge'}],
        transcript: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('Round 2 flow'));

    expect(frame).toMatch(/Round 2 flow · 1m \d+s/);
  });

  it('draws the round as a left-to-right graph when the terminal has room', async () => {
    const testRenderer = await createTestRenderer({width: 150, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        rounds: [{number: 1, status: 'active'}],
        phases: [
          {kind: 'orchestrator', status: 'completed', roundNumber: 1, roundLabel: 'round-1-plan'},
          {
            kind: 'implementer',
            status: 'active',
            roundNumber: 1,
            roundLabel: 'round-1-implementer',
          },
          {kind: 'judge', status: 'pending', roundNumber: 1, roundLabel: 'round-1-judge'},
        ],
        transcript: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('orchestrator'));

    // Stages share a row and are joined by edges, rather than stacked with ↓.
    const stageRow = frame
      .split('\n')
      .find(line => line.includes('orchestrator') && line.includes('implementer'));
    expect(stageRow).toBeDefined();
    expect(stageRow).toContain('judge');
    // A 40% pane at 150 columns names every agent of this round in full.
    expect(stageRow).not.toContain('…');
    expect(frame).toContain('▶');
    // The stacked strip's connector, not the arrow glyphs in the key help.
    expect(frame).not.toContain('        ↓');
  });

  it('walks the transcript with the arrow keys and filters it to a clicked agent', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 26});
    const controller = new FakeController({
      ...initialSessionState(),
      selectedRound: 1,
      core: {
        ...initialSessionState().core,
        rounds: [{number: 1, status: 'active'}],
        phases: [
          {kind: 'implementer', status: 'completed', roundNumber: 1, roundLabel: 'round-1-impl'},
          {kind: 'judge', status: 'active', roundNumber: 1, roundLabel: 'round-1-judge'},
        ],
        activeExecutions: {
          'judge-1': {
            executionId: 'judge-1',
            agentKind: 'judge',
            roundLabel: 'round-1-judge',
            roundNumber: 1,
            stage: 'evaluation',
            attempt: 1,
            assignment: 'Evaluate the candidate',
            startedAt: new Date().toISOString(),
            activity: {mode: 'thinking', summary: 'Checking the diff'},
          },
        },
        transcript: [
          {
            id: 'e1',
            kind: 'assistant',
            label: 'implementer · round-1',
            content: 'edited the kernel',
            agentKind: 'implementer',
            roundNumber: 1,
          },
          {
            id: 'e2',
            kind: 'assistant',
            label: 'judge · round-1',
            content: 'checking the diff',
            agentKind: 'judge',
            roundNumber: 1,
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    // A phase is running, but individual turn cards do not claim ownership of
    // that activity because the transcript may be filtered to another agent.
    const live = await testRenderer.waitForFrame(value => value.includes('checking the diff'));
    expect(live).toContain('Judge · Working');
    expect(live).not.toContain('Judge · Checking the diff');

    // Arrows put a cursor on an entry without touching the input.
    testRenderer.mockInput.pressKey('ARROW_UP');
    const cursored = await frameAfter(testRenderer);
    expect(controller.state.selectedEntryId).not.toBeNull();
    expect(cursored).toContain('▸');

    // Selecting an agent filters the transcript to that agent's turns.
    controller.selectAgent('implementer');
    const filtered = await frameAfter(testRenderer);
    expect(filtered).toContain('edited the kernel');
    expect(filtered).not.toContain('checking the diff');
    // A completed filtered transcript must not inherit another agent's live
    // activity. The global working indicator remains available on the experiment log.
    expect(filtered).not.toContain('Judge · Working');
  });

  it('summarizes concurrent executions and disappears when they finish', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 24});
    const base = initialSessionState();
    const controller = new FakeController({
      ...base,
      selectedRound: 2,
      core: {
        ...base.core,
        activeExecutions: {
          implementer: {
            executionId: 'implementer',
            agentKind: 'implementer',
            roundLabel: 'round-2-implementer',
            roundNumber: 2,
            stage: 'implementation',
            attempt: 1,
            assignment: 'Implement the queue',
            startedAt: new Date().toISOString(),
            activity: {mode: 'tool', summary: 'Running queue tests', tool: 'Bash'},
          },
          reviewer: {
            executionId: 'reviewer',
            agentKind: 'reviewer',
            roundLabel: 'round-2-review',
            roundNumber: 2,
            stage: 'review',
            attempt: 1,
            assignment: 'Review the diff',
            startedAt: new Date().toISOString(),
            activity: {mode: 'thinking', summary: 'Inspecting the diff'},
          },
        },
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const active = await testRenderer.waitForFrame(value => value.includes('2 agents active'));
    expect(active).toContain('Implementer: Working');
    expect(active).toContain('Reviewer: Working');
    expect(active).not.toContain('Running queue tests');
    expect(active).not.toContain('Inspecting the diff');

    controller.publish({
      ...controller.state,
      core: {
        ...controller.state.core,
        activeExecutions: {},
      },
    });
    const finished = await frameAfter(testRenderer);
    expect(finished).not.toContain('agents active');
    expect(finished).not.toContain('Running queue tests');
  });

  it('shows activity when an execution starts after its agent conversation is opened', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      selectedRound: 1,
      selectedAgentKind: 'implementer',
      core: {
        ...initialSessionState().core,
        rounds: [{number: 1, status: 'active'}],
        phases: [
          {
            kind: 'implementer',
            status: 'pending',
            roundNumber: 1,
            roundLabel: 'round-1-implementer',
          },
        ],
        transcript: [
          {
            id: 'prompt',
            kind: 'prompt',
            content: 'Implement the queue',
            agentKind: 'implementer',
            roundNumber: 1,
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    const idle = await testRenderer.waitForFrame(value => value.includes('Implement the queue'));
    expect(idle).not.toContain('Implementer · Working');

    controller.publish({
      ...controller.state,
      core: {
        ...controller.state.core,
        activeExecutions: {
          'impl-1': {
            executionId: 'impl-1',
            agentKind: 'implementer',
            roundLabel: 'round-1-implementer',
            roundNumber: 1,
            stage: 'implementation',
            attempt: 1,
            assignment: 'Implement the queue',
            startedAt: new Date().toISOString(),
            activity: {mode: 'responding', summary: 'Editing the queue'},
          },
        },
      },
    });

    const active = await testRenderer.waitForFrame(value =>
      value.includes('Implementer · Working'),
    );
    expect(active).toMatch(/[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏] Implementer · Working · \d+s/);
    expect(active).not.toContain('Editing the queue');
    const activityLine = active.split('\n').find(line => line.includes('Implementer · Working'));
    expect(activityLine?.indexOf('Implementer')).toBeGreaterThan(20);
    const lines = active.split('\n');
    const promptLine = lines.findIndex(line => line.includes('Implement the queue'));
    const activityLineIndex = lines.findIndex(line => line.includes('Implementer · Working'));
    const helpLine = lines.findIndex(line => line.includes('[/] or click: round'));
    // The command box is the foot of the transcript pane, so the pane's own
    // bottom border is below it and the row the activity line is measured
    // against is where that box starts.
    const commandTop = lines.findIndex(
      (line, index) => index > activityLineIndex && /[╭┏][─━] [▸ ] Command /.test(line),
    );
    const viewportBottomBorder = lines.findIndex(
      (line, index) =>
        index > commandTop && index < helpLine && FRAME_BOTTOM_RIGHT.test(line.trimEnd()),
    );
    expect(activityLineIndex).toBeGreaterThan(promptLine);
    expect(FRAME_VERTICAL.test(activityLine?.trimEnd() ?? '')).toBe(true);
    expect(commandTop).toBeGreaterThan(activityLineIndex);
    expect(viewportBottomBorder).toBeGreaterThan(commandTop);
    expect(viewportBottomBorder).toBeLessThan(helpLine);
    const transcriptColumn = Math.max(0, (activityLine?.indexOf('Implementer') ?? 2) - 2);
    expect(
      lines
        .slice(activityLineIndex + 1, commandTop)
        .every(line => line.slice(transcriptColumn).replaceAll(FRAME_VERTICALS, '').trim() === ''),
    ).toBe(true);

    controller.selectAgent('implementer');
    await frameAfter(testRenderer);
    controller.selectAgent('implementer');
    const reopened = await frameAfter(testRenderer);
    expect(reopened).toContain('Implementer · Working');
  });

  it('keeps activity fixed and aligned while a new turn changes the scroll height', async () => {
    const conversation = Array.from({length: 12}, (_, index) => ({
      id: `turn-${index}`,
      kind: 'status' as const,
      content: `recorded turn ${index}`,
      agentKind: 'implementer',
      roundNumber: 1,
    }));
    const testRenderer = await createTestRenderer({width: 100, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      selectedRound: 1,
      selectedAgentKind: 'implementer',
      core: {
        ...initialSessionState().core,
        transcript: conversation,
        rounds: [{number: 1, status: 'active'}],
        activeExecutions: {
          'impl-1': {
            executionId: 'impl-1',
            agentKind: 'implementer',
            roundLabel: 'round-1-implementer',
            roundNumber: 1,
            stage: 'implementation',
            attempt: 1,
            assignment: 'Implement the queue',
            startedAt: new Date().toISOString(),
            activity: {mode: 'thinking', summary: 'Thinking'},
          },
        },
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('Implementer · Working'));

    const frames: string[] = [];
    const captureFrame = (): void => {
      frames.push(testRenderer.captureCharFrame());
    };
    testRenderer.renderer.on(CliRenderEvents.FRAME, captureFrame);
    controller.publish({
      ...controller.state,
      core: {
        ...controller.state.core,
        transcript: [
          ...conversation,
          {
            id: 'new-turn',
            kind: 'assistant',
            content: 'newly rendered turn',
            agentKind: 'implementer',
            roundNumber: 1,
          },
        ],
      },
    });
    await testRenderer.waitForVisualIdle();
    testRenderer.renderer.off(CliRenderEvents.FRAME, captureFrame);

    expect(frames.length).toBeGreaterThan(0);
    const activityRows = frames.map(frame =>
      frame.split('\n').findIndex(line => line.includes('Implementer · Working')),
    );
    expect(activityRows.every(row => row >= 0)).toBe(true);
    expect(new Set(activityRows).size).toBe(1);
    expect(frames.at(-1)).toContain('newly rendered turn');

    const frame = testRenderer.renderer.root.findDescendantById('viewport');
    const scroll = testRenderer.renderer.root.findDescendantById('transcript-scroll');
    const activity = testRenderer.renderer.root.findDescendantById('conversation-activity-bar');
    const firstTurn = testRenderer.renderer.root.findDescendantById('event-turn-0');
    if (frame === undefined || activity === undefined)
      throw new Error('transcript frame was missing');
    if (!(scroll instanceof ScrollBoxRenderable)) throw new Error('transcript was not scrollable');
    expect(scroll.parent).toBe(frame);
    expect(activity?.parent).toBe(frame);
    expect(firstTurn?.x).toBe(activity.x);
  });

  it('keeps working indicators scoped to agent conversations', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        activeExecutions: {
          'impl-1': {
            executionId: 'impl-1',
            agentKind: 'implementer',
            roundLabel: 'round-1-implementer',
            roundNumber: 1,
            stage: 'implementation',
            attempt: 1,
            assignment: 'Implement the queue',
            startedAt: new Date().toISOString(),
            activity: {mode: 'responding', summary: 'Editing the queue'},
          },
        },
      },
    });
    controller.publish({...controller.state, experimentLog: initialSessionState().experimentLog});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await frameAfter(testRenderer);
    expect(frame).toContain('Experiments');
    expect(frame).not.toContain('Implementer · Working');
    expect(frame).not.toContain('Editing the queue');
  });

  it('shows the whole run in the strip and keeps early rounds reachable', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        maxRounds: 100,
        rounds: Array.from({length: 12}, (_, index) => ({
          number: index + 1,
          status: index === 11 ? ('active' as const) : ('completed' as const),
        })),
        phases: [{kind: 'judge', status: 'active', roundNumber: 12, roundLabel: 'round-12-judge'}],
        transcript: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'out'}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('r12'));
    // Rounds the run has not reached are still tabs, and the bar counts the
    // ones it could not fit on either side.
    expect(frame).toMatch(/r1[34]/);
    expect(frame).toMatch(/‹ \d+/);
    expect(frame).toMatch(/\d+ ›/);

    // `[` walks back to the first round, and the bar follows the selection
    // rather than leaving it hidden past the edge.
    let early = frame;
    for (let step = 0; step < 11; step += 1) {
      testRenderer.mockInput.pressKey('[');
      early = await frameAfter(testRenderer);
    }
    expect(controller.state.selectedRound).toBe(1);
    expect(early).toMatch(/▎ ?r1 ✓/);
    expect(early).not.toContain('‹');
  });

  it('leaves brackets and cursor keys to a typed command', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        rounds: [
          {number: 1, status: 'completed' as const},
          {number: 2, status: 'active' as const},
        ],
        transcript: [
          {
            id: 'live',
            kind: 'assistant',
            label: 'Agent',
            content: 'live output',
            roundNumber: 2,
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('live output'));
    const focusBefore = controller.state.roundFocus;

    // With text in the command input, brackets are characters and the cursor
    // keys stay in the input: nothing navigates rounds or moves pane focus.
    await testRenderer.mockInput.typeText('/steer fix arr[0]');
    testRenderer.mockInput.pressKey('ARROW_LEFT');
    const typed = await frameAfter(testRenderer);
    expect(typed).toContain('arr[0]');
    expect(controller.state.selectedRound).toBeNull();
    expect(controller.state.roundFocus).toBe(focusBefore);

    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.submissions.length === 1);
    expect(controller.submissions).toEqual(['/steer fix arr[0]']);

    // With the input empty again, the same key is round navigation.
    testRenderer.mockInput.pressKey('[');
    await frameAfter(testRenderer);
    expect(controller.state.selectedRound).toBe(1);
  });

  it('filters the transcript to an agent node that is clicked', async () => {
    const testRenderer = await createTestRenderer({width: 150, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      selectedRound: 1,
      core: {
        ...initialSessionState().core,
        rounds: [{number: 1, status: 'active'}],
        phases: [
          {kind: 'implementer', status: 'completed', roundNumber: 1, roundLabel: 'round-1-impl'},
          {kind: 'judge', status: 'active', roundNumber: 1, roundLabel: 'round-1-judge'},
        ],
        transcript: [
          {
            id: 'e1',
            kind: 'assistant',
            label: 'implementer',
            content: 'edited the kernel',
            agentKind: 'implementer',
            roundNumber: 1,
          },
          {
            id: 'e2',
            kind: 'assistant',
            label: 'judge',
            content: 'checking the diff',
            agentKind: 'judge',
            roundNumber: 1,
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    const frame = await testRenderer.waitForFrame(value => value.includes('implementer'));

    // Click the node's own label, which is what a pointer lands on.
    const lines = frame.split('\n');
    const row = lines.findIndex(line => line.includes('✓ implementer'));
    const column = (lines[row]?.indexOf('implementer') ?? 0) + 2;
    await testRenderer.mockMouse.click(column, row);
    const filtered = await frameAfter(testRenderer);

    expect(controller.state.selectedAgentKind).toBe('implementer');
    expect(filtered).not.toContain('checking the diff');
  });

  it('moves the round view keys between the graph and the transcript', async () => {
    const testRenderer = await createTestRenderer({width: 150, height: 26});
    const controller = new FakeController({
      ...initialSessionState(),
      selectedRound: 1,
      core: {
        ...initialSessionState().core,
        rounds: [{number: 1, status: 'active'}],
        phases: [
          {kind: 'implementer', status: 'completed', roundNumber: 1, roundLabel: 'round-1-impl'},
          {kind: 'judge', status: 'active', roundNumber: 1, roundLabel: 'round-1-judge'},
        ],
        transcript: [
          {
            id: 'e1',
            kind: 'assistant',
            label: 'implementer',
            content: 'edited the kernel',
            agentKind: 'implementer',
            roundNumber: 1,
          },
          {
            id: 'e2',
            kind: 'assistant',
            label: 'implementer',
            content: 'guarded the tail tile',
            agentKind: 'implementer',
            roundNumber: 1,
          },
          {
            id: 'e3',
            kind: 'assistant',
            label: 'judge',
            content: 'checking the diff',
            agentKind: 'judge',
            roundNumber: 1,
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('implementer'));

    // Left reaches the graph, and the pane says it holds the keys.
    testRenderer.mockInput.pressKey('ARROW_LEFT');
    const onAgents = await frameAfter(testRenderer);
    expect(controller.state.roundFocus).toBe('agents');
    expect(onAgents).toContain('▸ Agents');

    // There, up and down walk the agents rather than the transcript.
    testRenderer.mockInput.pressKey('ARROW_DOWN');
    await frameAfter(testRenderer);
    const firstAgent = controller.state.selectedAgentKind;
    expect(firstAgent).not.toBeNull();
    expect(controller.state.selectedEntryId).toBeNull();

    // Right hands them to the transcript, where they walk its entries instead.
    testRenderer.mockInput.pressKey('ARROW_RIGHT');
    await frameAfter(testRenderer);
    expect(controller.state.roundFocus).toBe('transcript');
    testRenderer.mockInput.pressKey('ARROW_UP');
    await frameAfter(testRenderer);
    expect(controller.state.selectedEntryId).not.toBeNull();
    // The agent picked on the left is still the filter: moving the keys is not
    // the same as giving up the selection.
    expect(controller.state.selectedAgentKind).toBe(firstAgent);

    // And Tab still works after coming back, from where the operator left off.
    testRenderer.mockInput.pressKey('ARROW_LEFT');
    testRenderer.mockInput.pressKey('TAB');
    await frameAfter(testRenderer);
    expect(controller.state.selectedAgentKind).not.toBe(firstAgent);
  });

  /**
   * The rounds are a row above the round view, so its width is the agents pane
   * and the transcript alone: the graph takes 40% of the terminal, never less
   * than names every agent in full, and the transcript the rest. Measured on
   * the laid-out boxes, since a layout regression only shows up there.
   */
  for (const [width, agentsWidth] of [
    [160, 64],
    // 40% of these is under the 63 columns that name every agent in full.
    [150, 63],
    [120, 63],
    // No room for those beside the transcript floor: the stacked list.
    [100, 30],
    [80, 30],
  ] as const) {
    it(`gives the agents ${agentsWidth} and the transcript the rest at ${width} columns`, async () => {
      const testRenderer = await createTestRenderer({width, height: 30});
      const controller = new FakeController(threeStageRound());
      const app = createOpenTuiApp(testRenderer.renderer, controller);
      registerCleanup(testRenderer.renderer, app);
      await testRenderer.waitForFrame(value => value.includes('live output'));

      const root = testRenderer.renderer.root;
      const agents = root.findDescendantById('agent-map');
      const transcript = root.findDescendantById('viewport');
      if (agents === undefined || transcript === undefined) {
        throw new Error('round view was missing a pane');
      }
      expect(root.findDescendantById('round-rail')).toBeUndefined();
      expect(agents.width).toBe(agentsWidth);
      expect(transcript.x).toBe(agents.x + agents.width);
      expect(transcript.width).toBe(width - agentsWidth);
      expect(transcript.width).toBeGreaterThanOrEqual(TRANSCRIPT_MIN);
    });
  }

  for (const width of [150, 120, 100]) {
    for (const selected of [null, 'orchestrator']) {
      it(`names every agent in full at ${width} columns, ${selected ?? 'none'} selected`, async () => {
        const pane = await agentPaneText(width, {
          ...threeStageRound(),
          selectedAgentKind: selected,
        });

        for (const name of ['orchestrator', 'implementer', 'judge']) expect(pane).toContain(name);
        expect(pane).not.toContain('…');
      });
    }
  }

  it('stacks the agents, names in full, just below the width the graph needs', async () => {
    // 105 = the 63 columns that name every agent in full + TRANSCRIPT_MIN.
    const below = await agentPaneText(104, {
      ...threeStageRound(),
      selectedAgentKind: 'orchestrator',
    });
    expect(below).not.toContain('▶');
    expect(below).toContain('› ✓ orchestrator');
    expect(below).toContain('● implementer');
    expect(below).toContain('○ judge');

    const at = await agentPaneText(105, threeStageRound());
    expect(at).toContain('▶');
  });

  it("reads the tabs' measured delta from the experiment log, not the design log", async () => {
    // Per-round stage facts, the measured delta included, cross the protocol on
    // `HypothesisRound`; the design log carries only each round's file list. The
    // tabs join by round number so they and the experiments table cannot report
    // different numbers for the same round.
    const testRenderer = await createTestRenderer({width: 120, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      selectedRound: 2,
      hypothesisScope: {id: 'H-01', label: 'H-01 · r1-r2', title: 'H-01', rounds: [1, 2]},
      core: {
        ...initialSessionState().core,
        rounds: [
          {number: 1, status: 'completed'},
          {number: 2, status: 'completed'},
        ],
        transcript: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('▎ r2'));
    controller.publish({
      ...controller.state,
      experimentLog: {
        entries: [
          logEntry('H-01', 1, 2, {
            rounds: [
              {round: 1, passed: true, reviewed: true, perf_delta_pct: -3.5},
              {round: 2, passed: true, reviewed: true, perf_delta_pct: 12.4},
            ],
          }),
        ],
        selectedId: null,
        pending: false,
        error: null,
      },
    });
    const frame = await testRenderer.waitForFrame(value => value.includes('%'));

    expect(frame).toContain('-3.5%');
    expect(frame).toContain('+12%');
  });

  it('steps Left and Right between the agents and the transcript, clamped at both', async () => {
    const testRenderer = await createTestRenderer({width: 150, height: 30});
    const controller = new FakeController(threeStageRound());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('live output'));

    const focus: RoundFocus[] = [];
    for (const key of [
      'ARROW_LEFT',
      'ARROW_LEFT',
      'ARROW_RIGHT',
      'ARROW_RIGHT',
      'ARROW_LEFT',
    ] as const) {
      testRenderer.mockInput.pressKey(key);
      await frameAfter(testRenderer);
      focus.push(controller.state.roundFocus);
    }
    // The tabs are not a pane, so nothing sits left of the agents.
    expect(focus).toEqual(['agents', 'agents', 'transcript', 'transcript', 'agents']);

    // Up and Down there walk the agents; only `[` and `]` change the round.
    testRenderer.mockInput.pressKey('ARROW_DOWN');
    await frameAfter(testRenderer);
    expect(controller.state.selectedRound).toBe(3);
    expect(controller.state.selectedAgentKind).toBe('judge');
  });

  it('shows the tabs only while the whole round view is on screen', async () => {
    const testRenderer = await createTestRenderer({width: 150, height: 30});
    const controller = new FakeController(threeStageRound());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('live output'));
    expect(tabsVisible(testRenderer)).toBe(true);

    // A zoomed pane has the content row to itself.
    testRenderer.mockInput.pressKey('F4');
    await frameAfter(testRenderer);
    expect(controller.state.layout.zoomedPane).toBe('transcript');
    expect(tabsVisible(testRenderer)).toBe(false);
    testRenderer.mockInput.pressKey('F4');
    await frameAfter(testRenderer);
    expect(tabsVisible(testRenderer)).toBe(true);

    // A split gives the right of the row to a visualization.
    await controller.openPane('perf');
    await frameAfter(testRenderer);
    expect(tabsVisible(testRenderer)).toBe(false);
    // Too narrow to split, the visualization floats over the round instead and
    // the round keeps its tabs: width alone never hides them.
    testRenderer.renderer.resize(MIN_SPLIT_WIDTH - 1, 30);
    await frameAfter(testRenderer);
    expect(tabsVisible(testRenderer)).toBe(true);
    testRenderer.renderer.resize(150, 30);
    await frameAfter(testRenderer);
    expect(tabsVisible(testRenderer)).toBe(false);

    // The experiment log is the landing view, not a round.
    controller.closePane();
    await controller.openExperimentLog();
    await frameAfter(testRenderer);
    expect(tabsVisible(testRenderer)).toBe(false);
  });

  it('keeps the tabs hidden under a zoom while the live round ticks', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 24});
    const round = threeStageRound();
    const startedAt = new Date(Date.now() - 5_000).toISOString();
    const controller = new FakeController({
      ...round,
      core: {
        ...round.core,
        rounds: [
          ...round.core.rounds.slice(0, 2),
          {
            number: 3,
            status: 'active',
            startedAt,
            activeAgentStarts: {'implementer:implementer-1': startedAt},
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('live output'));
    testRenderer.mockInput.pressKey('F4');
    await frameAfter(testRenderer);
    expect(tabsVisible(testRenderer)).toBe(false);

    // The live tab re-lays the bar out every second, and that tick must not
    // bring the bar back over the zoomed pane.
    await new Promise(resolve => setTimeout(resolve, 1100));
    await frameAfter(testRenderer);
    expect(tabsVisible(testRenderer)).toBe(false);
  });

  it('bills the tab row to the agent graph so a stacked stage stays in its pane', async () => {
    // Stacked nodes fit in steps of seven rows, so seven consecutive heights
    // include one where the tab row decides whether another node fits.
    for (let height = 30; height < 37; height += 1) {
      const testRenderer = await createTestRenderer({width: 150, height});
      const base = initialSessionState();
      const controller = new FakeController({
        ...base,
        selectedRound: 1,
        core: {
          ...base.core,
          rounds: [{number: 1, status: 'active'}],
          phases: [
            {kind: 'orchestrator', status: 'completed', roundNumber: 1, roundLabel: 'round-1-pre'},
            ...Array.from({length: 8}, (_, index) => ({
              kind: 'implementer',
              status: index === 7 ? ('active' as const) : ('interrupted' as const),
              roundNumber: 1,
              roundLabel: `round-1-retry-${index + 1}-implementer`,
              executionId: `e${index}`,
            })),
            {kind: 'judge', status: 'pending', roundNumber: 1, roundLabel: null},
          ],
          transcript: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
        },
      });
      const app = createOpenTuiApp(testRenderer.renderer, controller);
      registerCleanup(testRenderer.renderer, app);
      await testRenderer.waitForFrame(value => value.includes('↑'));

      const root = testRenderer.renderer.root;
      const content = root.findDescendantById('agent-map-content');
      const heading = root.findDescendantById('agent-map-heading');
      const canvas = root.findDescendantById('agent-graph-canvas');
      if (!content || !heading || !canvas) throw new Error('agent graph was missing');
      const nodes = canvas.getChildren().filter(child => child instanceof BoxRenderable);
      expect(nodes.length).toBeGreaterThan(0);
      // Every node sits under the heading and inside the pane's content, so it
      // shares no row with the overflow count or the pane's border.
      for (const node of nodes) {
        expect({height, top: node.y > heading.y}).toEqual({height, top: true});
        expect({height, bottom: node.y + node.height <= content.y + content.height}).toEqual({
          height,
          bottom: true,
        });
      }
    }
  });

  it('focuses round panes from blank and interactive click targets', async () => {
    const testRenderer = await createTestRenderer({width: 150, height: 26});
    const controller = new FakeController({
      ...initialSessionState(),
      experimentLog: null,
      selectedRound: 1,
      core: {
        ...initialSessionState().core,
        rounds: [{number: 1, status: 'active'}],
        phases: [
          {kind: 'implementer', status: 'completed', roundNumber: 1, roundLabel: 'round-1-impl'},
          {kind: 'judge', status: 'active', roundNumber: 1, roundLabel: 'round-1-judge'},
        ],
        transcript: [
          {
            id: 'e1',
            kind: 'assistant',
            label: 'implementer',
            content: 'edited the kernel',
            roundNumber: 1,
          },
          {
            id: 'e2',
            kind: 'assistant',
            label: 'judge',
            content: 'checking the diff',
            roundNumber: 1,
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    let frame = await testRenderer.waitForFrame(value => value.includes('edited the kernel'));

    // The graph heading has no action of its own. Clicking it still focuses
    // the containing pane rather than requiring a click on an agent.
    let lines = frame.split('\n');
    let row = lines.findIndex(line => line.includes('Round 1'));
    let column = (lines[row]?.indexOf('Round 1') ?? 0) + 2;
    await testRenderer.mockMouse.click(column, row);
    frame = await frameAfter(testRenderer);
    expect(controller.state.roundFocus).toBe('agents');
    expect(frame).toContain('▸ Agents');

    // Entering the pane selects its active agent. Clicking that inner node
    // keeps Agents focused and clears the filter, preserving node semantics.
    lines = frame.split('\n');
    row = lines.findIndex(line => line.includes('● judge'));
    column = (lines[row]?.indexOf('judge') ?? 0) + 2;
    await testRenderer.mockMouse.click(column, row);
    frame = await frameAfter(testRenderer);
    expect(controller.state.roundFocus).toBe('agents');
    expect(controller.state.selectedAgentKind).toBeNull();

    // A turn card has its own selection action. It composes pane focus with
    // that action, and the next arrow is consequently routed to transcript.
    lines = frame.split('\n');
    row = lines.findIndex(line => line.includes('edited the kernel'));
    column = (lines[row]?.indexOf('edited the kernel') ?? 0) + 2;
    await testRenderer.mockMouse.click(column, row);
    await frameAfter(testRenderer);
    expect(controller.state.roundFocus).toBe('transcript');
    expect(controller.state.selectedEntryId).toBe('e1');
    testRenderer.mockInput.pressKey('ARROW_DOWN');
    await frameAfter(testRenderer);
    expect(controller.state.selectedEntryId).toBe('e2');

    // Agent nodes likewise keep their selection behavior while taking focus.
    frame = testRenderer.captureCharFrame();
    lines = frame.split('\n');
    row = lines.findIndex(line => line.includes('✓ implementer'));
    column = (lines[row]?.indexOf('implementer') ?? 0) + 2;
    await testRenderer.mockMouse.click(column, row);
    await frameAfter(testRenderer);
    expect(controller.state.roundFocus).toBe('agents');
    expect(controller.state.selectedAgentKind).toBe('implementer');
    testRenderer.mockInput.pressKey('TAB');
    await frameAfter(testRenderer);
    expect(controller.state.selectedAgentKind).toBe('judge');
  });

  it('marks the chat input as focused when it is clicked', async () => {
    const testRenderer = await createTestRenderer({width: 150, height: 22});
    const controller = new FakeController({...initialSessionState(), chatDockFits: true});
    controller.experiments = [
      logEntry('H-01', 1, 1, {
        claim: 'fuse the epilogue',
        rounds: [{round: 1, passed: true, reviewed: true}],
      }),
    ];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    const docked = await testRenderer.waitForFrame(value =>
      value.includes('Ask about this experiment'),
    );
    // The hint says the keys are elsewhere, which is the state being reported.
    expect(docked).toContain('Ctrl+W to type here');

    // Click the box the operator types into, not the conversation above it.
    const lines = docked.split('\n');
    const row = lines.findIndex(line => line.includes('Ask about this experiment'));
    const column = (lines[row]?.indexOf('Ask about this experiment') ?? 0) + 2;
    await testRenderer.mockMouse.click(column, row);
    const focused = await frameAfter(testRenderer);

    expect(controller.state.layout.focus).toBe('chat');
    // And the surface says so: clicking must move the treatment, not only the
    // cursor. It moves onto the pane frame, since the composer is a box inside
    // that pane rather than a pane of its own.
    expect(focused).not.toContain('Ctrl+W to type here');
    const theme = resolveTheme('dark');
    const borders = paneBorders(testRenderer);
    expect(borders['▸ Experiment chat']).toBe(theme.borderFocus);
    expect(borders['Message']).toBe(theme.border);
  });

  it('gives the chat the keys when it is clicked, not only on Ctrl+W', async () => {
    const testRenderer = await createTestRenderer({width: 150, height: 22});
    const controller = new FakeController({
      ...initialSessionState(),
      chatDockFits: true,
      chatConversation: [
        {id: 'a1', kind: 'assistant', label: 'Answer', content: 'the epilogue was fused'},
      ],
    });
    controller.experiments = [
      logEntry('H-01', 1, 1, {
        claim: 'fuse the epilogue',
        rounds: [{round: 1, passed: true, reviewed: true}],
      }),
    ];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    const docked = await testRenderer.waitForFrame(value => value.includes('Experiment chat'));
    expect(controller.state.layout.focus).toBe('left');

    // Click inside the chat's body, which is where a pointer actually lands.
    const row = docked.split('\n').findIndex(line => line.includes('the epilogue was fused'));
    const column = docked.split('\n')[row]?.indexOf('the epilogue') ?? 0;
    await testRenderer.mockMouse.click(column, row);
    await frameAfter(testRenderer);

    expect(controller.state.layout.focus).toBe('chat');
  });

  it('says so when a round has not run instead of looking broken', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      selectedRound: 9,
      core: {
        ...initialSessionState().core,
        maxRounds: 20,
        rounds: [{number: 1, status: 'completed'}],
        phases: [{kind: 'judge', status: 'completed', roundNumber: 1, roundLabel: 'round-1-judge'}],
        transcript: [
          {id: 'e1', kind: 'assistant', label: 'judge', content: 'done', roundNumber: 1},
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('has not run yet'));
    expect(frame).toContain('Round 9 has not run yet.');
    // The tabs still show it as a round of this run, marked as the one open.
    expect(frame).toMatch(/▎ r9 ·/);
  });

  it('unwinds the modal chat over a visualization one Escape at a time', async () => {
    const testRenderer = await createTestRenderer({width: 130, height: 22});
    const controller = new FakeController({
      ...initialSessionState(),
      hypothesisScope: {id: 'H-01', label: 'H-01 · r1', title: 'H-01', rounds: [1]},
      selectedRound: 1,
      core: {
        ...initialSessionState().core,
        rounds: [{number: 1, status: 'active'}],
        phases: [{kind: 'judge', status: 'active', roundNumber: 1, roundLabel: 'round-1-judge'}],
        transcript: [
          {id: 'e1', kind: 'assistant', label: 'judge', content: 'weighing it', roundNumber: 1},
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openPane('perf');
    controller.publish({...controller.state, chatOpen: true});
    let frame = await frameAfter(testRenderer);
    expect(frame).toContain('1200');

    // The modal chat is the innermost layer: the first Escape closes only it,
    // and the visualization behind it stays exactly where it was.
    testRenderer.mockInput.pressKey('ESCAPE');
    frame = await frameAfterEscape(testRenderer);
    expect(controller.state.chatOpen).toBe(false);
    expect(controller.state.layout.right).not.toBeNull();
    expect(controller.state.layout.focus).toBe('right');
    expect(controller.state.hypothesisScope).not.toBeNull();
    expect(frame).toContain('1200');

    // The second Escape is the pane's own: it closes now, leaving the round
    // trajectory that was always behind both of them.
    testRenderer.mockInput.pressKey('ESCAPE');
    frame = await frameAfterEscape(testRenderer);
    expect(controller.state.layout.right).toBeNull();
    expect(controller.state.layout.focus).toBe('left');
    expect(controller.state.hypothesisScope).not.toBeNull();
    expect(frame).not.toContain('1200');
  });

  it('closes the chat alone on Escape when no pane is behind it', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 20});
    const controller = new FakeController({...initialSessionState(), chatOpen: true});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await frameAfter(testRenderer);

    testRenderer.mockInput.pressKey('ESCAPE');
    await frameAfterEscape(testRenderer);

    expect(controller.state.chatOpen).toBe(false);
    expect(controller.state.layout.right).toBeNull();
    expect(controller.state.layout.focus).toBe('left');
  });

  it('closes the pane alone on Escape when no chat is open', async () => {
    const testRenderer = await createTestRenderer({width: 130, height: 22});
    const controller = new FakeController({
      ...initialSessionState(),
      hypothesisScope: {id: 'H-01', label: 'H-01 · r1', title: 'H-01', rounds: [1]},
      selectedRound: 1,
      core: {
        ...initialSessionState().core,
        rounds: [{number: 1, status: 'active'}],
        phases: [{kind: 'judge', status: 'active', roundNumber: 1, roundLabel: 'round-1-judge'}],
        transcript: [
          {id: 'e1', kind: 'assistant', label: 'judge', content: 'weighing it', roundNumber: 1},
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openPane('perf');
    await frameAfter(testRenderer);
    expect(controller.state.layout.focus).toBe('right');

    testRenderer.mockInput.pressKey('ESCAPE');
    await frameAfterEscape(testRenderer);

    expect(controller.state.layout.right).toBeNull();
    expect(controller.state.chatOpen).toBe(false);
    expect(controller.state.layout.focus).toBe('left');
    expect(controller.state.hypothesisScope).not.toBeNull();
  });

  it('closes Help over a visualization before closing the visualization', async () => {
    const testRenderer = await createTestRenderer({width: 130, height: 22});
    const controller = new FakeController({
      ...initialSessionState(),
      hypothesisScope: {id: 'H-01', label: 'H-01 · r1', title: 'H-01', rounds: [1]},
      selectedRound: 1,
      core: {
        ...initialSessionState().core,
        rounds: [{number: 1, status: 'active'}],
        phases: [{kind: 'judge', status: 'active', roundNumber: 1, roundLabel: 'round-1-judge'}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openPane('perf');
    controller.publish({
      ...controller.state,
      overlay: {kind: 'help', content: 'Available commands'},
    });
    await testRenderer.waitForFrame(value => value.includes('Available commands'));

    // Help is foreground. Escape dismisses it without closing the Performance pane.
    testRenderer.mockInput.pressKey('ESCAPE');
    await testRenderer.waitForFrame(value => !value.includes('Available commands'));
    expect(controller.state.overlay).toBeNull();
    expect(controller.state.layout.right).not.toBeNull();
    expect(controller.state.layout.focus).toBe('right');

    // Once Help is gone, the next Escape closes the pane.
    testRenderer.mockInput.pressKey('ESCAPE');
    await frameAfterEscape(testRenderer);
    expect(controller.state.layout.right).toBeNull();
    expect(controller.state.layout.focus).toBe('left');
  });

  it('hands the keys back when the pane holding them closes', async () => {
    const testRenderer = await createTestRenderer({width: 130, height: 22});
    const controller = new FakeController({
      ...initialSessionState(),
      selectedRound: 1,
      core: {
        ...initialSessionState().core,
        rounds: [{number: 1, status: 'active'}],
        phases: [{kind: 'judge', status: 'active', roundNumber: 1, roundLabel: 'round-1-judge'}],
        transcript: [
          {id: 'e1', kind: 'assistant', label: 'judge', content: 'weighing it', roundNumber: 1},
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openPane('perf');
    controller.focusPane('right');
    await frameAfter(testRenderer);
    controller.closePane();
    await frameAfter(testRenderer);

    // Focus cannot be left pointing at a pane that is gone, or every key after
    // it goes nowhere and the client looks frozen.
    expect(controller.state.layout.focus).toBe('left');
    testRenderer.mockInput.pressKey(']');
    await frameAfter(testRenderer);
    expect(controller.state.selectedRound).toBe(1);
  });

  it('keeps the chat in one conversation across the log and a round', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 22});
    const controller = new FakeController({
      ...initialSessionState(),
      chatConversation: [
        {id: 'q1', kind: 'user', label: 'You', content: 'what changed?'},
        {id: 'a1', kind: 'assistant', label: 'Answer', content: 'the epilogue was fused'},
      ],
      chatOpen: true,
      core: {
        ...initialSessionState().core,
        rounds: [{number: 1, status: 'active'}],
        phases: [{kind: 'judge', status: 'active', roundNumber: 1, roundLabel: 'round-1-judge'}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    // The same exchange is on screen inside a round, not only on the log.
    const frame = await testRenderer.waitForFrame(value =>
      value.includes('the epilogue was fused'),
    );
    expect(frame).toContain('what changed?');
  });

  it('draws a round with many agents per stage without overlap', async () => {
    const testRenderer = await createTestRenderer({width: 150, height: 34});
    const phase = (kind: string, status: 'completed' | 'active' | 'pending', index: number) => ({
      kind,
      status,
      roundNumber: 1,
      roundLabel: `round-1-${kind}`,
      invocationId: `${kind}-${index}`,
    });
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        rounds: [{number: 1, status: 'active'}],
        phases: [
          phase('orchestrator', 'completed', 0),
          phase('implementer', 'completed', 1),
          phase('implementer', 'active', 2),
          phase('implementer', 'active', 3),
          phase('judge', 'pending', 4),
          phase('judge', 'pending', 5),
          phase('profiler', 'pending', 6),
        ],
        transcript: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'out'}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    // The last column is drawn once the whole graph is.
    const frame = await testRenderer.waitForFrame(value => value.includes('profiler'));

    expect(frame).toContain('7 agents');
    expect(frame).toContain('2 active');
    // Every stage is drawn, named in full, and the fan-out rows do not
    // collapse onto each other.
    expect(frame).toContain('orchestrator');
    const nodeRows = frame.split('\n').filter(line => line.includes('implementer'));
    expect(nodeRows.length).toBeGreaterThanOrEqual(3);
    expect(frame).toContain('▶');
  });

  it('falls back to the stacked strip when the terminal is too narrow for a graph', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 30});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        rounds: [{number: 1, status: 'active'}],
        phases: [
          {kind: 'orchestrator', status: 'completed', roundNumber: 1, roundLabel: 'round-1-plan'},
          {
            kind: 'implementer',
            status: 'active',
            roundNumber: 1,
            roundLabel: 'round-1-implementer',
          },
          {kind: 'judge', status: 'pending', roundNumber: 1, roundLabel: 'round-1-judge'},
        ],
        transcript: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('orchestrator'));

    expect(frame).toContain('        ↓');
    expect(frame).not.toContain('▶');
  });

  it('shows each graph node’s agent harness and model, when known', async () => {
    const testRenderer = await createTestRenderer({width: 150, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        rounds: [{number: 1, status: 'active'}],
        phases: [
          {
            kind: 'orchestrator',
            status: 'completed',
            roundNumber: 1,
            roundLabel: 'round-1-plan',
            provider: 'codex',
            model: 'gpt-5.1-codex-max',
          },
          {
            kind: 'implementer',
            status: 'active',
            roundNumber: 1,
            roundLabel: 'round-1-implementer',
          },
        ],
        transcript: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('orchestrator'));

    // Graph nodes are narrow enough that the label truncates like every other
    // node line; this stays inside the kept prefix regardless of node width.
    expect(frame).toContain('Codex (GPT');
    // The implementer node carries no runtime identity, so its row stays
    // blank rather than reusing the orchestrator's label.
    const implementerRow = frame.split('\n').find(line => line.includes('implementer'));
    expect(implementerRow).toBeDefined();
  });

  it('shows the stacked strip’s runtime label when the terminal is too narrow for a graph', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 30});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        rounds: [{number: 1, status: 'active'}],
        phases: [
          {
            kind: 'orchestrator',
            status: 'completed',
            roundNumber: 1,
            roundLabel: 'round-1-plan',
            // Short enough to stay on one line in the narrow stacked column;
            // the wrapping case for a long label is covered elsewhere.
            provider: 'codex',
            model: 'gpt-5',
          },
          {
            kind: 'implementer',
            status: 'active',
            roundNumber: 1,
            roundLabel: 'round-1-implementer',
          },
          {kind: 'judge', status: 'pending', roundNumber: 1, roundLabel: 'round-1-judge'},
        ],
        transcript: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('orchestrator'));

    // Confirms the stacked (non-graph) layout is in play, as in the sibling
    // narrow-terminal test above.
    expect(frame).toContain('        ↓');
    expect(frame).toContain('Codex (GPT 5)');
  });

  it('holds the agent-active elapsed time of a finished round', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 18});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        rounds: [
          {
            number: 1,
            status: 'completed',
            // 60s of wall clock with a 15s gap where no agent was running.
            agentIntervals: [
              {startedAt: '2026-01-01T00:00:00Z', finishedAt: '2026-01-01T00:00:30Z'},
              {startedAt: '2026-01-01T00:00:45Z', finishedAt: '2026-01-01T00:01:00Z'},
            ],
          },
        ],
        phases: [{kind: 'judge', status: 'completed', roundNumber: 1, roundLabel: 'round-1-judge'}],
        transcript: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('Round 1 flow'));

    expect(frame).toContain('Round 1 flow · 45s');
  });

  it('omits the elapsed time for a round with no recorded agent time', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 18});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        rounds: [{number: 1, status: 'completed'}],
        phases: [{kind: 'judge', status: 'completed', roundNumber: 1, roundLabel: 'round-1-judge'}],
        transcript: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('Round 1 flow'));

    expect(frame).not.toContain('Round 1 flow ·');
  });

  it('submits typed commands when Enter is pressed', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('/help');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.submissions.length === 1);
    expect(controller.submissions).toEqual(['/help']);
  });

  it('does nothing when Enter is pressed on an empty command box', async () => {
    // Reproduces #564: a round with no selected expandable tool result, so no
    // pane action consumes Enter, and the box has never been typed into.
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    testRenderer.mockInput.pressEnter();
    await frameAfter(testRenderer);
    expect(controller.submissions).toEqual([]);
    expect(controller.state.errorBanner).toBeNull();
  });

  it('does nothing when Enter is pressed with only whitespace typed', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('   ');
    testRenderer.mockInput.pressEnter();
    await frameAfter(testRenderer);
    expect(controller.submissions).toEqual([]);
    expect(controller.state.errorBanner).toBeNull();
  });

  it('rejects ordinary text from the command input without sending chat', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('what is running?');
    testRenderer.mockInput.pressEnter();
    const frame = await testRenderer.waitForFrame(value => value.includes('Commands start with /'));
    expect(frame).toContain('Use Experiment chat for questions.');
    expect(controller.submissions).toEqual([]);
    expect(controller.chatSubmissions).toEqual([]);
  });

  it('suggests and completes slash commands with Tab', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('/pa');
    const suggestions = await testRenderer.waitForFrame(value => value.includes('[Tab]'));
    expect(suggestions).toContain('/pause');
    expect(suggestions).not.toContain('/help  ');
    expect(suggestions).not.toContain('/perf');
    expect(suggestions.indexOf('/pause')).toBeLessThan(suggestions.indexOf('Command'));
    expect(testRenderer.renderer.root.findDescendantById('command-input-box')?.height).toBe(3);

    testRenderer.mockInput.pressKey('TAB');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.submissions.length === 1);
    expect(controller.submissions).toEqual(['/pause']);
  });

  it('completes the default-highlighted suggestion with Tab on the experiment log landing view', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    // The landing view: the experiment log (hypothesis table) is on screen,
    // same as the operator sees before opening a round.
    controller.publish({...controller.state, experimentLog: initialSessionState().experimentLog});

    await testRenderer.mockInput.typeText('/p');
    await testRenderer.waitForFrame(value => value.includes('[Tab]'));

    testRenderer.mockInput.pressKey('TAB');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.submissions.length === 1);
    expect(controller.submissions).toEqual(['/pause']);
  });

  it('completes a navigated suggestion with Tab on the experiment log landing view', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    controller.publish({...controller.state, experimentLog: initialSessionState().experimentLog});

    // /pause, /perf, /prompt: navigate down twice to land on /prompt.
    await testRenderer.mockInput.typeText('/p');
    await testRenderer.waitForFrame(value => value.includes('[Tab]'));
    testRenderer.mockInput.pressArrow('down');
    testRenderer.mockInput.pressArrow('down');
    const suggestions = await testRenderer.waitForFrame(value => value.includes('› /prompt'));
    expect(suggestions).toContain('/prompt');

    testRenderer.mockInput.pressKey('TAB');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.submissions.length === 1);
    expect(controller.submissions).toEqual(['/prompt']);
  });

  it('highlights a leading slash-command token', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('/steer inspect the cache');
    const input = testRenderer.renderer.root.findDescendantById('command-input');
    expect(input).toBeInstanceOf(InputRenderable);
    if (!(input instanceof InputRenderable)) throw new Error('input was not rendered');
    expect(input.getLineHighlights(0)).toMatchObject([{start: 0, end: 6}]);
  });

  it('exits on the first Ctrl-C even while the input is focused', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16, exitOnCtrlC: false});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    cleanup.push(() => app.destroy());
    const destroyed = new Promise<void>(resolve => testRenderer.renderer.once('destroy', resolve));

    testRenderer.mockInput.pressKey('c', {ctrl: true});

    await destroyed;
  });

  it('copies a selected range on Ctrl-C without exiting', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16, exitOnCtrlC: false});
    const controller = new FakeController(initialSessionState());
    const clipboard = clipboardReturning('copied');
    const app = createOpenTuiApp(testRenderer.renderer, controller, clipboard);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('Command'));

    testRenderer.mockInput.pressKey('c', {ctrl: true});

    const frame = await testRenderer.waitForFrame(value => value.includes('Copied selected text'));
    expect(frame).toContain('Ctrl+C exits when no text is selected');
    expect(clipboard.calls).toBe(1);

    testRenderer.mockInput.pressKey('x');
    await testRenderer.waitForFrame(value => !value.includes('Copied selected text'));
  });

  it('keeps running and explains the fallback when OSC52 copy is unavailable', async () => {
    const testRenderer = await createTestRenderer({width: 90, height: 16, exitOnCtrlC: false});
    const controller = new FakeController(initialSessionState());
    const clipboard = clipboardReturning('unsupported');
    const app = createOpenTuiApp(testRenderer.renderer, controller, clipboard);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('Command'));

    testRenderer.mockInput.pressKey('c', {ctrl: true});

    const frame = await testRenderer.waitForFrame(value => value.includes('Copy unavailable'));
    expect(frame).toContain('selection kept');
    expect(frame).toContain('terminal copy command');
    expect(clipboard.calls).toBe(1);
  });

  it('advertises Escape and returns a non-live view to live output', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 16});
    const controller = new FakeController({
      ...initialSessionState(),
      overlay: {kind: 'help', content: 'Available commands'},
      core: {
        ...initialSessionState().core,
        transcript: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const overlay = await testRenderer.waitForFrame(value => value.includes('Esc: close dialog'));
    expect(overlay).toContain('Available commands');
    // No round has landed yet, so there is no tab row and the round view is
    // just the agents graph and the transcript behind the overlay.
    expect(overlay).toContain('Agents');
    testRenderer.mockInput.pressKey('ESCAPE');
    await testRenderer.waitForFrame(value => !value.includes('Esc: close dialog'));
    expect(controller.liveCalls).toBe(1);
  });

  it('uses the native scrollbox for long output', async () => {
    const lines = Array.from({length: 50}, (_, index) => `tool output line ${index + 1}`).join(
      '\n',
    );
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        transcript: [{id: 'assistant', kind: 'assistant', label: 'Agent', content: lines}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.waitForFrame(value => value.includes('tool output line 50'));
    testRenderer.mockInput.pressKey('HOME');
    const frame = await testRenderer.waitForFrame(value => value.includes('tool output line 1'));
    expect(frame).not.toContain('tool output line 50');
  });

  it('renders a tool call and response as two regions in one card', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        transcript: [
          {
            id: 'tool',
            kind: 'tool',
            label: 'implementer · round 1',
            content: '2 passed',
            toolName: 'Bash',
            toolArguments: {command: 'pytest'},
            toolResult: {kind: 'tool_result', tool: 'Bash', content: '2 passed'},
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('2 passed'));
    expect(frame).toContain('→ Bash pytest');
    expect(frame).toContain('← 2 passed');
    // Header housing, agents pane, transcript frame, and the command box. A
    // round tab draws no border, and this fixture has no rounds. A focused pane draws a
    // heavy corner, so the count is over both styles. The transcript card used
    // to contribute a fifth: #565 replaced its four-sided border with a
    // top-edge rule, which draws no corner glyph at all. The call and result
    // regions #620 added are bands inside the card, not bordered boxes, so they
    // never contributed one.
    expect(frame.match(/[╭┏]/g)).toHaveLength(4);
  });

  it('renders a typed command payload with labeled stderr and exit code', async () => {
    // 18 rows, not 16: the header is a three-row pane, so a 16-row terminal
    // leaves the transcript too short to hold the whole payload card.
    const testRenderer = await createTestRenderer({width: 80, height: 18});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        transcript: [
          {
            id: 'tool',
            kind: 'tool',
            label: 'implementer · round 1',
            content: '1 failed',
            toolName: 'Bash',
            toolArguments: {command: 'pytest'},
            toolResult: {
              kind: 'tool_result',
              tool: 'Bash',
              content: '1 failed',
              payload: {
                kind: 'command',
                stdout: '1 failed',
                stderr: 'assertion error',
                exit_code: 1,
                duration: 0.4,
              },
            },
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('exit code: 1'));
    expect(frame).toContain('← 1 failed');
    expect(frame).toContain('stderr:');
    expect(frame).toContain('assertion error');
  });

  it('unwraps the shell wrapper codex writes around an execute command', async () => {
    // Wide enough that the unwrapped command is one row, so the assertion is
    // about the text and not about where the renderer chose to wrap it.
    const testRenderer = await createTestRenderer({width: 100, height: 16});
    const controller = new FakeController(
      toolCallState({
        toolName: 'execute',
        toolArguments: {command: `/bin/bash -lc "cargo test -p queue-rs 'ring buffer'"`},
      }),
    );
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('→ execute'));
    expect(frame).toContain(`→ execute cargo test -p queue-rs 'ring buffer'`);
    expect(frame).not.toContain('/bin/bash');
    expect(frame).not.toContain('command=');
  });

  it('summarizes a structured file_change call instead of inlining its JSON', async () => {
    const testRenderer = await createTestRenderer({width: 130, height: 16});
    const controller = new FakeController(
      toolCallState({
        toolName: 'file_change',
        toolArguments: {
          changes: [
            {path: 'src/lib.rs', kind: 'delete'},
            {path: 'src/queue.rs', kind: 'modified'},
            {path: 'src/new.rs', kind: 'added'},
          ],
        },
      }),
    );
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('→ file_change'));
    expect(frame).toContain('3 changes: delete src/lib.rs, modify src/queue.rs, +1 more');
    expect(frame).not.toContain('"path"');
  });

  it('collapses a long command result to its exit status and output size', async () => {
    const stdout = `${Array.from({length: 40}, (_, index) => `compiling crate ${index}`).join('\n')}\n`;
    const testRenderer = await createTestRenderer({width: 80, height: 18});
    const controller = new FakeController(
      toolCallState({
        toolName: 'execute',
        toolArguments: {command: 'cargo build'},
        content: stdout,
        toolResult: {
          kind: 'tool_result',
          tool: 'execute',
          content: stdout,
          payload: {
            kind: 'command',
            stdout,
            stderr: 'error: could not compile\n',
            exit_code: 101,
            duration: 12.5,
          },
        },
      }),
    );
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const collapsed = await testRenderer.waitForFrame(value => value.includes('exit 101'));
    expect(collapsed).toContain('← exit 101 · 12.5s · 41 lines');
    expect(collapsed).not.toContain('compiling crate 0');
    expect(collapsed).toContain('Show full response');
  });

  it('collapses a json tool result to its top-level shape', async () => {
    const value = Object.fromEntries(Array.from({length: 9}, (_, index) => [`k${index}`, index]));
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController(
      toolCallState({
        toolName: 'Read',
        toolArguments: {path: 'run-state.json'},
        content: 'irrelevant',
        toolResult: {
          kind: 'tool_result',
          tool: 'Read',
          content: 'irrelevant',
          payload: {kind: 'json', value},
        },
      }),
    );
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value_ => value_.includes('keys:'));
    expect(frame).toContain('← {keys: k0, k1, k2, k3, +5 more}');
    expect(frame).not.toContain('"k8"');
  });

  it('draws provider lifecycle chatter without cards and keeps an error prominent', async () => {
    const testRenderer = await createTestRenderer({width: 90, height: 26});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        transcript: [
          {
            id: 'lifecycle',
            kind: 'diagnostic',
            label: 'implementer · round 1',
            content: '[codex thread 01a0 started]\n[codex turn started]',
          },
          {
            id: 'banner',
            kind: 'diagnostic',
            label: 'implementer · round 1',
            content: 'driver: agentshim, provider: codex, model: gpt-5.6',
          },
          {
            id: 'failure',
            kind: 'diagnostic',
            label: 'implementer · round 1',
            content: '[codex error] stream disconnected before completion',
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('[codex error]'));
    expect(frame).toContain('[codex turn started]');
    expect(frame).toContain('driver: agentshim');
    // Header housing, agents pane, transcript frame, and the command bar. The
    // two quiet diagnostics draw no border at all, so they add nothing to the
    // count, and the one card the error diagnostic still earns no longer adds
    // one either: #565 turned that four-sided border into a top-edge rule,
    // which has no corner glyph.
    expect(frame.match(/[╭┏]/g)).toHaveLength(4);
  });

  it('renders an unanswered tool call once, with no response band', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 16});
    const controller = new FakeController(
      toolCallState({
        toolName: 'file_change',
        toolArguments: {changes: [{path: 'a.rs', kind: 'delete'}]},
      }),
    );
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('→ file_change'));
    expect(frame.match(/→ file_change/g)).toHaveLength(1);
    // The response band is the only thing that draws a left arrow followed by
    // text; the footer's key hint is the glyph pair, not this.
    expect(frame).not.toContain('← ');
    // The header, the two panes, and the command bar. This turn's card no
    // longer adds a corner: #565 made it a top-edge rule instead of a border.
    expect(frame.match(/[╭┏]/g)).toHaveLength(4);
  });

  it('collapses, prettifies, and expands long JSON tool responses', async () => {
    const response = JSON.stringify(
      Object.fromEntries(Array.from({length: 12}, (_, index) => [`field_${index}`, index])),
    );
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      selectedEntryId: 'tool',
      core: {
        ...initialSessionState().core,
        transcript: [
          {
            id: 'tool',
            kind: 'tool',
            label: 'implementer · round 1',
            content: response,
            toolName: 'Read',
            toolArguments: {path: 'run-state.json'},
            toolResult: {kind: 'tool_result', tool: 'Read', content: response},
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const collapsed = await testRenderer.waitForFrame(value =>
      value.includes('Show full response'),
    );
    expect(collapsed).toContain('← {');
    expect(collapsed).toContain('"field_0": 0');
    expect(collapsed).not.toContain('"field_11": 11');

    testRenderer.mockInput.pressEnter();
    const expanded = await testRenderer.waitForFrame(value => value.includes('"field_11": 11'));
    expect(expanded).toContain('click or Enter to collapse response');

    // Expanding scrolls the card's tail into view, so click the collapse hint,
    // which the previous assertion proves is on screen.
    const hint = 'click or Enter to collapse response';
    const lines = expanded.split('\n');
    const row = lines.findIndex(line => line.includes(hint));
    const column = (lines[row]?.indexOf(hint) ?? 0) + 2;
    await testRenderer.mockMouse.click(column, row);
    const recollapsed = await testRenderer.waitForFrame(value =>
      value.includes('Show full response'),
    );
    expect(recollapsed).not.toContain('"field_11": 11');
  });

  it('collapses prompts and expands the latest prompt with Ctrl+P', async () => {
    const content = Array.from({length: 20}, (_, index) => `prompt line ${index + 1}`).join('\n');
    const testRenderer = await createTestRenderer({width: 80, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        transcript: [{id: 'prompt', kind: 'prompt', label: 'Prompt', content}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const collapsed = await testRenderer.waitForFrame(value => value.includes('8 more lines'));
    expect(collapsed).not.toContain('prompt line 20');
    testRenderer.mockInput.pressKey('p', {ctrl: true});
    const expanded = await testRenderer.waitForFrame(value => value.includes('prompt line 20'));
    expect(expanded).toContain('collapse');
  });

  it('expands the latest visible prompt without dropping agent filters', async () => {
    const visiblePrompt = Array.from(
      {length: 20},
      (_, index) => `implementer prompt ${index + 1}`,
    ).join('\n');
    const hiddenPrompt = Array.from({length: 20}, (_, index) => `judge prompt ${index + 1}`).join(
      '\n',
    );
    const testRenderer = await createTestRenderer({width: 100, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      selectedRound: 1,
      selectedAgentKind: 'implementer',
      core: {
        ...initialSessionState().core,
        rounds: [{number: 1, status: 'active'}],
        transcript: [
          {
            id: 'implementer-prompt',
            kind: 'prompt',
            agentKind: 'implementer',
            roundNumber: 1,
            content: visiblePrompt,
          },
          {
            id: 'judge-prompt',
            kind: 'prompt',
            agentKind: 'judge',
            roundNumber: 1,
            content: hiddenPrompt,
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.waitForFrame(value => value.includes('implementer prompt 1'));
    testRenderer.mockInput.pressKey('p', {ctrl: true});
    const expanded = await testRenderer.waitForFrame(value =>
      value.includes('implementer prompt 20'),
    );
    expect(expanded).not.toContain('judge prompt');
  });

  it('preserves existing cards when a large transcript receives state-only and tail updates', async () => {
    const conversation = Array.from({length: 1_000}, (_, index) => ({
      id: `entry-${index}`,
      kind: 'status' as const,
      content: `event ${index}`,
    }));
    const testRenderer = await createTestRenderer({width: 100, height: 20});
    const initial = initialSessionState();
    const controller = new FakeController({
      ...initial,
      core: {...initial.core, transcript: conversation},
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('event 999'));
    const firstCard = testRenderer.renderer.root.findDescendantById('event-entry-0');
    const lastCard = testRenderer.renderer.root.findDescendantById('event-entry-999');

    controller.publish({
      ...controller.state,
      core: {
        ...controller.state.core,
        status: 'paused',
      },
    });
    expect(testRenderer.renderer.root.findDescendantById('event-entry-0')).toBe(firstCard);
    expect(testRenderer.renderer.root.findDescendantById('event-entry-999')).toBe(lastCard);

    const previousLast = conversation.at(-1);
    if (previousLast === undefined) throw new Error('large transcript is unexpectedly empty');
    const updatedLast = {...previousLast, content: 'updated tail'};
    controller.publish({
      ...controller.state,
      core: {
        ...controller.state.core,
        transcript: [...conversation.slice(0, -1), updatedLast],
      },
    });
    await testRenderer.waitForFrame(value => value.includes('updated tail'));
    expect(testRenderer.renderer.root.findDescendantById('event-entry-0')).toBe(firstCard);
    expect(testRenderer.renderer.root.findDescendantById('event-entry-999')).not.toBe(lastCard);
  });

  it('paints only the tail of a huge transcript, then reveals history on scroll', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 20});
    const controller = new FakeController(hugeTranscriptState(20_000));
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.waitForFrame(value => value.includes('event 19999'));
    // Only a bounded tail gets cards: the newest entry is on screen, the run's
    // older history is not built at all.
    expect(testRenderer.renderer.root.findDescendantById('event-entry-19999')).toBeDefined();
    expect(testRenderer.renderer.root.findDescendantById('event-entry-0')).toBeUndefined();
    expect(testRenderer.renderer.root.findDescendantById('event-entry-19799')).toBeUndefined();

    testRenderer.mockInput.pressKey('HOME');
    await testRenderer.waitForFrame(() => true);

    // Scrolling back materializes the next block, so the capped history stays
    // reachable rather than being discarded.
    expect(testRenderer.renderer.root.findDescendantById('event-entry-19799')).toBeDefined();
    expect(testRenderer.renderer.root.findDescendantById('event-entry-19999')).toBeDefined();
  });

  it('asks the controller for history once the window reaches what is loaded', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 20});
    const state = hugeTranscriptState(2_400);
    const controller = new FakeController({
      ...state,
      // The client folded a suffix of the run: everything at or below sequence
      // 3000 is still on the server.
      core: {...state.core, historyAfterSequence: 3_000},
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.waitForFrame(value => value.includes('event 2399'));

    // Each press materializes another block of what is already loaded, and asks
    // for nothing while there is more of it left.
    for (let press = 0; press < 11; press += 1) {
      testRenderer.mockInput.pressKey('HOME');
      await testRenderer.waitForFrame(() => true);
    }
    expect(testRenderer.renderer.root.findDescendantById('event-entry-0')).toBeDefined();
    expect(controller.historyLoads).toBe(0);

    testRenderer.mockInput.pressKey('HOME');
    await testRenderer.waitForFrame(() => true);

    // The window starts at the oldest entry the client holds, so the next block
    // has to come from the backend.
    expect(controller.historyLoads).toBe(1);
  });

  it('appends live entries incrementally on a windowed transcript', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 20});
    const state = hugeTranscriptState(20_000);
    const controller = new FakeController(state);
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.waitForFrame(value => value.includes('event 19999'));
    const tailCard = testRenderer.renderer.root.findDescendantById('event-entry-19999');

    controller.publish({
      ...controller.state,
      core: {
        ...controller.state.core,
        transcript: [
          ...state.core.transcript,
          {id: 'entry-20000', kind: 'status' as const, content: 'event 20000'},
        ],
      },
    });
    await testRenderer.waitForFrame(value => value.includes('event 20000'));

    // The window anchor held, so the append stayed a prefix extension and the
    // cards already on screen were not rebuilt.
    expect(testRenderer.renderer.root.findDescendantById('event-entry-19999')).toBe(tailCard);
    expect(testRenderer.renderer.root.findDescendantById('event-entry-20000')).toBeDefined();
  });

  it('selects an agent with Tab and filters the transcript', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        phases: [
          {kind: 'implementer', status: 'completed', roundNumber: 1, roundLabel: 'round-1'},
          {kind: 'judge', status: 'active', roundNumber: 1, roundLabel: 'round-1'},
        ],
        rounds: [{number: 1, status: 'active'}],
        transcript: [
          {
            id: 'implementer',
            kind: 'assistant',
            label: 'implementer · round 1',
            agentKind: 'implementer',
            roundNumber: 1,
            content: 'edited files',
          },
          {
            id: 'judge',
            kind: 'assistant',
            label: 'judge · round 1',
            agentKind: 'judge',
            roundNumber: 1,
            content: 'checking behavior',
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.waitForFrame(value => value.includes('checking behavior'));
    testRenderer.mockInput.pressKey('TAB');
    // The header names the selection in the same words as the phase segment,
    // never as the backend phase kind it is stored as.
    const filtered = await testRenderer.waitForFrame(value =>
      value.includes('filtered to implementing'),
    );
    expect(filtered).not.toContain('selected implementer');
    expect(filtered).toContain('edited files');
    expect(filtered).not.toContain('checking behavior');
  });

  it('summarizes the active agent’s todos and expands them with Ctrl+T', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        agentKind: 'implementer',
        todos: [
          {
            agentKind: 'implementer',
            roundNumber: null,
            items: [
              {content: 'Profile the hot loop', status: 'completed'},
              {content: 'Vectorize the kernel', status: 'in_progress'},
              {content: 'Re-run the benchmark', status: 'pending'},
            ],
          },
        ],
        transcript: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const collapsed = await testRenderer.waitForFrame(value => value.includes('Todo 1/3'));
    expect(collapsed).toContain('▶ Vectorize the kernel');
    expect(collapsed).not.toContain('Re-run the benchmark');

    testRenderer.mockInput.pressKey('t', {ctrl: true});
    const expanded = await testRenderer.waitForFrame(value =>
      value.includes('Re-run the benchmark'),
    );
    expect(expanded).toContain('✓ Profile the hot loop');
    expect(expanded).toContain('○ Re-run the benchmark');
  });

  it('hides the todo strip when the visible agent has no todos', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        agentKind: 'judge',
        todos: [
          {
            agentKind: 'implementer',
            roundNumber: null,
            items: [{content: 'Edit files', status: 'completed'}],
          },
        ],
        transcript: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('live output'));
    expect(frame).not.toContain('Todo');
    expect(frame).not.toContain('Edit files');
  });

  it('keeps terminal results visible until the operator exits', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        status: 'failed',
        transcript: [
          {
            id: 'configuration-error',
            kind: 'result',
            label: 'Configuration failed',
            content: 'Invalid --max-rounds value',
            tone: 'failure',
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    let destroyed = false;
    testRenderer.renderer.once('destroy', () => {
      destroyed = true;
    });

    const frame = await testRenderer.waitForFrame(value =>
      value.includes('Invalid --max-rounds value'),
    );
    expect(frame).toContain('Configuration failed');
    await new Promise(resolve => setTimeout(resolve, 150));
    expect(destroyed).toBe(false);
  });

  it('opens a focused chat popup after a configuration failure', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        status: 'failed',
        transcript: [
          {
            id: 'configuration-error',
            kind: 'result',
            label: 'Configuration failed',
            content: 'agent.toml was not found',
            tone: 'failure',
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('/chat');
    testRenderer.mockInput.pressEnter();
    const popup = await testRenderer.waitForFrame(value => value.includes('Experiment chat'));
    expect(popup).toContain('Ask about this experiment');
    expect(popup).toContain('Message');

    await testRenderer.mockInput.typeText('why did startup fail?');
    testRenderer.mockInput.pressEnter();
    const answer = await testRenderer.waitForFrame(value => value.includes('Recorded diagnostic'));
    expect(controller.chatSubmissions).toEqual(['why did startup fail?']);
    expect(answer).toContain('Inspecting configuration events');
    expect(answer).toContain('→ Read(run-events.jsonl)');

    const overlay = testRenderer.renderer.root.findDescendantById('chat-overlay');
    const transcript = testRenderer.renderer.root.findDescendantById('chat-transcript');
    const turn = testRenderer.renderer.root.findDescendantById('event-chat-user');
    const input = testRenderer.renderer.root.findDescendantById('chat-modal-composer-box');
    if (
      overlay === undefined ||
      transcript === undefined ||
      turn === undefined ||
      input === undefined
    )
      throw new Error('modal chat geometry was missing');
    expect(transcript.x).toBe(overlay.x + 2);
    expect(turn.x).toBe(transcript.x);
    expect(input.x).toBe(transcript.x);

    testRenderer.mockInput.pressKey('ESCAPE');
    await testRenderer.waitForFrame(value => !value.includes('Experiment chat'));
    expect(controller.state.chatOpen).toBe(false);
  });

  it('stacks the modal chat without a seam at any terminal height', async () => {
    // The modal takes a share of the terminal, and a share of a row is not a
    // row: the layout rounds a child's offset from its parent separately from
    // that child's size, so at a fractional offset the two disagree and the
    // transcript either runs a row into the composer or leaves a row of the
    // modal's floor blank. Which heights round badly follows from the shares
    // rather than from any one size, so the whole range is checked.
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const controller = new FakeController({...initialSessionState(), chatOpen: true});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    for (let height = 18; height <= 48; height += 1) {
      testRenderer.renderer.resize(100, height);
      await frameAfter(testRenderer);
      const modal = testRenderer.renderer.root.findDescendantById('chat-overlay');
      const transcript = testRenderer.renderer.root.findDescendantById('chat-transcript');
      const composer = testRenderer.renderer.root.findDescendantById('chat-modal-composer');
      if (modal === undefined || transcript === undefined || composer === undefined)
        throw new Error('modal chat geometry was missing');
      // The height rides along in the comparison so a failure names the
      // terminal it happened on, not only the rows that disagreed.
      expect({
        height,
        top: transcript.y,
        end: transcript.y + transcript.height,
        floor: composer.y + composer.height,
      }).toEqual({
        height,
        // The transcript starts under the top border and ends exactly where
        // the composer starts, and the composer's last row is the one above
        // the bottom border.
        top: modal.y + 1,
        end: composer.y,
        floor: modal.y + modal.height - 1,
      });
    }
  });

  it('pins the modal rectangle across a width sweep', async () => {
    // Whole-cell edges replaced percentage ones, and the two agree only
    // because the layout rounds per edge rather than per size: it rounds the
    // absolute left and the absolute right and subtracts. Rounding the width
    // itself instead, `round(0.8W)`, looks equivalent and is not, so the
    // rectangle is pinned column by column rather than at one width.
    const testRenderer = await createTestRenderer({width: 100, height: 32});
    const controller = new FakeController({...initialSessionState(), chatOpen: true});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    for (let width = 60; width <= 160; width += 1) {
      testRenderer.renderer.resize(width, 32);
      await frameAfter(testRenderer);
      const modal = testRenderer.renderer.root.findDescendantById('chat-overlay');
      if (modal === undefined) throw new Error('modal chat geometry was missing');
      // The width rides along so a failure names the terminal it happened on.
      expect({width, left: modal.x, right: modal.x + modal.width}).toEqual({
        width,
        left: Math.round(width * 0.1),
        right: Math.round(width * 0.9),
      });
    }
  });

  it('rewraps the modal composer against the width the resize just gave it', async () => {
    // A renderable answers `width` with the last width the layout computed,
    // not the one just assigned, so reading the modal back after resizing it
    // describes the previous rectangle. The composer sizes its editor from
    // that number, and nothing else asks again: a resize is not a state
    // change, so a draft would stay wrapped for the old width until the
    // operator happened to type.
    const testRenderer = await createTestRenderer({width: 140, height: 30});
    const controller = new FakeController({...initialSessionState(), chatOpen: true});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await frameAfter(testRenderer);

    const composerBox = (): {y: number; height: number} => {
      const box = testRenderer.renderer.root.findDescendantById('chat-modal-composer-box');
      if (box === undefined) throw new Error('modal composer geometry was missing');
      return {y: box.y, height: box.height};
    };

    // A state update after the first layout, so the composer starts out sized
    // against the modal as drawn rather than against the width it reported
    // before ever being laid out. The resize below is then the only thing that
    // can put the two out of step.
    controller.publish({...controller.state});
    await frameAfter(testRenderer);

    // Sixty characters: one editor row inside the 140-column modal, whose
    // editor is 104 columns wide, and two inside the 60-column modal's 40.
    await testRenderer.mockInput.typeText('x'.repeat(60));
    await frameAfter(testRenderer);
    expect(composerBox().height).toBe(3);

    testRenderer.renderer.resize(60, 30);
    await frameAfter(testRenderer);
    expect(composerBox().height).toBe(4);

    // And the modal still stacks: the transcript ends where the composer
    // starts, so the taller box came out of the transcript rather than
    // overrunning the floor.
    const modal = testRenderer.renderer.root.findDescendantById('chat-overlay');
    const transcript = testRenderer.renderer.root.findDescendantById('chat-transcript');
    const composer = testRenderer.renderer.root.findDescendantById('chat-modal-composer');
    if (modal === undefined || transcript === undefined || composer === undefined)
      throw new Error('modal chat geometry was missing');
    expect(transcript.y + transcript.height).toBe(composer.y);
    expect(composer.y + composer.height).toBe(modal.y + modal.height - 1);
  });

  it('accepts another chat message while an agent turn is pending', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      chatPending: true,
      chatConversation: [
        {id: 'active-question', kind: 'user', label: 'You', content: 'first question'},
      ],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('/chat');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(value => value.includes('Experiment chat'));
    await testRenderer.mockInput.typeText('queued follow-up');
    testRenderer.mockInput.pressEnter();

    await testRenderer.waitForFrame(value => value.includes('Recorded diagnostic'));
    expect(controller.chatSubmissions).toEqual(['queued follow-up']);
  });
});

describe('overlay scrolling', () => {
  /** Content taller than the overlay box at any terminal size it is used at. */
  function longOverlayContent(): string {
    return Array.from(
      {length: 60},
      (_, index) => `overlay line ${String(index).padStart(2, '0')}`,
    ).join('\n');
  }

  function hintRows(frame: string): number {
    return frameRows(frame).filter(row => row.includes('PgUp/PgDn: scroll')).length;
  }

  it('reaches the last line of content taller than the box', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      overlay: {kind: 'help', content: longOverlayContent()},
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const first = await testRenderer.waitForFrame(value => value.includes('overlay line 00'));
    expect(first).not.toContain('overlay line 59');

    // No named page key in the mock input; send the raw terminal sequence.
    for (let index = 0; index < 12; index += 1) testRenderer.mockInput.pressKey('\x1B[6~');
    const scrolled = await frameAfter(testRenderer);

    // The last row of content is the one the hint used to overpaint.
    expect(scrolled).toContain('overlay line 59');
    expect(scrolled).not.toContain('overlay line 00');

    for (let index = 0; index < 12; index += 1) testRenderer.mockInput.pressKey('\x1B[5~');
    expect(await frameAfter(testRenderer)).toContain('overlay line 00');
  });

  it('keeps one hint row however often the content changes', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      overlay: {kind: 'detail', content: 'ack 0'},
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    expect(hintRows(await frameAfter(testRenderer))).toBe(1);

    for (let index = 1; index <= 6; index += 1) {
      controller.publish({
        ...controller.state,
        overlay: {kind: 'detail', content: `ack ${index}`},
      });
      const frame = await frameAfter(testRenderer);
      expect(frame, `after ${index} content changes`).toContain(`ack ${index}`);
      expect(hintRows(frame), `after ${index} content changes`).toBe(1);
    }
  });

  it('reopens the same overlay at the top', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const overlay = {kind: 'help' as const, content: longOverlayContent()};
    const controller = new FakeController({...initialSessionState(), overlay});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('overlay line 00'));

    for (let index = 0; index < 12; index += 1) testRenderer.mockInput.pressKey('\x1B[6~');
    expect(await frameAfter(testRenderer)).toContain('overlay line 59');

    controller.publish({...controller.state, overlay: null});
    await frameAfter(testRenderer);
    controller.publish({...controller.state, overlay});
    const reopened = await frameAfter(testRenderer);

    expect(reopened).toContain('overlay line 00');
    expect(reopened).not.toContain('overlay line 59');
  });

  it('scrolls the overlay rather than the docked chat behind it', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 24});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);
    testRenderer.mockInput.pressKey('w', {ctrl: true});
    await frameAfter(testRenderer);
    expect(controller.state.layout.focus).toBe('chat');

    controller.publish({
      ...controller.state,
      overlay: {kind: 'help', content: longOverlayContent()},
    });
    await testRenderer.waitForFrame(value => value.includes('overlay line 00'));

    const chatScroll = testRenderer.renderer.root.findDescendantById('chat-pane-scroll');
    if (!(chatScroll instanceof ScrollBoxRenderable)) throw new Error('no docked chat scroll box');
    for (let index = 0; index < 12; index += 1) testRenderer.mockInput.pressKey('\x1B[6~');
    const scrolled = await frameAfter(testRenderer);

    expect(scrolled).toContain('overlay line 59');
    expect(chatScroll.scrollTop).toBe(0);
    expect(controller.state.layout.focus).toBe('chat');
  });
});

describe('theming', () => {
  /**
   * The role colour a transcript card carries.
   *
   * The top-edge rule, because that is where the role lives: a card has no
   * fill (tui-conventions.md) and #565 replaced its four sides with that one
   * rule, so the body text reports the canvas and the role would otherwise go
   * unasserted here. `conversation.test.ts` pins the same colour as a
   * computation over theme.ts across all eight themes; this asserts the
   * rendered frame actually carries what that computation produces.
   */
  function cardBorder(testRenderer: TestRendererSetup, id: string): string | undefined {
    const card = testRenderer.renderer.root.findDescendantById(id);
    return card instanceof BoxRenderable ? rgbToHex(card.borderColor).toLowerCase() : undefined;
  }

  const assistantEntry = {
    id: 'themed',
    kind: 'assistant' as const,
    label: 'implementer',
    agentKind: 'implementer',
    content: 'themed body text',
  };

  it('paints the whole surface from the selected theme', async () => {
    const light = resolveTheme('light');
    const testRenderer = await createTestRenderer({width: 90, height: 20});
    const controller = new FakeController({
      ...initialSessionState('light'),
      core: {
        ...initialSessionState('light').core,
        status: 'running',
        transcript: [assistantEntry],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('themed body text'));

    expect(spanColors(testRenderer, 'VibeSys')?.fg).toBe(light.accent);
    const body = spanColors(testRenderer, 'themed body text');
    expect(body?.fg).toBe(light.conversation.assistant.content);
    // #565: the card carries its role on the divider rule, not a fill, so its
    // body sits on the theme's canvas.
    expect(body?.bg).toBe(light.canvas);
    expect(cardBorder(testRenderer, 'event-themed')).toBe(
      ensureContrast(
        light.conversation.assistant.border,
        light.canvas,
        SUBTLE_TEXT_MIN_CONTRAST,
      ).toLowerCase(),
    );
    expect(spanColors(testRenderer, 'implementer')?.fg).toBe(light.conversation.assistant.label);
  });

  it('keeps the dark baseline identical to the pre-theme palette, background aside', async () => {
    const testRenderer = await createTestRenderer({width: 90, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        status: 'running',
        transcript: [assistantEntry],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('themed body text'));

    expect(spanColors(testRenderer, 'VibeSys')?.fg).toBe('#22d3ee');
    const body = spanColors(testRenderer, 'themed body text');
    expect(body?.fg).toBe('#e2e8f0');
    // Both changes land on this one cell. #565 removed the card's fill, so the
    // body reports whatever is behind it, and #574 collapsed the palette, so
    // what is behind it is the one universal background rather than the old
    // lighter `canvas`. Neither value either change asserted on its own
    // survives: not `#0f172a` (the canvas before the collapse) and not
    // `#03192d` (a role tint the card no longer paints).
    expect(body?.bg).toBe('#020617');
    expect(spanColors(testRenderer, 'implementer')?.fg).toBe('#5cb6cc');
  });

  it('gives the key-help line the same background as the chrome above it', async () => {
    // #574's second symptom, and the one visible without comparing two panes:
    // the key-help line sets no background of its own, so it fell through to
    // the root frame. The root was painted `canvas` while every pane and the
    // header were painted the darker `elevatedSurface`, which left a pale rule
    // across the bottom of the screen under a dark theme. With one background
    // there is nothing left to fall through to that disagrees.
    //
    // Asserted against the header's own cells rather than a literal, because
    // the symptom is that two rows disagree; that stays the property being
    // checked if the palette moves again.
    const testRenderer = await createTestRenderer({width: 150, height: 40});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        status: 'running',
        transcript: [assistantEntry],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('F4: zoom'));

    const help = spanColors(testRenderer, 'F4: zoom');
    expect(help?.bg).toBe(spanColors(testRenderer, 'VibeSys')?.bg);
    expect(help?.bg).toBe(resolveTheme('dark').canvas);
  });

  it('repaints live when the selected theme changes', async () => {
    const testRenderer = await createTestRenderer({width: 90, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        status: 'running',
        transcript: [assistantEntry],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('themed body text'));
    expect(spanColors(testRenderer, 'VibeSys')?.fg).toBe(resolveTheme('dark').accent);

    controller.setTheme('solarized-light');
    await testRenderer.waitForVisualIdle();

    const solarized = resolveTheme('solarized-light');
    expect(spanColors(testRenderer, 'VibeSys')?.fg).toBe(solarized.accent);
    const body = spanColors(testRenderer, 'themed body text');
    expect(body?.fg).toBe(solarized.conversation.assistant.content);
    expect(body?.bg).toBe(solarized.canvas);
  });

  it('navigates the theme list with the keyboard and applies on Enter', async () => {
    const testRenderer = await createTestRenderer({width: 90, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        rounds: [
          {number: 1, status: 'completed'},
          {number: 2, status: 'active'},
        ],
        transcript: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('/theme');
    testRenderer.mockInput.pressEnter();
    const opened = await testRenderer.waitForFrame(value => value.includes('Themes'));
    // The view opens on the theme in use.
    expect(opened).toContain('\u203a dark');
    expect(opened).toContain('active');

    testRenderer.mockInput.pressKey('ARROW_DOWN');
    const moved = await testRenderer.waitForFrame(value => value.includes('\u203a light'));
    expect(moved).not.toContain('\u203a dark');
    // Navigating the list leaves the view behind it alone.
    expect(controller.state.selectedRound).toBeNull();
    expect(controller.state.selectedAgentKind).toBeNull();
    expect(controller.state.themeName).toBe('dark');

    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForVisualIdle();

    expect(controller.state.themeName).toBe('light');
    expect(controller.state.themePicker).toBeNull();
    expect(spanColors(testRenderer, 'VibeSys')?.fg).toBe(resolveTheme('light').accent);
  });

  it('closes the theme list on Escape without switching theme', async () => {
    const testRenderer = await createTestRenderer({width: 90, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        transcript: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('/theme');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(value => value.includes('Themes'));
    testRenderer.mockInput.pressKey('ARROW_DOWN');
    await testRenderer.waitForFrame(value => value.includes('\u203a light'));

    testRenderer.mockInput.pressKey('ESCAPE');
    // A bare ESC is held by the stdin parser until its escape-sequence
    // timeout expires, so the key lands a beat after it is pressed.
    await new Promise(resolve => setTimeout(resolve, 40));
    await testRenderer.flush();
    const closed = testRenderer.captureCharFrame();

    expect(closed).not.toContain('\u203a light');
    expect(closed).toContain('live output');
    expect(controller.state.themePicker).toBeNull();
    expect(controller.state.themeName).toBe('dark');
    // Escape closed the picker rather than resetting the view behind it.
    expect(controller.liveCalls).toBe(0);
  });

  it('contains typing and Enter while the theme list is open', async () => {
    const testRenderer = await createTestRenderer({width: 90, height: 24});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('/theme');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(value => value.includes('Themes'));
    testRenderer.mockInput.pressKey('ARROW_DOWN');
    await testRenderer.waitForFrame(value => value.includes('\u203a light'));

    // The picker is modal: typed text is swallowed instead of reaching the
    // command input hidden behind it, and Enter applies the highlighted theme
    // rather than submitting whatever leaked through.
    await testRenderer.mockInput.typeText('/quack');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.state.themePicker === null);

    expect(controller.submissions).toEqual(['/theme']);
    expect(controller.state.themeName).toBe('light');
    const settled = testRenderer.captureCharFrame();
    // The input still shows its placeholder: nothing typed reached it.
    expect(settled).not.toContain('/quack');
    expect(settled).toContain('Type /help for commands');
  });

  it('owns its keys over the chat it was opened from', async () => {
    const testRenderer = await createTestRenderer({width: 90, height: 26});
    const controller = new FakeController({...initialSessionState(), chatOpen: true});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('Ask a question about'));

    await controller.submitChat('/theme');
    await testRenderer.waitForFrame(value => value.includes('Themes'));
    testRenderer.mockInput.pressKey('ARROW_DOWN');
    await testRenderer.waitForFrame(value => value.includes('\u203a light'));

    // The chat is still open behind the picker, and neither the arrows nor
    // Escape reached it.
    expect(controller.state.chatOpen).toBe(true);
    testRenderer.mockInput.pressKey('ESCAPE');
    // A bare ESC is held by the stdin parser until its escape-sequence
    // timeout expires, so the key lands a beat after it is pressed.
    await new Promise(resolve => setTimeout(resolve, 40));
    await testRenderer.flush();
    expect(testRenderer.captureCharFrame()).not.toContain('\u203a light');
    expect(controller.state.themePicker).toBeNull();
    expect(controller.state.chatOpen).toBe(true);
    expect(controller.chatSubmissions).toEqual([]);
  });

  it('themes the overlay and the chat panel from the same theme', async () => {
    const latte = resolveTheme('catppuccin-latte');
    const testRenderer = await createTestRenderer({width: 90, height: 30});
    const controller = new FakeController(initialSessionState('catppuccin-latte'));
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    controller.publish({
      ...controller.state,
      overlay: {kind: 'error', content: 'configuration failed'},
    });
    await testRenderer.waitForFrame(value => value.includes('configuration failed'));
    expect(spanColors(testRenderer, 'configuration failed')?.fg).toBe(
      latte.conversation.failure.content,
    );
    expect(spanColors(testRenderer, 'Esc to close')?.fg).toBe(latte.textSubtle);

    controller.publish({...controller.state, overlay: null, chatOpen: true});
    await testRenderer.waitForFrame(value => value.includes('Ask a question about'));
    expect(spanColors(testRenderer, 'Ask a question about')?.fg).toBe(latte.textSubtle);
  });

  it('conveys agent and todo status with glyphs and words, not color alone', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 36});
    const controller = new FakeController({
      ...initialSessionState('high-contrast-light'),
      todosExpanded: true,
      core: {
        ...initialSessionState('high-contrast-light').core,
        rounds: [{number: 1, status: 'active'}],
        phases: [
          {kind: 'implementer', status: 'completed', roundNumber: 1, roundLabel: 'round-1'},
          {kind: 'judge', status: 'failed', roundNumber: 1, roundLabel: 'round-1'},
          {kind: 'profiler', status: 'cancelled', roundNumber: 1, roundLabel: 'round-1'},
          {kind: 'reviewer', status: 'interrupted', roundNumber: 1, roundLabel: 'round-1'},
        ],
        todos: [
          {
            agentKind: null,
            roundNumber: null,
            items: [
              {content: 'write the kernel', status: 'completed'},
              {content: 'benchmark it', status: 'in_progress'},
            ],
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    const frame = await testRenderer.waitForFrame(value => value.includes('benchmark it'));

    expect(frame).toContain('✓ implementer');
    expect(frame).toContain('× judge');
    expect(frame).toContain('completed');
    expect(frame).toContain('failed');
    expect(frame).toContain('■ profiler');
    expect(frame).toContain('cancelled');
    expect(frame).toContain('! reviewer');
    expect(frame).toContain('interrupted');
    expect(frame).toContain('✓ write the kernel');
    expect(frame).toContain('▶ benchmark it');
  });
  it('lands on the experiment log instead of the per-round transcript', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 22});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        rounds: [{number: 41, status: 'completed'}],
        transcript: [
          {
            id: 'a',
            kind: 'assistant',
            label: 'implementer',
            content: 'round 41 detail',
            roundNumber: 41,
          },
        ],
      },
    });
    // The landing view is what a fresh client starts on.
    controller.publish({...controller.state, experimentLog: initialSessionState().experimentLog});
    controller.experiments = [
      logEntry('H-07', 41, 41, {
        claim: 'batch the prefill step',
        resolved_outcome: 'proven',
        judge_verdict: 'pass',
        rounds: [{round: 41, passed: true, reviewed: true}],
      }),
    ];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    const landing = await frameAfter(testRenderer);
    expect(landing).toContain('Experiments');
    expect(landing).toContain('Implementation Details');
    expect(landing).toContain('Outcome');
    expect(landing).toContain('H-07');
    expect(landing).toContain('Accepted');
    expect(landing).not.toContain('Verdict');
    expect(landing).not.toContain('Pass');
    // Per-round detail is what the operator opts into, not what greets them.
    expect(landing).not.toContain('round 41 detail');
    // The rounds strip and agent map are per-round chrome; neither is drawn.
    expect(landing).not.toContain('─ Rounds ─');
    expect(paneFrameColumn(landing, 'Agents')).toBeNull();
    expect(landing).not.toContain('Agents');
  });

  it('shows a round’s agent harness and model inside a hypothesis round drilldown', async () => {
    // The Agents pane inside a hypothesis round drilldown is the same
    // AgentMapView the live run uses, driven by the same core.phases replayed
    // from the full event history, so a completed historical round must
    // carry its runtime label exactly like a live one does.
    const testRenderer = await createTestRenderer({width: 120, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        rounds: [{number: 42, status: 'completed'}],
        phases: [
          {
            kind: 'implementer',
            status: 'completed',
            roundNumber: 42,
            roundLabel: 'round-42-implementer',
            provider: 'codex',
            model: 'gpt-5.1-codex-max',
          },
        ],
        transcript: [
          {
            id: 'b',
            kind: 'assistant',
            label: 'implementer',
            content: 'grew the block',
            roundNumber: 42,
          },
        ],
      },
    });
    controller.experiments = [
      logEntry('H-08', 42, 42, {
        claim: 'increase kv cache block',
        resolved_outcome: 'proven',
        rounds: [{round: 42, passed: true, reviewed: true}],
      }),
    ];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    testRenderer.mockInput.pressEnter(); // hypothesis summary
    await frameAfter(testRenderer);
    testRenderer.mockInput.pressEnter(); // round trajectory
    const trajectory = await frameAfter(testRenderer);

    expect(trajectory).toContain('Codex (GPT');
  });

  it('drills from a full hypothesis summary into a round trajectory and back', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        rounds: [
          {number: 41, status: 'completed'},
          {number: 42, status: 'completed'},
          {number: 43, status: 'completed'},
        ],
        transcript: [
          {
            id: 'a',
            kind: 'assistant',
            label: 'implementer',
            content: 'unrelated round 41',
            roundNumber: 41,
          },
          {
            id: 'b',
            kind: 'assistant',
            label: 'implementer',
            content: 'grew the block',
            roundNumber: 42,
          },
          {
            id: 'c',
            kind: 'assistant',
            label: 'judge',
            content: 'regression found',
            roundNumber: 43,
          },
        ],
      },
    });
    controller.experiments = [
      logEntry('H-07', 41, 41, {
        claim: 'batch the prefill step',
        resolved_outcome: 'proven',
        rounds: [{round: 41, passed: true, reviewed: true}],
      }),
      logEntry('H-08', 42, 43, {
        claim:
          'Increasing the KV cache block should reduce allocator synchronization across producer and consumer operations without changing queue ordering.',
        resolved_outcome: 'rejected',
        rounds: [
          {round: 42, passed: false, reviewed: false},
          {round: 43, passed: false, reviewed: true},
        ],
      }),
    ];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    const table = await frameAfter(testRenderer);
    expect(table).toContain('42-43');
    expect(table).not.toContain('grew the block');

    testRenderer.mockInput.pressKey('ARROW_DOWN');
    testRenderer.mockInput.pressEnter();
    const detail = await frameAfter(testRenderer);

    expect(detail).toContain('Hypothesis H-08');
    expect(detail).toContain(
      'Increasing the KV cache block should reduce allocator synchronization',
    );
    expect(detail).toContain('without changing queue ordering.');
    expect(detail).toContain('Decision Rejected');
    expect(detail).toContain('Round 42 · Judge pending');
    expect(detail).toContain('Round 43 · Judge fail');
    expect(controller.state.hypothesisScope).toBeNull();

    testRenderer.mockInput.pressEnter();
    const trajectory = await frameAfter(testRenderer);

    // Opening a hypothesis lands on its latest round, and the earlier ones are
    // one `[` away.
    expect(trajectory).toContain('r43');
    expect(trajectory).toContain('regression found');
    expect(trajectory).not.toContain('unrelated round 41');
    // The header names the hypothesis; the round range beside it duplicated
    // the rounds strip directly below, so only the title is up there now.
    expect(trajectory).toContain('H-08');
    expect(trajectory).not.toContain('H-08 · r42-43');
    expect(trajectory).toContain('r42');
    expect(trajectory).toContain('r43');
    // The strip covers the whole run, so rounds outside this hypothesis are
    // reachable from it; the transcript still shows only the selected round.
    expect(trajectory).toContain('r41');
    expect(controller.state.hypothesisScope).toMatchObject({id: 'H-08', rounds: [42, 43]});

    testRenderer.mockInput.pressKey('ESCAPE');
    const backToHypothesis = await frameAfterEscape(testRenderer);
    expect(backToHypothesis).toContain('Increasing the KV cache block');
    expect(backToHypothesis).not.toContain('grew the block');

    testRenderer.mockInput.pressKey('ESCAPE');
    const backToIndex = await frameAfterEscape(testRenderer);
    expect(backToIndex).toContain('Implementation Details');
    expect(controller.state.experimentLog?.selectedId).toBe('H-08');
  });

  it('runs a typed command from the log on the first Enter', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 20});
    const controller = new FakeController(initialSessionState());
    controller.experiments = [
      logEntry('H-07', 41, 41, {
        claim: 'batch the prefill step',
        resolved_outcome: 'proven',
        rounds: [{round: 41, passed: true, reviewed: true}],
      }),
    ];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    await testRenderer.mockInput.typeText('/help');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.submissions.length === 1);

    // One Enter runs the command; the table is still the view behind it.
    expect(controller.submissions).toEqual(['/help']);
    expect(controller.state.hypothesisScope).toBeNull();
    expect(await frameAfter(testRenderer)).toContain('Implementation Details');
  });

  it('opens a command overlay on the log and leaves the table behind it', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 24});
    const controller = new FakeController(initialSessionState());
    controller.experiments = [
      logEntry('H-07', 41, 41, {
        claim: 'batch the prefill step',
        resolved_outcome: 'proven',
        rounds: [{round: 41, passed: true, reviewed: true}],
      }),
    ];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    controller.publish({
      ...controller.state,
      overlay: {kind: 'help', content: 'Available commands'},
    });

    const overlaid = await frameAfter(testRenderer);
    expect(overlaid).toContain('Available commands');
    expect(controller.state.experimentLog).not.toBeNull();

    // Enter behind an overlay must not move the operator somewhere unseen.
    testRenderer.mockInput.pressEnter();
    await frameAfter(testRenderer);
    expect(controller.state.hypothesisScope).toBeNull();

    testRenderer.mockInput.pressKey('ESCAPE');
    const back = await frameAfterEscape(testRenderer);
    expect(back).toContain('Implementation Details');
    expect(back).not.toContain('Available commands');
  });

  it('swallows keys an overlay does not use instead of leaking them behind', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      overlay: {kind: 'help', content: 'Available commands'},
      core: {
        ...initialSessionState().core,
        rounds: [
          {number: 1, status: 'completed' as const},
          {number: 2, status: 'active' as const},
        ],
        transcript: [
          {
            id: 'live',
            kind: 'assistant',
            label: 'Agent',
            content: 'live output',
            roundNumber: 2,
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('Available commands'));

    // The overlay is modal: round navigation, pane focus, and typing are
    // swallowed rather than applied to the panes or the hidden command input.
    testRenderer.mockInput.pressKey('[');
    testRenderer.mockInput.pressKey('ARROW_LEFT');
    await testRenderer.mockInput.typeText('/quack');
    testRenderer.mockInput.pressEnter();
    const held = await frameAfter(testRenderer);
    expect(held).toContain('Available commands');
    expect(controller.state.overlay).not.toBeNull();
    expect(controller.state.selectedRound).toBeNull();
    expect(controller.submissions).toEqual([]);

    // Escape still closes it, and nothing typed while it was open surfaces.
    testRenderer.mockInput.pressKey('ESCAPE');
    const back = await frameAfterEscape(testRenderer);
    expect(back).not.toContain('Available commands');
    expect(back).toContain('live output');
    expect(back).not.toContain('/quack');
  });

  it('colors the outcome cell from the active theme in light and dark', async () => {
    for (const name of ['dark', 'light'] as const) {
      const theme = resolveTheme(name);
      const testRenderer = await createTestRenderer({width: 120, height: 18});
      const controller = new FakeController(initialSessionState(name));
      controller.experiments = [
        logEntry('H-07', 41, 41, {claim: 'batch the prefill step', resolved_outcome: 'proven'}),
        logEntry('H-08', 42, 43, {claim: 'bigger KV cache block', resolved_outcome: 'disproven'}),
        logEntry('H-09', 44, 44, {
          claim: 'retry with tuning',
          resolved_outcome: null,
          active: true,
        }),
      ];
      const app = createOpenTuiApp(testRenderer.renderer, controller);
      registerCleanup(testRenderer.renderer, app);
      await controller.openExperimentLog();
      await frameAfter(testRenderer);

      expect(spanColors(testRenderer, 'Accepted')?.fg).toBe(theme.success);
      expect(spanColors(testRenderer, 'Rejected')?.fg).toBe(theme.error);
      expect(spanColors(testRenderer, 'Active')?.fg).toBe(theme.warning);
      // The claim keeps body text: only the resolution is colored.
      expect(spanColors(testRenderer, 'Batch the prefill step')?.fg).toBe(theme.textPrimary);
    }
  });

  it('scrolls a log taller than the panel and keeps ordering stable', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 20});
    const controller = new FakeController(initialSessionState());
    controller.experiments = Array.from({length: 120}, (_, index) =>
      logEntry(`H-${String(index + 1).padStart(3, '0')}`, index + 1, index + 1, {
        resolved_outcome: index % 2 === 0 ? 'proven' : 'rejected',
      }),
    );
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    const first = await frameAfter(testRenderer);
    expect(first).toContain('1/120');
    expect(first).not.toContain('H-120');

    // No named page key in the mock input; send the raw terminal sequence.
    for (let index = 0; index < 12; index += 1) testRenderer.mockInput.pressKey('\x1B[6~');
    const scrolled = await frameAfter(testRenderer);
    expect(scrolled).toContain('120/120');
    expect(scrolled).not.toContain('H-001');
  });

  it('scrolls the log with the wheel, independently of the selection', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 18});
    const controller = new FakeController(initialSessionState());
    controller.experiments = Array.from({length: 60}, (_, index) =>
      logEntry(`H-${String(index + 1).padStart(3, '0')}`, index + 1, index + 1, {
        resolved_outcome: 'proven',
      }),
    );
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    const top = await frameAfter(testRenderer);
    expect(top).toContain('H-001');

    const rows = testRenderer.renderer.root.findDescendantById('experiment-rows');
    if (!(rows instanceof ScrollBoxRenderable)) throw new Error('rows were not a scroll box');
    for (let index = 0; index < 20; index += 1) {
      await testRenderer.mockMouse.scroll(60, 8, 'down');
    }
    const scrolled = await frameAfter(testRenderer);

    expect(rows.scrollTop).toBeGreaterThan(0);
    expect(scrolled).not.toContain('H-001');
    // The wheel moves the viewport, not the cursor.
    expect(controller.state.experimentLog?.selectedId).toBe('H-001');
  });

  it('lands with the chat docked beside the hypothesis table', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    const landing = await frameAfter(testRenderer);

    // Both columns at once, and the table keeps the claim it came to show.
    expect(landing).toContain('Experiment chat');
    expect(landing).toContain('Experiments');
    expect(landing).toContain('Implementation Details');
    expect(landing).toContain('Batch the prefill step');

    // Each column has its own input, inside the pane that input writes to.
    // The cursor starts in the command box, and the chat says how to reach it.
    expect(landing).toContain('Ctrl+W to type here');
    expect(paneFrameColumn(landing, 'Experiment chat')).not.toBeNull();
    expect(paneFrameColumn(landing, 'Message')).not.toBeNull();
    // Inset by the table's own border and padding rather than level with its
    // frame: the command box is a child of the pane whose keys it takes, so it
    // starts inside that pane and cannot run back under the chat beside it.
    const table = paneFrameColumn(landing, 'Experiments');
    expect(table).not.toBeNull();
    expect(paneFrameColumn(landing, 'Command')).toBe((table ?? 0) + 2);
  });

  it('keeps the message and command boxes on the same rows while the chat is docked', async () => {
    // Both bottom inputs are one row of the same landing view, so a reader
    // scanning across the screen finds one input line, not two at different
    // heights. Each state is checked because the hint above the message box
    // changes wording, and a taller hint would push the box off the row.
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    const bottomRows = (): {message: number; command: number} => {
      const message = testRenderer.renderer.root.findDescendantById('chat-dock-composer-box');
      const command = testRenderer.renderer.root.findDescendantById('command-input-box');
      if (message === undefined || command === undefined)
        throw new Error('landing composer geometry was missing');
      return {message: message.y + message.height, command: command.y + command.height};
    };

    // Unfocused: the chat says how to reach it.
    let rows = bottomRows();
    expect(rows.message).toBe(rows.command);

    // Focused: the hint becomes the send keys.
    controller.focusPane('chat');
    await frameAfter(testRenderer);
    rows = bottomRows();
    expect(rows.message).toBe(rows.command);

    // Pending: the hint says a follow-up is queued, and the title grows too.
    controller.publish({...controller.state, chatPending: true});
    expect(await frameAfter(testRenderer)).toContain('Awaiting the agent');
    rows = bottomRows();
    expect(rows.message).toBe(rows.command);

    // Narrow enough that the hint no longer fits: it truncates on its one row
    // rather than wrapping onto a second, so the boxes stay on their row.
    controller.publish({...controller.state, chatPending: false});
    testRenderer.renderer.resize(MIN_DOCK_WIDTH, 20);
    const narrow = await frameAfter(testRenderer);
    // Either focus form: this test is about the rows the boxes sit on, not
    // which of them holds focus.
    expect(narrow).toMatch(/[╭┏][─━]\s*▸?\s*Message/);
    rows = bottomRows();
    expect(rows.message).toBe(rows.command);
  });

  it('keeps the command box inside the pane whose keys it takes, in every view', async () => {
    // The box used to sit in a row of its own beneath every pane, which is why
    // it could take keys while another pane wore the marker. It is a child of
    // the pane it writes to now, and which pane that is differs by view, so
    // what has to hold is that it lands in the right one through a view change,
    // a split, each zoom that takes the left pane off screen, and the width
    // that replaces the split with a floating pane.
    const testRenderer = await createTestRenderer({width: 140, height: 24});
    const controller = splitController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    /** The command box is the foot of `paneId`, inside that pane's frame. */
    const expectCommandInside = (paneId: string): void => {
      const pane = testRenderer.renderer.root.findDescendantById(paneId);
      const command = testRenderer.renderer.root.findDescendantById('command-input-box');
      if (pane === undefined || command === undefined)
        throw new Error(`${paneId} geometry was missing`);
      expect({pane: paneId, onScreen: pane.visible && command.visible}).toEqual({
        pane: paneId,
        onScreen: true,
      });
      expect({pane: paneId, insideLeft: command.x > pane.x}).toEqual({
        pane: paneId,
        insideLeft: true,
      });
      expect({
        pane: paneId,
        insideRight: command.x + command.width < pane.x + pane.width,
      }).toEqual({pane: paneId, insideRight: true});
      // The pane's own bottom border is the row under the box, so the box ends
      // one row short of the pane. That is also why the two columns of the
      // landing view line up without a row being spent on it.
      expect({pane: paneId, foot: command.y + command.height}).toEqual({
        pane: paneId,
        foot: pane.y + pane.height - 1,
      });
    };

    // A round: the transcript.
    await frameAfter(testRenderer);
    expectCommandInside('viewport');

    // A split: still the left pane, not the one the split opened on the right.
    await controller.openPane('perf');
    await frameAfter(testRenderer);
    const rightPane = testRenderer.renderer.root.findDescendantById('right-pane');
    const command = testRenderer.renderer.root.findDescendantById('command-input-box');
    if (rightPane === undefined || command === undefined)
      throw new Error('split geometry was missing');
    expect(rightPane.visible).toBe(true);
    expect(command.x).toBeLessThan(rightPane.x);
    expectCommandInside('viewport');

    // Zoomed onto the visualization, which is now the only pane on screen.
    controller.focusPane('right');
    testRenderer.mockInput.pressKey('F4');
    await frameAfter(testRenderer);
    expect(controller.state.layout.zoomedPane).toBe('performance');
    expectCommandInside('right-pane');

    // Zoomed onto the agents pane, whose contents are rebuilt on every repaint:
    // the box has to survive the repaint, not only the first frame after it.
    testRenderer.mockInput.pressKey('F4');
    controller.closePane();
    controller.focusRound('agents');
    testRenderer.mockInput.pressKey('F4');
    await frameAfter(testRenderer);
    expect(controller.state.layout.zoomedPane).toBe('agents');
    expectCommandInside('agent-map');
    controller.publish({
      ...controller.state,
      core: {...controller.state.core, status: 'completed'},
    });
    await frameAfter(testRenderer);
    expectCommandInside('agent-map');

    // Too narrow to split: the visualization becomes a floating pane over the
    // round, the transcript keeps the box, and the floating pane keeps clear of
    // it rather than covering the surface still taking the keystrokes.
    testRenderer.mockInput.pressKey('F4');
    await controller.openPane('perf');
    testRenderer.renderer.resize(MIN_SPLIT_WIDTH - 1, 20);
    const narrow = await frameAfter(testRenderer);
    expect(narrow).toContain('best r7 1135 tok_s');
    expectCommandInside('viewport');
    const floating = testRenderer.renderer.root.findDescendantById('overlay');
    const narrowCommand = testRenderer.renderer.root.findDescendantById('command-input-box');
    if (floating === undefined || narrowCommand === undefined)
      throw new Error('fallback geometry was missing');
    expect(floating.visible).toBe(true);
    expect(floating.y + floating.height).toBeLessThanOrEqual(narrowCommand.y);

    // The landing view: the table, at a width that carries the chat beside it.
    testRenderer.renderer.resize(140, 24);
    controller.closePane();
    await controller.openExperimentLog();
    await frameAfter(testRenderer);
    expectCommandInside('experiment-log');

    // Through all of it, the box never claimed a focus of its own: it takes no
    // marker, and exactly one pane wears one.
    expect(markedPanes(paneBorders(testRenderer))).toHaveLength(1);
    expect(markedPanes(paneBorders(testRenderer))).not.toContain('▸ Command');
  });

  it('keeps the short terminal both its landing rows and the box alignment', async () => {
    // Alignment used to cost the table a row, so a terminal with none to spare
    // gave the row back and let the two boxes sit one row apart. Each box now
    // sits inside its own pane and the two panes are the same rectangle, so
    // the boxes share a row because they are siblings rather than because a
    // row was budgeted for it. That budget is what #556 measured, and it is
    // gone: nothing is bought, so a short terminal has nothing to give up.
    const testRenderer = await createTestRenderer({width: 100, height: 16});
    const controller = kickoffController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    const bottomRows = (): {message: number; command: number} => {
      const message = testRenderer.renderer.root.findDescendantById('chat-dock-composer-box');
      const command = testRenderer.renderer.root.findDescendantById('command-input-box');
      if (message === undefined || command === undefined)
        throw new Error('landing composer geometry was missing');
      return {message: message.y + message.height, command: command.y + command.height};
    };

    // The kickoff copy is whole, down to its last line, and the command
    // surface is on screen with it.
    const short = await frameAfter(testRenderer);
    expect(short).toContain('Run kickoff');
    expect(short).toContain('Form Hypothesis 1');
    expect(short).toContain('This activity becomes');
    expect(short).toMatch(/[╭┏][─━]\s*▸?\s*Command/);
    expect(short).toContain('Type /help for commands');
    // Level at the height that used to have to choose.
    expect(bottomRows().command).toBe(bottomRows().message);

    // And at the height that could afford it before, copy still whole.
    testRenderer.renderer.resize(100, 17);
    const taller = await frameAfter(testRenderer);
    expect(taller).toContain('This activity becomes');
    expect(bottomRows().command).toBe(bottomRows().message);
  });

  it('opens both suggestion menus flush on the box they complete', async () => {
    // A menu that is not touching its input reads as belonging to whatever it
    // is touching instead. The composer's hint sits above its box, so a menu
    // anchored on the composer as a whole clears the hint rather than the box.
    // The command bar is the reference on the other side of the same view.
    const testRenderer = await createTestRenderer({width: 140, height: 24});
    const controller = new FakeController(initialSessionState());
    controller.publish({...controller.state, experimentLog: emptyLog(), layout: chatFocus()});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await frameAfter(testRenderer);

    const boxTopUnder = (menuId: string, boxId: string): {menu: number; box: number} => {
      const menu = testRenderer.renderer.root.findDescendantById(menuId);
      const box = testRenderer.renderer.root.findDescendantById(boxId);
      if (menu === undefined || box === undefined)
        throw new Error(`${menuId} geometry was missing`);
      if (!menu.visible) throw new Error(`${menuId} was not on screen`);
      return {menu: menu.y + menu.height, box: box.y};
    };

    await testRenderer.mockInput.typeText('/');
    await testRenderer.waitForFrame(value => value.includes('/resume'));
    const chat = boxTopUnder('chat-dock-composer-menu', 'chat-dock-composer-box');
    expect(chat.menu).toBe(chat.box);

    // The same rule on the command bar, whose list this one is meant to match.
    controller.focusPane('left');
    await frameAfter(testRenderer);
    await testRenderer.mockInput.typeText('/');
    // Not `waitForFrame(text.includes('/pause'))`: the chat's own list is still
    // open beside this one and offers the same commands, so the text can match
    // on a frame the command list has not been laid out in yet. Wait for the
    // list itself to have taken a row per match.
    await testRenderer.waitForFrame(() => {
      const list = testRenderer.renderer.root.findDescendantById('command-input-suggestions');
      return list?.visible === true && list.height > 2;
    });
    const command = boxTopUnder('command-input-suggestions', 'command-input-box');
    expect(command.menu).toBe(command.box);
  });

  it('routes typing to whichever input Ctrl+W points at', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    await testRenderer.mockInput.typeText('this belongs in chat');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(value => value.includes('Commands start with /'));
    expect(controller.submissions).toEqual([]);
    expect(controller.chatSubmissions).toEqual([]);
    testRenderer.mockInput.pressKey('ESCAPE');
    await frameAfterEscape(testRenderer);

    testRenderer.mockInput.pressKey('w', {ctrl: true});
    await frameAfter(testRenderer);
    await testRenderer.mockInput.typeText('why is r41 slow?');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.chatSubmissions.length === 1);

    // The chat's own box took it, not the command input.
    expect(controller.chatSubmissions).toEqual(['why is r41 slow?']);
    expect(controller.submissions).toEqual([]);

    testRenderer.mockInput.pressKey('w', {ctrl: true});
    await frameAfter(testRenderer);
    await testRenderer.mockInput.typeText('/perf');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.submissions.length === 1);

    expect(controller.submissions).toEqual(['/perf']);
    expect(controller.chatSubmissions).toHaveLength(1);
  });

  it('wraps a long docked question, caps its growth, and submits every character', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 22});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    testRenderer.mockInput.pressKey('w', {ctrl: true});
    await frameAfter(testRenderer);
    const question = Array.from({length: 28}, (_, index) => `word-${index}`).join(' ');
    await testRenderer.mockInput.typeText(question);
    await frameAfter(testRenderer);

    const composer = testRenderer.renderer.root.findDescendantById('chat-dock-composer');
    const editor = testRenderer.renderer.root.findDescendantById('chat-dock-composer-editor');
    if (composer === undefined || editor === undefined)
      throw new Error('chat composer was missing');
    expect(composer.height).toBe(9);
    expect(editor.height).toBe(6);

    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.chatSubmissions.length === 1);
    expect(controller.chatSubmissions).toEqual([question]);
    await frameAfter(testRenderer);
    expect(composer.height).toBe(4);
  });

  it('keeps multiline editor keys out of experiment navigation', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20, kittyKeyboard: true});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);
    const selected = controller.state.experimentLog?.selectedId;

    testRenderer.mockInput.pressKey('w', {ctrl: true});
    await frameAfter(testRenderer);
    await testRenderer.mockInput.typeText('first line');
    testRenderer.mockInput.pressEnter({shift: true});
    await testRenderer.mockInput.typeText('second line');
    testRenderer.mockInput.pressArrow('up');
    await frameAfter(testRenderer);

    expect(controller.state.experimentLog?.selectedId).toBe(selected);
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.chatSubmissions.length >= 1);
    expect(controller.chatSubmissions).toEqual(['first line\nsecond line']);
  });

  it('contains the theme picker over a focused docked chat', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    // Put the keys on the docked chat and start a draft in its composer.
    testRenderer.mockInput.pressKey('w', {ctrl: true});
    await frameAfter(testRenderer);
    await testRenderer.mockInput.typeText('keep this');
    await frameAfter(testRenderer);
    expect(controller.state.layout.focus).toBe('chat');

    // A global command opens the picker without moving focus off the chat, so
    // the composer stays focused behind it.
    controller.openThemePicker();
    await testRenderer.waitForFrame(value => value.includes('Themes'));

    // The picker is modal: arrows drive it rather than the chat suggestions.
    testRenderer.mockInput.pressKey('ARROW_DOWN');
    await frameAfter(testRenderer);
    expect(controller.state.themePicker?.selected).toBe('light');

    // A printable key is swallowed instead of leaking into the composer.
    await testRenderer.mockInput.typeText('z');
    await frameAfter(testRenderer);

    // Escape closes the picker rather than focusing the left pane behind it.
    testRenderer.mockInput.pressKey('ESCAPE');
    await frameAfterEscape(testRenderer);
    expect(controller.state.themePicker).toBeNull();
    expect(controller.state.layout.focus).toBe('chat');

    // The draft is exactly what was typed before the picker opened: the arrow
    // and the 'z' never reached the composer.
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.chatSubmissions.length === 1);
    expect(controller.chatSubmissions).toEqual(['keep this']);
    expect(controller.submissions).toEqual([]);
  });

  it('contains a help overlay over a focused docked chat', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    testRenderer.mockInput.pressKey('w', {ctrl: true});
    await frameAfter(testRenderer);
    await testRenderer.mockInput.typeText('keep this');
    await frameAfter(testRenderer);
    expect(controller.state.layout.focus).toBe('chat');

    // /help opens an overlay without moving focus off the docked chat.
    controller.publish({
      ...controller.state,
      overlay: {kind: 'help', content: 'Available commands'},
    });
    await testRenderer.waitForFrame(value => value.includes('Available commands'));

    // The overlay is modal: a printable key is swallowed instead of leaking
    // into the composer focused behind it.
    await testRenderer.mockInput.typeText('z');
    await frameAfter(testRenderer);

    // Escape closes the overlay (goes live) rather than focusing the left pane.
    testRenderer.mockInput.pressKey('ESCAPE');
    await frameAfterEscape(testRenderer);
    expect(controller.state.overlay).toBeNull();
    expect(controller.liveCalls).toBe(1);
    expect(controller.state.layout.focus).toBe('chat');

    // The draft still holds only what was typed before the overlay: the 'z'
    // never reached the composer.
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.chatSubmissions.length === 1);
    expect(controller.chatSubmissions).toEqual(['keep this']);
  });

  it('preserves a draft when a resize moves chat from dock to modal', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    testRenderer.mockInput.pressKey('w', {ctrl: true});
    await frameAfter(testRenderer);
    await testRenderer.mockInput.typeText('draft survives the layout change');
    testRenderer.renderer.resize(80, 20);
    await frameAfter(testRenderer);
    controller.publish({...controller.state, chatOpen: true});
    const modal = await testRenderer.waitForFrame(value =>
      value.includes('draft survives the layout change'),
    );

    expect(modal).toContain('Experiment chat');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.chatSubmissions.length === 1);
    expect(controller.chatSubmissions).toEqual(['draft survives the layout change']);
  });

  it('gives the docked chat and the table the same rectangle, each holding its own input', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    const chatPane = testRenderer.renderer.root.findDescendantById('chat-pane');
    const table = testRenderer.renderer.root.findDescendantById('experiment-log');
    const composer = testRenderer.renderer.root.findDescendantById('chat-dock-composer-box');
    const command = testRenderer.renderer.root.findDescendantById('command-input-box');
    if (
      chatPane === undefined ||
      table === undefined ||
      composer === undefined ||
      command === undefined
    )
      throw new Error('landing layout was missing');
    // Two columns of one row, so they start and end on the same lines. This is
    // what makes the boxes below line up without a row being budgeted for it.
    expect(chatPane.y).toBe(table.y);
    expect(chatPane.y + chatPane.height).toBe(table.y + table.height);
    // Each input sits within the frame of the pane it writes to.
    expect(composer.x).toBeGreaterThan(chatPane.x);
    expect(composer.x + composer.width).toBeLessThan(chatPane.x + chatPane.width);
    expect(command.x).toBeGreaterThan(table.x);
    expect(command.x + command.width).toBeLessThan(table.x + table.width);
  });

  it('raises the command list out of the command input, clear of the chat', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    await testRenderer.mockInput.typeText('/pe');
    const frame = await testRenderer.waitForFrame(value => value.includes('[Tab]'));

    const suggestion = frame.split('\n').find(line => line.includes('/perf')) ?? '';
    const commandColumn = paneFrameColumn(frame, 'Command');
    // The list belongs to the box it completes, so it starts where that box
    // starts rather than running back across the chat column.
    expect(commandColumn).not.toBeNull();
    expect(suggestion.indexOf('/perf')).toBeGreaterThan(commandColumn ?? 0);
  });

  it('drops /chat from the command surface while the chat is already docked', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    await testRenderer.mockInput.typeText('/c');
    const frame = await frameAfter(testRenderer);

    // Nothing to open: the chat is the column beside the table. The thread
    // commands (/chats, /new-chat) remain, so match /chat as a whole word.
    expect(frame).not.toMatch(/\/chat\s/);
  });

  it('says when the docked chat is waiting on the agent', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    controller.publish({...controller.state, chatPending: true});

    expect(await frameAfter(testRenderer)).toContain('Awaiting the agent');
  });

  it('answers in the docked chat without covering the table', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    testRenderer.mockInput.pressKey('w', {ctrl: true});
    await frameAfter(testRenderer);
    await testRenderer.mockInput.typeText('why is r41 slow?');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.chatSubmissions.length === 1);
    controller.publish({
      ...controller.state,
      chatConversation: [
        {id: 'q', kind: 'user', label: 'You', content: 'why is r41 slow?'},
        {id: 'a', kind: 'assistant', label: 'Answer', content: 'Prefill dominates.'},
      ],
    });

    const answered = await frameAfter(testRenderer);

    expect(answered).toContain('Prefill dominates.');
    expect(answered).toContain('H-07');
    expect(answered).toContain('Implementation Details');

    const pane = testRenderer.renderer.root.findDescendantById('chat-pane');
    const scroll = testRenderer.renderer.root.findDescendantById('chat-pane-scroll');
    const turn = testRenderer.renderer.root.findDescendantById('event-q');
    if (pane === undefined || scroll === undefined || turn === undefined)
      throw new Error('docked chat geometry was missing');
    expect(scroll.x).toBe(pane.x + 2);
    expect(turn.x).toBe(scroll.x);
  });

  it('switches chat threads, swapping the transcript and the composer draft', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    controller.publish({
      ...controller.state,
      core: {
        ...controller.state.core,
        chatThreads: [
          ...controller.state.core.chatThreads,
          {
            id: 'thread-a',
            title: 'GPU stalls',
            driver: 'omnigent',
            provider: 'claude',
            model: 'opus',
          },
        ],
      },
      chatConversations: {
        default: [
          {id: 'd1', kind: 'assistant', label: 'Answer', content: 'Default thread answer.'},
        ],
        'thread-a': [
          {id: 't1', kind: 'assistant', label: 'Answer', content: 'Stalls come from prefill.'},
        ],
      },
      chatConversation: [
        {id: 'd1', kind: 'assistant', label: 'Answer', content: 'Default thread answer.'},
      ],
    });

    // Focus the docked chat and leave a half-typed question on the default thread.
    testRenderer.mockInput.pressKey('w', {ctrl: true});
    await frameAfter(testRenderer);
    await testRenderer.mockInput.typeText('half-typed question');
    let frame = await frameAfter(testRenderer);
    expect(frame).toContain('Default thread answer.');
    expect(frame).toContain('half-typed question');

    controller.switchChatThread('thread-a');
    frame = await frameAfter(testRenderer);
    // The pane is titled by the backend-owned thread title, shows the
    // thread's own transcript, and the other thread's draft is parked.
    expect(frame).toContain('GPU stalls');
    expect(frame).toContain('Stalls come from prefill.');
    expect(frame).not.toContain('Default thread answer.');
    expect(frame).not.toContain('half-typed question');

    controller.switchChatThread('default');
    frame = await frameAfter(testRenderer);
    expect(frame).toContain('Default thread answer.');
    expect(frame).not.toContain('Stalls come from prefill.');
    // The parked draft returns with its thread.
    expect(frame).toContain('half-typed question');
  });

  it('opens /model as a menu beside the composer, grouped by harness', async () => {
    const testRenderer = await createTestRenderer({width: 200, height: 30});
    const controller = new FakeController(initialSessionState());
    controller.publish({...controller.state, experimentLog: emptyLog(), layout: chatFocus()});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await frameAfter(testRenderer);

    await testRenderer.mockInput.typeText('/model');
    testRenderer.mockInput.pressEnter();
    const frame = await testRenderer.waitForFrame(value => value.includes('Harness and model'));

    // Grouped by harness, showing exactly what the backend reported. The
    // driver behind the run is never named.
    expect(frame).toContain('Codex');
    expect(frame).toContain('gpt-run');
    expect(frame).toContain('run default');
    expect(frame).toContain('Claude Code');
    expect(frame).toContain('custom model');
    expect(frame).not.toContain('agentshim');
    expect(frame).not.toContain('omnigent');

    // The menu is anchored to the composer, not centred over the screen: its
    // rows sit in the chat column, directly above the message box.
    const rows = frameRows(frame);
    const menuRow = rows.findIndex(row => row.includes('Harness and model'));
    const messageRow = rows.findIndex(row => row.includes('Message'));
    expect(menuRow).toBeGreaterThan(-1);
    expect(messageRow).toBeGreaterThan(menuRow);
    const chatColumn = rows[messageRow]?.indexOf('Message') ?? 0;
    // Both belong to the same column, so the menu never spans the whole width.
    expect(rows[menuRow]?.indexOf('Harness and model')).toBeGreaterThan(chatColumn - 6);
    // The table it was opened over is still on screen beside it, which a
    // centred dialog would have covered.
    expect(frame).toContain('Experiments');
    expect(frame).toContain('No hypotheses have been recorded yet.');
  });

  it('takes typing into a group custom entry rather than the composer', async () => {
    const testRenderer = await createTestRenderer({width: 200, height: 30});
    const controller = new FakeController(initialSessionState());
    controller.publish({...controller.state, experimentLog: emptyLog(), layout: chatFocus()});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await frameAfter(testRenderer);

    await testRenderer.mockInput.typeText('/model');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(value => value.includes('Harness and model'));
    // Down onto the codex group's custom entry.
    testRenderer.mockInput.pressKey('ARROW_DOWN');
    testRenderer.mockInput.pressKey('ARROW_DOWN');
    await testRenderer.waitForFrame(
      () => controller.state.chatMenu?.rows[controller.state.chatMenu.selected]?.kind === 'custom',
    );

    await testRenderer.mockInput.typeText('gpt-5.5');
    const frame = await testRenderer.waitForFrame(value => value.includes('gpt-5.5'));
    expect(frame).toContain('gpt-5.5');
    // The keystrokes went to the entry, not to the question underneath it.
    expect(controller.chatSubmissions).toEqual([]);

    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.state.chatMenu === null);
    expect(controller.createdThreads).toEqual([{provider: 'codex', model: 'gpt-5.5'}]);
  });

  it('lists the chat threads for /switch and switches to the highlighted one', async () => {
    const testRenderer = await createTestRenderer({width: 200, height: 30});
    const controller = new FakeController(initialSessionState());
    controller.publish({
      ...controller.state,
      experimentLog: emptyLog(),
      layout: chatFocus(),
      core: {
        ...controller.state.core,
        chatThreads: [
          ...controller.state.core.chatThreads,
          {
            id: 'thread-a',
            title: 'GPU stalls',
            driver: 'omnigent',
            provider: 'claude',
            model: 'opus',
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await frameAfter(testRenderer);

    await testRenderer.mockInput.typeText('/switch');
    testRenderer.mockInput.pressEnter();
    const frame = await testRenderer.waitForFrame(value => value.includes('Chat threads'));
    // The implicit default is named by the client; a created thread shows the
    // backend-owned title beside its harness and model. Not its driver.
    expect(frame).toContain('Experiment chat');
    expect(frame).toContain('GPU stalls');
    expect(frame).toContain('Claude Code');
    expect(frame).not.toContain('omnigent');

    // Anchored to the composer, above the message box, not centred on screen.
    const rows = frameRows(frame);
    expect(rows.findIndex(row => row.includes('Chat threads'))).toBeLessThan(
      rows.findIndex(row => row.includes('Message')),
    );

    testRenderer.mockInput.pressKey('ARROW_DOWN');
    await testRenderer.waitForFrame(value => value.includes('› GPU stalls'));
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.state.chatMenu === null);

    expect(controller.state.activeChatThreadId).toBe('thread-a');
  });

  it('/clear starts a thread on the active thread settings from the composer', async () => {
    const testRenderer = await createTestRenderer({width: 200, height: 30});
    const controller = new FakeController(initialSessionState());
    controller.publish({
      ...controller.state,
      experimentLog: emptyLog(),
      layout: chatFocus(),
      activeChatThreadId: 'thread-a',
      core: {
        ...controller.state.core,
        chatThreads: [
          ...controller.state.core.chatThreads,
          {
            id: 'thread-a',
            title: 'GPU stalls',
            driver: 'omnigent',
            provider: 'claude',
            model: 'opus',
          },
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    // The pane header names the thread and the agent answering it.
    const opening = await frameAfter(testRenderer);
    expect(opening).toContain('GPU stalls');
    expect(opening).toContain('Claude Code');

    await testRenderer.mockInput.typeText('/clear');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.clearedSettings.length === 1);

    expect(controller.clearedSettings).toEqual([{provider: 'claude', model: 'opus'}]);
    // A command, not a question: nothing was sent to the agent.
    expect(controller.chatSubmissions).toEqual([]);
  });

  it('suggests the chat commands beside the composer as they are typed', async () => {
    const testRenderer = await createTestRenderer({width: 200, height: 30});
    const controller = new FakeController(initialSessionState());
    controller.publish({...controller.state, experimentLog: emptyLog(), layout: chatFocus()});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await frameAfter(testRenderer);

    await testRenderer.mockInput.typeText('/');
    const frame = await testRenderer.waitForFrame(value => value.includes('/switch'));

    // The chat leads with its own thread commands, then the globals it forwards.
    expect(frame).toContain('/clear');
    expect(frame).toContain('/model');
    expect(frame).toContain('/switch');
    expect(frame).toContain('/pause');
    const rows = frameRows(frame);
    expect(rows.findIndex(row => row.includes('/model'))).toBeLessThan(
      rows.findIndex(row => row.includes('Message')),
    );
  });

  it('highlights and navigates the chat composer suggestions like the command bar', async () => {
    const testRenderer = await createTestRenderer({width: 200, height: 30});
    const controller = new FakeController(initialSessionState());
    controller.publish({...controller.state, experimentLog: emptyLog(), layout: chatFocus()});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await frameAfter(testRenderer);

    // /clear, /model, /switch: the composer's own command set leads, in that order.
    await testRenderer.mockInput.typeText('/');
    const first = await testRenderer.waitForFrame(value => value.includes('[Tab]'));
    expect(first).toContain('› /clear');

    testRenderer.mockInput.pressArrow('down');
    const second = await testRenderer.waitForFrame(value => value.includes('› /model'));
    expect(second).not.toContain('› /clear');

    testRenderer.mockInput.pressArrow('down');
    const third = await testRenderer.waitForFrame(value => value.includes('› /switch'));
    expect(third).not.toContain('› /model');
  });

  it('fills the highlighted chat composer suggestion into the composer with Tab', async () => {
    const testRenderer = await createTestRenderer({width: 200, height: 30});
    const controller = new FakeController(initialSessionState());
    controller.publish({...controller.state, experimentLog: emptyLog(), layout: chatFocus()});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await frameAfter(testRenderer);

    await testRenderer.mockInput.typeText('/');
    await testRenderer.waitForFrame(value => value.includes('[Tab]'));
    testRenderer.mockInput.pressArrow('down');
    await testRenderer.waitForFrame(value => value.includes('› /model'));

    testRenderer.mockInput.pressKey('TAB');
    await frameAfter(testRenderer);
    const editor = testRenderer.renderer.root.findDescendantById('chat-dock-composer-editor');
    expect(editor).toBeInstanceOf(TextareaRenderable);
    if (!(editor instanceof TextareaRenderable)) throw new Error('composer editor was missing');
    expect(editor.plainText).toBe('/model');

    // The highlighted match already equals the typed text, so a second Tab
    // does not clobber what was just filled in.
    testRenderer.mockInput.pressKey('TAB');
    await frameAfter(testRenderer);
    expect(editor.plainText).toBe('/model');
  });

  it('leaves the chat composer alone when Tab has no suggestion to complete', async () => {
    const testRenderer = await createTestRenderer({width: 200, height: 30});
    const controller = new FakeController(initialSessionState());
    controller.publish({...controller.state, experimentLog: emptyLog(), layout: chatFocus()});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await frameAfter(testRenderer);

    await testRenderer.mockInput.typeText('what is running?');
    testRenderer.mockInput.pressKey('TAB');
    await frameAfter(testRenderer);

    const editor = testRenderer.renderer.root.findDescendantById('chat-dock-composer-editor');
    expect(editor).toBeInstanceOf(TextareaRenderable);
    if (!(editor instanceof TextareaRenderable)) throw new Error('composer editor was missing');
    expect(editor.plainText).toBe('what is running?');
    expect(controller.chatSubmissions).toEqual([]);
  });

  it('dismisses the chat composer suggestion menu once nothing matches', async () => {
    const testRenderer = await createTestRenderer({width: 200, height: 30});
    const controller = new FakeController(initialSessionState());
    controller.publish({...controller.state, experimentLog: emptyLog(), layout: chatFocus()});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await frameAfter(testRenderer);

    await testRenderer.mockInput.typeText('/');
    await testRenderer.waitForFrame(value => value.includes('[Tab]'));

    await testRenderer.mockInput.typeText('zz');
    const frame = await testRenderer.waitForFrame(value => value.includes('/zz'));
    expect(frame).not.toContain('/clear');
    expect(frame).not.toContain('/model');
    expect(frame).not.toContain('/switch');

    // Nothing to complete once the menu is gone: Tab is a no-op.
    testRenderer.mockInput.pressKey('TAB');
    await frameAfter(testRenderer);
    const editor = testRenderer.renderer.root.findDescendantById('chat-dock-composer-editor');
    expect(editor).toBeInstanceOf(TextareaRenderable);
    if (!(editor instanceof TextareaRenderable)) throw new Error('composer editor was missing');
    expect(editor.plainText).toBe('/zz');
  });

  it('fills the highlighted suggestion into the modal chat composer with Tab', async () => {
    const testRenderer = await createTestRenderer({width: 90, height: 26});
    const controller = new FakeController({...initialSessionState(), chatOpen: true});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('Ask a question about'));

    await testRenderer.mockInput.typeText('/');
    await testRenderer.waitForFrame(value => value.includes('[Tab]'));
    testRenderer.mockInput.pressArrow('down');
    await testRenderer.waitForFrame(value => value.includes('› /model'));

    testRenderer.mockInput.pressKey('TAB');
    await frameAfter(testRenderer);
    const editor = testRenderer.renderer.root.findDescendantById('chat-modal-composer-editor');
    expect(editor).toBeInstanceOf(TextareaRenderable);
    if (!(editor instanceof TextareaRenderable))
      throw new Error('modal composer editor was missing');
    expect(editor.plainText).toBe('/model');
    // Only the modal moved; the docked chat is not on screen to disturb.
    expect(controller.state.chatOpen).toBe(true);
  });

  it('answers unknown composer slash input with the chat help', async () => {
    const testRenderer = await createTestRenderer({width: 200, height: 30});
    const controller = new FakeController(initialSessionState());
    controller.publish({...controller.state, experimentLog: emptyLog(), layout: chatFocus()});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await frameAfter(testRenderer);

    await testRenderer.mockInput.typeText('/threads');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.chatHelpShown.length === 1);

    expect(controller.chatHelpShown[0]).toContain('/model');
    expect(controller.submissions).toEqual([]);
    expect(controller.chatSubmissions).toEqual([]);
  });

  it('keeps the chat, the table, and the visualization on screen together', async () => {
    const testRenderer = await createTestRenderer({width: 200, height: 20});
    const controller = logController();
    controller.paneContent = 'Performance · tok_s\n    1135 ┤   ●\nbest r7 1135 tok_s';
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    controller.publish({
      ...controller.state,
      chatConversation: [{id: 'a', kind: 'assistant', label: 'Answer', content: 'Prefill.'}],
    });

    await controller.openPane('perf');
    const frame = await frameAfter(testRenderer);

    // Three columns: chat, table, visualization. None replaced another.
    expect(frame).toContain('Experiment chat');
    expect(frame).toContain('H-07');
    expect(frame).toContain('best r7 1135 tok_s');
    expect(frame).toContain('Ctrl+W: switch pane');
  });

  it('gives the row to the table alone when the chat cannot fit beside it', async () => {
    const testRenderer = await createTestRenderer({width: 84, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    const landing = await frameAfter(testRenderer);

    // Two columns would both be unreadable here, so the table keeps the row.
    expect(landing).not.toContain('Experiment chat');
    expect(landing).toContain('H-07');
    expect(controller.state.chatDockFits).toBe(false);
  });

  it('keeps the table behind the chat modal instead of the round transcript', async () => {
    const testRenderer = await createTestRenderer({width: 84, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    // What a question does where the chat cannot dock.
    controller.publish({...controller.state, chatOpen: true});

    const frame = await frameAfter(testRenderer);

    expect(frame).toContain('Experiment chat');
    expect(frame).toContain('H-07');
    // The per-round chrome belongs to a hypothesis the operator never opened.
    expect(frame).not.toContain('─ Rounds ─');
    expect(paneFrameColumn(frame, 'Agents')).toBeNull();
  });

  it('moves the pane keys onto the docked chat and back with Ctrl+W', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    testRenderer.mockInput.pressKey('w', {ctrl: true});
    const focused = await frameAfter(testRenderer);
    expect(focused).toContain('▸ Experiment chat');
    expect(focused).toContain('Ask about this run');
    expect(controller.state.layout.focus).toBe('chat');

    testRenderer.mockInput.pressKey('w', {ctrl: true});
    await frameAfter(testRenderer);
    expect(controller.state.layout.focus).toBe('left');
  });

  it('leaves the table its own keys while the chat is docked', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    controller.experiments = [
      logEntry('H-07', 41, 41, {claim: 'batch the prefill step', resolved_outcome: 'proven'}),
      logEntry('H-08', 42, 42, {claim: 'bigger KV cache block', resolved_outcome: 'disproven'}),
    ];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    // Arrows still belong to the table, docked chat or not.
    testRenderer.mockInput.pressKey('ARROW_DOWN');
    await frameAfter(testRenderer);
    expect(controller.state.experimentLog?.selectedId).toBe('H-08');
  });

  it('renders the transcript and the visualization side by side', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = splitController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await controller.openPane('perf');
    const frame = await frameAfter(testRenderer);

    // Both live at once; neither obscures the other.
    expect(frame).toContain('batched the prefill step');
    expect(frame).toContain('best r7 1135 tok_s');
    expect(frame).toContain('Performance');
    expect(frame).toContain('Ctrl+W: switch pane');
  });

  it('moves focus between panes and shows which one has it', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = splitController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openPane('perf');

    const onRight = await frameAfter(testRenderer);
    expect(onRight).toContain('▸ Performance');
    const theme = resolveTheme('dark');
    expect(spanColors(testRenderer, '▸ Performance')?.fg).toBe(theme.borderFocus);

    testRenderer.mockInput.pressKey('w', {ctrl: true});
    const onLeft = await frameAfter(testRenderer);

    expect(controller.state.layout.focus).toBe('left');
    expect(onLeft).toContain('▸ Transcript');
  });

  it('marks exactly one focused pane across the hypothesis layout', async () => {
    const testRenderer = await createTestRenderer({width: 200, height: 20});
    const controller = logController();
    controller.paneContent = 'Performance · tok_s\nbest r7 1135 tok_s';
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await controller.openPane('perf');

    expect(await frameAfter(testRenderer)).toContain('▸ Performance');
    testRenderer.mockInput.pressKey('w', {ctrl: true});
    expect(await frameAfter(testRenderer)).toContain('▸ Experiment chat');
    testRenderer.mockInput.pressKey('w', {ctrl: true});
    expect(await frameAfter(testRenderer)).toContain('▸ Experiments');
    expect(controller.state.layout.focus).toBe('left');
  });

  it('lights one pane and never the command box, in every built-in theme', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 24});
    const controller = splitController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await frameAfter(testRenderer);

    for (const name of THEME_NAMES) {
      controller.setTheme(name);
      const theme = resolveTheme(name);
      await frameAfter(testRenderer);

      // The transcript holds the round view's keys by default. Every other
      // titled surface, the shared command box included, stays neutral.
      expect(paneBorders(testRenderer)).toEqual({
        Agents: theme.border,
        '▸ Transcript': theme.borderFocus,
        Command: theme.border,
      });

      testRenderer.mockInput.pressKey('ARROW_LEFT');
      await frameAfter(testRenderer);
      // The treatment moves whole: the pane that gains it and the pane that
      // loses it are repainted in the same frame.
      expect(paneBorders(testRenderer)).toEqual({
        '▸ Agents': theme.borderFocus,
        Transcript: theme.border,
        Command: theme.border,
      });

      testRenderer.mockInput.pressKey('ARROW_RIGHT');
      await frameAfter(testRenderer);
    }
  });

  it('keeps the command box neutral while a proven hypothesis is on screen', async () => {
    // Issue #433: the command box was painted in the success colour, so it read
    // as the focused surface while the keys were on the table beside it. A
    // success outcome on screen must not put any focus treatment on that box.
    const testRenderer = await createTestRenderer({width: 160, height: 22});
    const controller = logController();
    controller.experiments = [
      logEntry('H-07', 41, 41, {claim: 'batch the prefill step', resolved_outcome: 'disproven'}),
      logEntry('H-08', 42, 42, {claim: 'bigger KV cache block', resolved_outcome: 'proven'}),
    ];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    for (const name of THEME_NAMES) {
      controller.setTheme(name);
      const theme = resolveTheme(name);
      const frame = await frameAfter(testRenderer);

      // The success colour really is on screen, which is the state #433 was
      // reported in rather than a hypothetical one.
      expect(frame).toContain('Accepted');
      expect(spanColors(testRenderer, 'Accepted')?.fg).toBe(theme.success);

      const borders = paneBorders(testRenderer);
      expect(borders['▸ Experiments']).toBe(theme.borderFocus);
      expect(borders['Command']).toBe(theme.border);
      expect(borders['Command']).not.toBe(theme.success);
      expect(borders['Command']).not.toBe(theme.borderFocus);
    }
  });

  it('leaves every round pane neutral when a fallback modal takes the keys', async () => {
    // Under the split width the visualization has no pane to be focused in, so
    // it falls back to a modal and takes the keys with it. Reading `roundFocus`
    // instead of `focusedPane` left the Agents pane lit behind that modal. The
    // terminal is tall enough that the modal starts below the pane headings it
    // would otherwise cover.
    const testRenderer = await createTestRenderer({width: 90, height: 40});
    const controller = splitController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    testRenderer.mockInput.pressKey('ARROW_LEFT');
    await frameAfter(testRenderer);
    expect(controller.state.roundFocus).toBe('agents');

    await controller.openPane('perf');
    const frame = await frameAfter(testRenderer);

    expect(controller.state.layout.focus).toBe('right');
    expect(frame).not.toContain('▸ Agents');
    expect(frame).not.toContain('▸ Transcript');
    const theme = resolveTheme('dark');
    const borders = paneBorders(testRenderer);
    expect(borders['Agents']).toBe(theme.border);
    expect(borders['Transcript']).toBe(theme.border);
  });

  it('moves the focus treatment on a click, not only on a keystroke', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 24});
    const controller = splitController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    testRenderer.mockInput.pressKey('ARROW_LEFT');
    let frame = await frameAfter(testRenderer);
    const theme = resolveTheme('dark');
    expect(paneBorders(testRenderer)['▸ Agents']).toBe(theme.borderFocus);

    const lines = frame.split('\n');
    const row = lines.findIndex(line => line.includes('batched the prefill step'));
    const column = (lines[row]?.indexOf('batched the prefill step') ?? 0) + 2;
    await testRenderer.mockMouse.click(column, row);
    frame = await frameAfter(testRenderer);

    expect(controller.state.roundFocus).toBe('transcript');
    expect(paneBorders(testRenderer)).toEqual({
      Agents: theme.border,
      '▸ Transcript': theme.borderFocus,
      Command: theme.border,
    });
  });

  it('moves the focus treatment onto the expanded todo list, under every parent focus', async () => {
    // Ctrl+T routes Up/Down to the list, so the list is the surface taking the
    // keys and has to be the one wearing the marker. Whichever pane held them
    // before goes back to rest, whether that is the transcript, the agent
    // graph, or a visualization beside them.
    const testRenderer = await createTestRenderer({width: 140, height: 32});
    const controller = todoController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    const theme = resolveTheme('dark');
    await testRenderer.waitForFrame(value => value.includes('Todo 1/3'));

    for (const parent of ['▸ Transcript', '▸ Agents', '▸ Performance'] as const) {
      if (parent === '▸ Agents') controller.focusRound('agents');
      if (parent === '▸ Performance') await controller.openPane('perf');
      await frameAfter(testRenderer);
      expect(markedPanes(paneBorders(testRenderer))).toEqual([parent]);

      testRenderer.mockInput.pressKey('t', {ctrl: true});
      const expanded = await testRenderer.waitForFrame(value =>
        value.includes('Re-run the benchmark'),
      );
      expect(expanded).toContain('▸ Todo 1/3');
      const borders = paneBorders(testRenderer);
      expect(markedPanes(borders)).toEqual(['▸ Todo 1/3']);
      expect(borders['▸ Todo 1/3']).toBe(theme.borderFocus);
      // The pane it opened over is back at rest: one surface takes the keys,
      // so one surface says so.
      expect(borders[parent.slice(2)]).toBe(theme.border);

      testRenderer.mockInput.pressKey('t', {ctrl: true});
      await frameAfter(testRenderer);
    }
  });

  it('keeps zoom and the expanded todo list from claiming the row together', async () => {
    // The list holds the keys but is as tall as its own contents, so F4 has
    // nothing to give it and leaves the row alone. A zoomed pane still has the
    // strip under it, so opening the list there moves the marker onto it the
    // same way, rather than lighting both.
    const testRenderer = await createTestRenderer({width: 140, height: 32});
    const controller = todoController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('Todo 1/3'));
    testRenderer.mockInput.pressKey('t', {ctrl: true});
    await testRenderer.waitForFrame(value => value.includes('Re-run the benchmark'));

    testRenderer.mockInput.pressKey('F4');
    const open = await frameAfter(testRenderer);

    expect(controller.state.layout.zoomedPane).toBeNull();
    expect(open).toContain('batched the prefill step');
    expect(markedPanes(paneBorders(testRenderer))).toEqual(['▸ Todo 1/3']);

    testRenderer.mockInput.pressKey('t', {ctrl: true});
    await frameAfter(testRenderer);
    testRenderer.mockInput.pressKey('F4');
    await frameAfter(testRenderer);

    expect(controller.state.layout.zoomedPane).toBe('transcript');
    expect(markedPanes(paneBorders(testRenderer))).toEqual(['▸ Transcript']);

    testRenderer.mockInput.pressKey('t', {ctrl: true});
    await frameAfter(testRenderer);

    expect(markedPanes(paneBorders(testRenderer))).toEqual(['▸ Todo 1/3']);
  });

  it('shows the narrow fallback as the focused visualization, not a Command box', async () => {
    // Under the split width the visualization is drawn through the modal, so
    // that modal is the keyboard target and has to carry the pane's own title
    // and its focus marker. It used to render as a generic Command box in the
    // info border, leaving the treatment on a RightPaneView nobody could see.
    const testRenderer = await createTestRenderer({width: 90, height: 40});
    const controller = splitController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await controller.openPane('perf');
    const frame = await frameAfter(testRenderer);

    expect(controller.state.layout.focus).toBe('right');
    expect(frame).toContain('best r7 1135 tok_s');
    const theme = resolveTheme('dark');
    const borders = paneBorders(testRenderer);
    expect(markedPanes(borders)).toEqual(['▸ Performance']);
    expect(borders['▸ Performance']).toBe(theme.borderFocus);
    // The shared command box is still on screen under the modal and stays
    // neutral, so the only lit frame is the one taking the keys.
    expect(borders['Command']).toBe(theme.border);
  });

  it('keeps the composer inside a focused chat pane at rest', async () => {
    // The treatment belongs to the pane frame. The composer is a box inside
    // that frame rather than a pane of its own, so a focused chat used to draw
    // the marker and the focused border twice, one nested in the other.
    const testRenderer = await createTestRenderer({width: 160, height: 24});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    controller.focusPane('chat');
    await frameAfter(testRenderer);

    const theme = resolveTheme('dark');
    const borders = paneBorders(testRenderer);
    expect(markedPanes(borders)).toEqual(['▸ Experiment chat']);
    expect(borders['Message']).toBe(theme.border);
  });

  it('shows the hypothesis title as a heading in the detail view', async () => {
    const testRenderer = await createTestRenderer({width: 200, height: 22});
    const controller = logController();
    controller.experiments = [
      logEntry('H-01', 1, 1, {
        title: 'Batch prefill to cut latency',
        claim: 'batching the prefill step reduces latency',
      }),
      logEntry('H-02', 2, 2, {claim: 'untitled legacy hypothesis'}),
    ];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    controller.publish({
      ...controller.state,
      hypothesisDetail: {entryKey: 'H-01', selectedRound: 1},
    });

    const titled = await testRenderer.waitForFrame(value => value.includes('Hypothesis H-01'));
    expect(titled).toContain('Batch prefill to cut latency');

    controller.publish({
      ...controller.state,
      hypothesisDetail: {entryKey: 'H-02', selectedRound: 2},
    });
    const untitled = await testRenderer.waitForFrame(value => value.includes('Hypothesis H-02'));
    expect(untitled).toContain('untitled legacy hypothesis');
    expect(untitled).not.toContain('Batch prefill to cut latency');
  });

  it('keeps the newest design round reachable in the pane at 100x30', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 30});
    const controller = logController();
    controller.paneContent = renderDesignSummary(
      Array.from({length: 10}, (_, index) => ({
        round: index + 1,
        files: [{path: `src/round-${index + 1}.rs`, change: 'modified' as const}],
        hypothesisId: 'H-07',
        title: 'Batch the prefill step',
        record: null,
      })),
    );
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await controller.openPane('design');

    // Ten rounds is 24 lines. The modal this replaced was 60% of 30 rows and
    // did not scroll, so the newest rounds were unreachable; the pane is the
    // full column and scrolls, so every round can be read.
    //
    // Both ends no longer sit on screen together at 30 rows: the curated
    // header is housed in a bordered box, which costs two rows the bare status
    // line did not. The oldest round is there on open and the newest is a page
    // away, rather than lost as it was in the modal.
    const frame = await testRenderer.waitForFrame(value =>
      value.includes('Design changes by round'),
    );
    expect(frame).toContain('Round 1 · H-07');

    for (let index = 0; index < 3; index += 1) testRenderer.mockInput.pressKey('\x1B[6~');
    const scrolled = await frameAfter(testRenderer);
    expect(scrolled).toContain('Round 10 · H-07');
    expect(scrolled).toContain('src/round-10.rs');
  });

  it('annotates the selected round with its design changes once the log loads', async () => {
    const testRenderer = await createTestRenderer({width: 200, height: 30});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    controller.publish({
      ...controller.state,
      hypothesisDetail: {entryKey: 'H-07', selectedRound: 41},
    });

    // Before the design log loads there is no section to mislabel.
    const bare = await testRenderer.waitForFrame(value => value.includes('Hypothesis H-07'));
    expect(bare).not.toContain('CHANGES');

    controller.publish({
      ...controller.state,
      designLog: [
        {
          round: 41,
          commit: 'abcdef1234567890',
          files: [
            {path: 'src/ring.rs', change: 'added'},
            {path: 'src/lib.rs', change: 'renamed', renamed_from: 'src/queue.rs'},
            {path: 'src/ffi.rs', change: 'deleted'},
          ],
        },
      ],
    });

    const annotated = await testRenderer.waitForFrame(value => value.includes('ROUND 41 CHANGES'));
    expect(annotated).toContain('+ src/ring.rs');
    expect(annotated).toContain('→ src/lib.rs (was src/queue.rs)');
    expect(annotated).toContain('- src/ffi.rs');
    // Stage facts stay on the round's own row, stated once.
    expect(annotated).not.toContain('Outcome proven');
  });
  it('keeps the hypothesis title through a no-op state notification', async () => {
    // Clicking an already-focused log area calls focusPane('left'), which
    // returns the same state; the controller notifies every listener anyway.
    // The pane then has to redraw the title it is actually showing, not the
    // index title, which the detail body underneath would contradict.
    const testRenderer = await createTestRenderer({width: 200, height: 22});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    controller.publish({
      ...controller.state,
      hypothesisDetail: {entryKey: 'H-07', selectedRound: 41},
    });
    await testRenderer.waitForFrame(value => value.includes('Hypothesis H-07'));

    controller.focusPane('left');
    const frame = await frameAfter(testRenderer);

    expect(frame).toContain('▸ Hypothesis H-07');
    expect(frame).not.toContain('Experiments');
    expect(frame).toContain('batch the prefill step');
  });

  it('opens hypothesis detail from a row click and keeps pane clicks routed', async () => {
    const testRenderer = await createTestRenderer({width: 200, height: 22});
    const controller = logController();
    controller.experiments = [
      logEntry('H-07', 41, 41, {claim: 'batch the prefill step'}),
      logEntry('H-08', 42, 43, {
        claim: 'increase the cache block',
        rounds: [
          {round: 42, passed: true, reviewed: true},
          {round: 43, passed: false, reviewed: true},
        ],
      }),
    ];
    controller.paneContent = 'Performance · tok_s\nbest r7 1135 tok_s';
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await controller.openPane('perf');
    let frame = await frameAfter(testRenderer);
    expect(controller.state.layout.focus).toBe('right');

    // A table row opens the hypothesis summary directly and gives it the full
    // content row, closing an unrelated visualization.
    let lines = frame.split('\n');
    let row = lines.findIndex(line => line.includes('H-08'));
    let column = (lines[row]?.indexOf('H-08') ?? 0) + 2;
    await testRenderer.mockMouse.click(column, row);
    frame = await frameAfter(testRenderer);
    expect(controller.state.layout.focus).toBe('left');
    expect(controller.state.experimentLog?.selectedId).toBe('H-08');
    expect(controller.state.hypothesisDetail).toEqual({entryKey: 'H-08', selectedRound: 43});
    expect(controller.state.layout.right).toBeNull();
    expect(frame).toContain('▸ Hypothesis H-08');
    testRenderer.mockInput.pressKey('ARROW_UP');
    frame = await frameAfter(testRenderer);
    expect(controller.state.hypothesisDetail?.selectedRound).toBe(42);

    lines = frame.split('\n');
    row = lines.findIndex(line => line.includes('Round 42'));
    column = (lines[row]?.indexOf('Round 42') ?? 0) + 2;
    await testRenderer.mockMouse.click(column, row);
    await frameAfter(testRenderer);
    expect(controller.state.hypothesisScope).toMatchObject({id: 'H-08'});
    expect(controller.state.selectedRound).toBe(42);

    testRenderer.mockInput.pressKey('ESCAPE');
    await frameAfterEscape(testRenderer);
    expect(controller.state.hypothesisDetail?.selectedRound).toBe(42);
    testRenderer.mockInput.pressKey('ESCAPE');
    await frameAfterEscape(testRenderer);
    await controller.openPane('perf');
    frame = await frameAfter(testRenderer);

    // The chart body is inside a scroll surface. Clicking it focuses the
    // performance pane, so Escape is routed there and closes it.
    frame = testRenderer.captureCharFrame();
    lines = frame.split('\n');
    row = lines.findIndex(line => line.includes('best r7 1135 tok_s'));
    column = (lines[row]?.indexOf('best r7 1135 tok_s') ?? 0) + 2;
    await testRenderer.mockMouse.click(column, row);
    await frameAfter(testRenderer);
    expect(controller.state.layout.focus).toBe('right');
    testRenderer.mockInput.pressKey('ESCAPE');
    await frameAfterEscape(testRenderer);
    expect(controller.state.layout.right).toBeNull();
  });

  it('zooms and restores every pane without replacing its model state', async () => {
    const testRenderer = await createTestRenderer({width: 200, height: 20});
    const controller = logController();
    controller.paneContent = 'Performance · tok_s\nbest r7 1135 tok_s';
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await controller.openPane('perf');
    const right = controller.state.layout.right;

    testRenderer.mockInput.pressKey('F4');
    const performance = await frameAfter(testRenderer);
    expect(performance).toContain('best r7 1135 tok_s');
    expect(performance).not.toContain('H-07');
    expect(performance).not.toContain('Experiment chat');

    testRenderer.mockInput.pressKey('F4');
    const restored = await frameAfter(testRenderer);
    expect(restored).toContain('H-07');
    expect(restored).toContain('Experiment chat');
    expect(controller.state.layout.right).toBe(right);

    testRenderer.mockInput.pressKey('w', {ctrl: true});
    testRenderer.mockInput.pressKey('F4');
    const chat = await frameAfter(testRenderer);
    expect(chat).toContain('Experiment chat');
    expect(chat).not.toContain('H-07');

    testRenderer.mockInput.pressKey('F4');
    testRenderer.mockInput.pressKey('w', {ctrl: true});
    testRenderer.mockInput.pressKey('F4');
    const experiments = await frameAfter(testRenderer);
    expect(experiments).toContain('H-07');
    expect(experiments).not.toContain('Experiment chat');
    expect(experiments).not.toContain('best r7 1135 tok_s');
  });

  it('zooms the selected agents or transcript pane in a round', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = splitController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    controller.focusRound('agents');
    testRenderer.mockInput.pressKey('F4');
    const agents = await frameAfter(testRenderer);
    expect(agents).toContain('▸ Agents');
    expect(agents).not.toContain('batched the prefill step');

    testRenderer.mockInput.pressKey('F4');
    controller.focusRound('transcript');
    testRenderer.mockInput.pressKey('F4');
    const transcript = await frameAfter(testRenderer);
    expect(transcript).toContain('batched the prefill step');
    expect(transcript).not.toContain('Agents');
  });

  it('closes the pane with Escape and restores the full-width transcript', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = splitController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openPane('perf');
    await frameAfter(testRenderer);

    testRenderer.mockInput.pressKey('ESCAPE');
    const frame = await frameAfterEscape(testRenderer);

    expect(controller.state.layout.right).toBeNull();
    expect(frame).not.toContain('best r7 1135 tok_s');
    expect(frame).toContain('batched the prefill step');
    expect(frame).toContain('Agents');
  });

  it('falls back to the modal below the split threshold and recovers on resize', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = splitController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openPane('perf');
    const wide = await frameAfter(testRenderer);
    expect(wide).toContain('Ctrl+W: switch pane');

    testRenderer.renderer.resize(80, 20);
    const narrow = await frameAfter(testRenderer);

    // Single view, chart in the modal it used before the split existed.
    expect(narrow).toContain('best r7 1135 tok_s');
    expect(narrow).toContain('Command');
    expect(narrow).not.toContain('Ctrl+W: switch pane');

    testRenderer.renderer.resize(140, 20);
    const recovered = await frameAfter(testRenderer);

    expect(recovered).toContain('Ctrl+W: switch pane');
    expect(recovered).toContain('batched the prefill step');
  });

  it('puts a visualization beside the experiment log rather than replacing it', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = new FakeController(initialSessionState());
    controller.publish({...controller.state, experimentLog: initialSessionState().experimentLog});
    controller.experiments = [
      logEntry('H-07', 41, 41, {claim: 'batch the prefill step', resolved_outcome: 'proven'}),
    ];
    controller.paneContent = 'Performance · tok_s\nbest r41 1135 tok_s';
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await controller.openPane('perf');

    const frame = await frameAfter(testRenderer);

    expect(frame).toContain('H-07');
    expect(frame).toContain('best r41 1135 tok_s');
    // The table gives up its widest column to make room, rather than vanishing.
    expect(frame).toContain('Hypothesis');
  });

  it('styles both panes and the focus indicator from the selected theme', async () => {
    const theme = resolveTheme('solarized-light');
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = splitController();
    controller.publish({...controller.state, themeName: 'solarized-light'});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openPane('perf');
    await frameAfter(testRenderer);

    expect(spanColors(testRenderer, '▸ Performance')?.fg).toBe(theme.borderFocus);
    expect(spanColors(testRenderer, 'best r7 1135 tok_s')?.fg).toBe(theme.textPrimary);

    controller.cyclePaneFocus();
    await frameAfter(testRenderer);

    // Focus moved to the transcript, so the pane border drops back to the
    // ordinary border colour and the transcript takes the focus colour.
    expect(spanColors(testRenderer, 'Performance')?.fg).toBe(theme.border);
  });

  /**
   * Focus has to survive a palette that cannot express it. `borderFocus` and
   * `border` are within 1.5x of each other in five of the eight themes, so the
   * marker in the title's reserved gutter and the frame style are what actually
   * carry the change, and neither may move the label they sit beside.
   */
  it.each(
    THEME_NAMES.map(name => [name] as const),
  )('marks focus in %s without moving the title', async (name: ThemeName) => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = splitController();
    controller.publish({...controller.state, themeName: name});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openPane('perf');

    const onRight = await frameAfter(testRenderer);
    controller.cyclePaneFocus();
    const onLeft = await frameAfter(testRenderer);

    const focused = paneFrameCorner(onRight, 'Performance');
    const resting = paneFrameCorner(onLeft, 'Performance');
    expect(focused).toBeDefined();
    expect(resting).toBeDefined();
    // The marker is the non-colour channel, and it is independent of the theme.
    expect(onRight).toContain(paneTitle('Performance', true));
    expect(onLeft).toContain(paneTitle('Performance', false));
    // The frame does not change: focus never alters a pane's shape.
    expect(focused?.glyph).toBe(resting?.glyph);
    // And the label itself does not move when either changes.
    expect(focused?.column).toBe(resting?.column);
    expect(onRight.split('\n')[0]?.length).toBe(onLeft.split('\n')[0]?.length);
  });

  it('keeps the pane current while the run advances', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = splitController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openPane('perf');
    expect(await frameAfter(testRenderer)).toContain('best r7 1135 tok_s');

    // A later round lands and the controller refetches; the pane redraws in
    // place rather than needing to be reopened.
    controller.paneContent = 'Performance · tok_s\n    1180 ┤    ●\nbest r8 1180 tok_s';
    await controller.openPane('perf');
    const updated = await frameAfter(testRenderer);

    expect(updated).toContain('best r8 1180 tok_s');
    expect(updated).not.toContain('best r7 1135 tok_s');
  });

  it('shows an explicit placeholder for records with no hypothesis id', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 16});
    const controller = new FakeController(initialSessionState());
    controller.experiments = [
      logEntry('(unidentified)', 1, 1, {identified: false, claim: null, resolved_outcome: null}),
    ];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    const frame = await frameAfter(testRenderer);
    expect(frame).toContain('—');
  });

  it('says so plainly when a run has no hypotheses yet', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 16});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    const frame = await frameAfter(testRenderer);
    expect(frame).toContain('No hypotheses have been recorded yet.');
    expect(frame).toContain('once the orchestrator has planned a round');
    // The log is the root view: nothing offers a way out of it.
    expect(frame).not.toContain('Esc');
  });

  it('uses the empty hypotheses screen as a truthful planning kickoff', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 16});
    const controller = kickoffController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    const frame = await frameAfter(testRenderer);
    expect(frame).toContain('Planning Hypothesis 1 · Round 1');
    expect(frame).toContain('Run kickoff');
    expect(frame).toContain('Decide whether profiling is needed');
    expect(frame).toContain('Profile if needed');
    expect(frame).toContain('Form Hypothesis 1');
    expect(frame).toMatch(/1m \d+s/);
    expect(frame).toContain('This activity becomes');
    expect(frame).not.toContain('No hypotheses have been recorded yet.');
  });

  it('keeps earlier unassociated rounds visible and openable during kickoff', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 18});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        status: 'running',
        rounds: [{number: 1, status: 'completed'}],
        phases: [
          {kind: 'orchestrator', status: 'active', roundNumber: 2, roundLabel: 'round-2-pre'},
        ],
        transcript: [
          {id: 'r1', kind: 'assistant', content: 'earlier unassociated turn', roundNumber: 1},
          {id: 'r2', kind: 'assistant', content: 'planning turn', roundNumber: 2},
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    const kickoff = await frameAfter(testRenderer);
    expect(kickoff).toContain('Planning Hypothesis 1');
    expect(kickoff).toContain('Round 2');
    expect(kickoff).toContain('Round 1 · recorded agent turns · no hypothesis');

    testRenderer.mockInput.pressEnter();
    const round = await frameAfter(testRenderer);
    expect(round).toContain('earlier unassociated turn');
    expect(round).not.toContain('planning turn');
  });

  it('indexes and opens recorded rounds that have no hypothesis', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 18});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        rounds: [{number: 7, status: 'completed'}],
        transcript: [
          {id: 'r6', kind: 'assistant', content: 'other round', roundNumber: 6},
          {id: 'r7', kind: 'assistant', content: 'unindexed turn', roundNumber: 7},
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    expect(await frameAfter(testRenderer)).toContain(
      'Round 7 · recorded agent turns · no hypothesis',
    );

    testRenderer.mockInput.pressEnter();
    const detail = await frameAfter(testRenderer);
    expect(detail).toContain('Round 7');
    expect(detail).toContain('unindexed turn');
    expect(detail).not.toContain('other round');

    testRenderer.mockInput.pressKey('ESCAPE');
    expect(await frameAfterEscape(testRenderer)).toContain(
      'Round 7 · recorded agent turns · no hypothesis',
    );
  });

  it('keeps later hypothesis planning below the existing history', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 18});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        status: 'running',
        phases: [
          {kind: 'orchestrator', status: 'active', roundNumber: 42, roundLabel: 'round-42-plan'},
        ],
      },
    });
    controller.experiments = [logEntry('H-07', 41, 41, {claim: 'batch the prefill step'})];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    const frame = await frameAfter(testRenderer);
    expect(frame).toContain('CURRENT ACTIVITY');
    expect(frame).toContain('Planning Hypothesis 2 · forming it · Round 42');
    expect(frame).toContain('H-07');
    expect(frame.indexOf('H-07')).toBeLessThan(frame.indexOf('Planning Hypothesis 2'));
    expect(frame).not.toContain('UNASSOCIATED ROUNDS');
  });

  it('renders shuffled hypotheses and unassociated rounds in one ascending index', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        status: 'running',
        rounds: [
          {number: 4, status: 'completed'},
          {number: 2, status: 'completed'},
          {number: 5, status: 'active'},
        ],
        phases: [
          {kind: 'orchestrator', status: 'active', roundNumber: 5, roundLabel: 'round-5-plan'},
        ],
      },
    });
    controller.experiments = [
      logEntry('H-03', 3, 3, {claim: 'third'}),
      logEntry('H-01', 1, 1, {claim: 'first'}),
    ];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    const frame = await frameAfter(testRenderer);
    const positions = [
      frame.indexOf('H-01'),
      frame.indexOf('Round 2 · recorded'),
      frame.indexOf('H-03'),
      frame.indexOf('Round 4 · recorded'),
      frame.indexOf('Planning Hypothesis 3'),
    ];
    expect(positions.every(position => position >= 0)).toBe(true);
    expect(positions).toEqual([...positions].sort((left, right) => left - right));
  });

  it('labels an explicit profiler phase without claiming it will happen earlier', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 16});
    const controller = new FakeController({
      ...initialSessionState(),
      core: {
        ...initialSessionState().core,
        status: 'running',
        phases: [
          {kind: 'profiler', status: 'active', roundNumber: 1, roundLabel: 'round-1-profiler'},
        ],
      },
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    expect(await frameAfter(testRenderer)).toContain('Profile before Hypothesis 1');
  });
});

describe('header hierarchy', () => {
  const runState = (status: CoreRunStatus): SessionState => ({
    ...initialSessionState(),
    core: {
      ...initialSessionState().core,
      status,
      agentKind: 'implementer',
      roundLabel: 'round-1-retry-2-implementer',
      usage: {inputTokens: 223_000, contextWindow: 400_000, model: 'claude-opus-5'},
    },
  });

  it('draws each header role in its own tone rather than all in one accent', async () => {
    const theme = resolveTheme('dark');
    const testRenderer = await createTestRenderer({width: 100, height: 20});
    const controller = new FakeController(runState('completed'));
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('223k/400k context'));

    // Four tones on one line, which is the point: before this the renderer
    // reported one accent-coloured span for the whole header.
    expect(spanColors(testRenderer, 'VibeSys')?.fg).toBe(theme.accent);
    expect(spanColors(testRenderer, 'completed')?.fg).toBe(theme.success);
    expect(spanColors(testRenderer, 'implementing')?.fg).toBe(theme.textPrimary);
    expect(spanColors(testRenderer, '223k/400k context')?.fg).toBe(theme.textMuted);
  });

  it('recolours the run state when the run ends badly', async () => {
    const theme = resolveTheme('dark');
    const testRenderer = await createTestRenderer({width: 100, height: 20});
    const controller = new FakeController(runState('running'));
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('223k/400k context'));
    expect(spanColors(testRenderer, 'running')?.fg).toBe(theme.textPrimary);

    controller.publish({
      ...controller.state,
      core: {...controller.state.core, status: 'failed'},
    });
    await testRenderer.waitForVisualIdle();

    expect(spanColors(testRenderer, 'failed')?.fg).toBe(theme.error);
  });

  it('reads the header against the cell it is actually drawn on, in every theme', async () => {
    // The frame paints a surface of its own, so `theme.canvas` is not what the
    // header's text sits on and a floor measured against it checks a
    // background nothing draws. The background here is read back off the
    // rendered cell rather than named, which is also what pins `app.ts` and
    // `headerSpanStyle` to one answer about where the header sits.
    const themes = listThemes();
    expect(themes).toHaveLength(8);
    for (const theme of themes) {
      const testRenderer = await createTestRenderer({width: 90, height: 20});
      const controller = new FakeController({
        ...initialSessionState(theme.name),
        core: {...initialSessionState(theme.name).core, status: 'completed'},
      });
      const app = createOpenTuiApp(testRenderer.renderer, controller);
      registerCleanup(testRenderer.renderer, app);
      await testRenderer.waitForFrame(value => value.includes('VibeSys'));

      const floor = theme.name.startsWith('high-contrast') ? 7 : 4.5;
      for (const word of ['VibeSys', 'completed']) {
        const drawn = spanColors(testRenderer, word);
        expect({theme: theme.name, word, bg: drawn?.bg}).toEqual({
          theme: theme.name,
          word,
          bg: headerBackground(theme),
        });
        const ratio = drawn === undefined ? 0 : contrastRatio(drawn.fg, drawn.bg);
        expect({theme: theme.name, word, readable: ratio >= floor}).toEqual({
          theme: theme.name,
          word,
          readable: true,
        });
      }
    }
  });

  it('keeps the header whole in a terminal too narrow for all of it', async () => {
    // One renderable per span means the row can shrink, and a shrinking row
    // puts an ellipsis through every span at once (`V...ys·r...ng`) instead of
    // the single cut the width budget decided on.
    const testRenderer = await createTestRenderer({width: 24, height: 20});
    const controller = new FakeController(runState('running'));
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('VibeSys'));
    expect(frame).toContain('VibeSys · running');
    expect(frame).not.toContain('V...');
  });
});
/**
 * A fill lives on an inner box, so a border ring shows what is behind the box.
 *
 * `docs/contributing/tui-conventions.md` states the rule and the reason.
 * `BoxRenderable` hands `OptimizedBuffer.drawBox` one `backgroundColor` for the
 * whole rectangle and the buffer is write-only, so a fill set on a bordered box
 * paints the ring as well: the painted rectangle ends up one cell larger than
 * the drawn line on all four sides, the line sits in a solid block, and under a
 * rounded arc the fill paints the outside of the curve and squares the corner
 * back off. That is #642.
 *
 * Asserted by walking the constructed tree rather than by reading a frame, so
 * it covers every box the app builds, including the modals that stay
 * `visible: false` until they are opened, and so a call site added later fails
 * this without anyone remembering to extend a list.
 */
describe('box fills', () => {
  /**
   * The boxes that keep an outer fill, which is the one exception.
   *
   * Each of them floats over other content, so the fill has to reach the border
   * ring or what is behind shows through it. A list rather than a structural
   * test because floating is not a property a box carries: the agent map's
   * cards are absolutely positioned too, and they are laid out on a canvas
   * rather than over anything, so `position` would exempt them as well. Every
   * entry here is an exception someone reviewed, which is the point of holding
   * them in one place.
   */
  const OVERLAY_IDS = new Set([
    'overlay',
    'theme-picker',
    'chat-overlay',
    'command-input-suggestions',
  ]);

  /** One per composer, and a composer is built per surface, so match the suffix. */
  function isOverlay(id: string): boolean {
    return OVERLAY_IDS.has(id) || id.endsWith('-composer-menu');
  }

  /** Every box under `renderable`, itself included. */
  function* boxesIn(renderable: Renderable): Generator<BoxRenderable> {
    if (renderable instanceof BoxRenderable) yield renderable;
    for (const child of renderable.getChildren()) yield* boxesIn(child);
  }

  /** The id of every box that breaks the rule, so a failure names the site. */
  function offenders(renderable: Renderable): string[] {
    const found: string[] = [];
    for (const box of boxesIn(renderable)) {
      const sides = getBorderSides(box.border);
      if (!sides.top && !sides.right && !sides.bottom && !sides.left) continue;
      // `transparent` is the default and is what "no fill of its own" means
      // here: `drawBox` leaves the rectangle alone, so the ring keeps whatever
      // was already under it.
      if (box.backgroundColor.a === 0) continue;
      // An overlay may keep the outer fill, but only square. The fill reaches
      // the ring either way, and a ring of fill under a rounded arc is #642.
      if (isOverlay(box.id) && box.borderStyle !== 'rounded') continue;
      found.push(box.id);
    }
    return found;
  }

  function boxIds(renderable: Renderable): string[] {
    return [...boxesIn(renderable)].map(box => box.id);
  }

  /** A box that is there and is actually painting something. */
  function isPainted(root: Renderable, id: string): boolean {
    const box = root.findDescendantById(id);
    return box instanceof BoxRenderable && box.backgroundColor.a > 0;
  }

  /** Enough state to build the boxes that are made per entry, not at startup. */
  function shapeController(): FakeController {
    return new FakeController({
      ...initialSessionState(),
      selectedAgentKind: 'implementer',
      selectedEntryId: 'card',
      core: {
        ...initialSessionState().core,
        status: 'running',
        agentKind: 'implementer',
        rounds: [{number: 1, status: 'active'}],
        phases: [
          {kind: 'optimizer', status: 'completed', roundNumber: 1, roundLabel: 'round 1'},
          {kind: 'implementer', status: 'active', roundNumber: 1, roundLabel: 'round 1'},
        ],
        // Stamped with the agent and the round, because `visibleConversation`
        // filters on both and an unstamped entry would be dropped before a
        // card was ever built for it.
        transcript: [
          {
            id: 'card',
            kind: 'assistant',
            agentKind: 'implementer',
            roundNumber: 1,
            label: 'implementer · round 1',
            content: 'a bordered card',
          },
          {
            id: 'status',
            kind: 'status',
            agentKind: 'implementer',
            roundNumber: 1,
            content: 'a status line',
          },
        ],
      },
    });
  }

  it('keeps a fill off every bordered box that is not an overlay', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 30});
    const app = createOpenTuiApp(testRenderer.renderer, shapeController());
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('a bordered card'));
    const root = testRenderer.renderer.root;

    // Coverage first, because a walk that reached nothing passes vacuously.
    // One box from each side of the rule, plus the two that are built per
    // entry rather than once at startup.
    const ids = boxIds(root);
    expect(ids).toContain('header-frame'); // bordered, fill moved inwards
    expect(ids).toContain('experiment-log'); // a pane, the same way
    expect(ids).toContain('theme-picker'); // an overlay, built here, never opened
    expect(ids).toContain('event-card'); // a transcript card, now a top-edge rule
    expect(ids).toContain('event-status'); // borderless, so it keeps its own fill
    expect(ids.some(id => id.startsWith('agent-implementer-'))).toBe(true);

    expect(offenders(root)).toEqual([]);
  });

  it('moves a fill inwards rather than dropping it', async () => {
    // The other half of the rule, and the one a walk for offenders cannot see:
    // deleting a fill satisfies that walk too, and it was how this branch first
    // tried to answer #642. Every site that used to fill its own rectangle has
    // to still be painting one, on a layer inside the border.
    const testRenderer = await createTestRenderer({width: 120, height: 30});
    const app = createOpenTuiApp(testRenderer.renderer, shapeController());
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('a bordered card'));
    const root = testRenderer.renderer.root;

    // 'event-card-fill' is deliberately absent: #565 removed the transcript
    // card's border and its fill together, so there is no longer a rounded
    // corner to keep a fill out of, and nothing left to move inwards.
    for (const id of ['header-fill', 'experiment-log-fill']) {
      expect([id, isPainted(root, id)]).toEqual([id, true]);
    }
  });

  it('keeps the rule in the stacked agent layout', async () => {
    // Two things only a narrow terminal builds. The agent pane falls back to
    // stacked rows when there is no width to lay the graph out, and a
    // visualization below `MIN_SPLIT_WIDTH` is drawn through the overlay,
    // which then wears the pane treatment. That second one is the case a
    // construction-time rule cannot cover on its own: `applyPaneFocus` sets
    // the frame at render time, so the rounded overlay it could reintroduce
    // only exists after a frame.
    const testRenderer = await createTestRenderer({width: 60, height: 30});
    const controller = shapeController();
    controller.paneContent = 'Performance · tok_s\nbest r1 1135 tok_s';
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('implementer'));
    await controller.openPane('perf');
    await testRenderer.waitForFrame(value => value.includes('1135 tok_s'));
    const root = testRenderer.renderer.root;

    expect(boxIds(root)).toContain('agent-implementer');
    expect(offenders(root)).toEqual([]);
  });

  it('flags a bordered box that fills its own rectangle', async () => {
    // The walks above are only evidence while they can fail. These are the
    // controls: a box built the way #642 reported, and the overlay exception
    // used to take a rounded frame back, which is the same bug again.
    const {renderer} = await createTestRenderer({width: 20, height: 6});
    cleanup.push(() => renderer.destroy());
    for (const [id, borderStyle] of [
      ['bordered-and-filled', 'rounded'],
      ['overlay', 'rounded'],
    ] as const) {
      const box = new BoxRenderable(renderer, {
        id,
        width: 6,
        height: 3,
        border: true,
        borderStyle,
        backgroundColor: '#ffffff',
      });
      renderer.root.add(box);
      cleanup.push(() => box.destroyRecursively());
    }

    expect(offenders(renderer.root)).toEqual(['bordered-and-filled', 'overlay']);
  });
});

/**
 * The experiment log settles synchronously, so there is no later frame to wait
 * for. Flush pending work, then read the single settled frame.
 */
async function frameAfter(testRenderer: TestRendererSetup): Promise<string> {
  await testRenderer.flush();
  return testRenderer.captureCharFrame();
}

/** A captured frame as its screen rows, for asserting where something sits. */
function frameRows(frame: string): string[] {
  return frame.split('\n');
}

/** Whether the round tab row is on screen. */
function tabsVisible(testRenderer: TestRendererSetup): boolean {
  return testRenderer.renderer.root.findDescendantById('round-tabs')?.visible === true;
}

/** The agents pane's columns of every screen row, drawn for `state` at `width`. */
async function agentPaneText(width: number, state: SessionState): Promise<string> {
  const testRenderer = await createTestRenderer({width, height: 30});
  const app = createOpenTuiApp(testRenderer.renderer, new FakeController(state));
  registerCleanup(testRenderer.renderer, app);
  const frame = await testRenderer.waitForFrame(value => value.includes('judge'));
  const pane = testRenderer.renderer.root.findDescendantById('agent-map');
  if (pane === undefined) throw new Error('agent pane was missing');
  return frameRows(frame)
    .map(row => row.slice(pane.x, pane.x + pane.width))
    .join('\n');
}

/** The third of three rounds, with three stages and its transcript on screen. */
function threeStageRound(): SessionState {
  const base = initialSessionState();
  return {
    ...base,
    selectedRound: 3,
    core: {
      ...base.core,
      rounds: [
        {number: 1, status: 'completed'},
        {number: 2, status: 'completed'},
        {number: 3, status: 'active'},
      ],
      phases: [
        {kind: 'orchestrator', status: 'completed', roundNumber: 3, roundLabel: 'round-3-pre'},
        {kind: 'implementer', status: 'active', roundNumber: 3, roundLabel: 'round-3'},
        {kind: 'judge', status: 'pending', roundNumber: 3, roundLabel: null},
      ],
      transcript: [
        {
          id: 'live',
          kind: 'assistant',
          label: 'implementer',
          content: 'live output',
          agentKind: 'implementer',
          roundNumber: 3,
        },
      ],
    },
  };
}

/** The landing view, which is where the chat is a docked pane. */
function emptyLog(): NonNullable<SessionState['experimentLog']> {
  return {entries: [], selectedId: null, pending: false, error: null};
}

/** Puts the keys on the docked chat, which is where its commands are typed. */
function chatFocus(): SessionState['layout'] {
  return {right: null, focus: 'chat', zoomedPane: null};
}

/**
 * A bare ESC is held by the stdin parser until its escape-sequence timeout
 * expires, so the key lands a beat after it is pressed.
 */
async function frameAfterEscape(testRenderer: TestRendererSetup): Promise<string> {
  await new Promise(resolve => setTimeout(resolve, 40));
  return frameAfter(testRenderer);
}

/** A client on the landing view, with one hypothesis to show. */
function logController(): FakeController {
  const controller = new FakeController({
    ...initialSessionState(),
    core: {
      ...initialSessionState().core,
      status: 'running',
      rounds: [{number: 41, status: 'completed'}],
    },
  });
  controller.publish({...controller.state, experimentLog: initialSessionState().experimentLog});
  controller.experiments = [
    logEntry('H-07', 41, 41, {
      claim: 'batch the prefill step',
      resolved_outcome: 'proven',
      judge_verdict: 'pass',
      rounds: [{round: 41, passed: true, reviewed: true}],
    }),
  ];
  return controller;
}

/**
 * A run that has started planning but has no hypotheses yet, which is the
 * landing view at its tallest: the kickoff panel is the widest and longest
 * thing the table ever shows.
 */
function kickoffController(): FakeController {
  const planningStartedAt = new Date(Date.now() - 65_000).toISOString();
  return new FakeController({
    ...initialSessionState(),
    core: {
      ...initialSessionState().core,
      status: 'running',
      phases: [
        {
          kind: 'orchestrator',
          status: 'active',
          roundNumber: 1,
          roundLabel: 'round-1-pre',
          startedAt: planningStartedAt,
        },
      ],
    },
  });
}

function splitController(): FakeController {
  const controller = new FakeController({
    ...initialSessionState(),
    core: {
      ...initialSessionState().core,
      status: 'running',
      rounds: [{number: 7, status: 'active'}],
      transcript: [
        {
          id: 'a',
          kind: 'assistant',
          label: 'implementer · round 7',
          content: 'batched the prefill step',
          roundNumber: 7,
        },
      ],
    },
  });
  controller.paneContent = 'Performance · tok_s\n    1135 ┤   ●\nbest r7 1135 tok_s';
  return controller;
}

/** A live round with a todo list, and a visualization to open beside it. */
function todoController(): FakeController {
  const controller = new FakeController({
    ...initialSessionState(),
    core: {
      ...initialSessionState().core,
      status: 'running',
      agentKind: 'implementer',
      rounds: [{number: 7, status: 'active'}],
      todos: [
        {
          agentKind: 'implementer',
          roundNumber: null,
          items: [
            {content: 'Profile the hot loop', status: 'completed'},
            {content: 'Vectorize the kernel', status: 'in_progress'},
            {content: 'Re-run the benchmark', status: 'pending'},
          ],
        },
      ],
      transcript: [
        {
          id: 'a',
          kind: 'assistant',
          label: 'implementer · round 7',
          content: 'batched the prefill step',
          roundNumber: 7,
        },
      ],
    },
  });
  controller.paneContent = 'Performance · tok_s\nbest r7 1135 tok_s';
  return controller;
}

function logEntry(
  id: string,
  firstRound: number,
  lastRound: number,
  overrides: Partial<HypothesisEntry> = {},
): HypothesisEntry {
  return {
    hypothesis_id: id,
    identified: true,
    first_round: firstRound,
    last_round: lastRound,
    rounds: [],
    kept: false,
    active: false,
    ...overrides,
  };
}

/**
 * Every titled box on screen, as its rendered title and border colour. The
 * focus treatment is a frame style, a border colour, and a `▸` in the title's
 * reserved gutter, so keying on the title as drawn asserts the marker and the
 * colour together, and asserting the whole record catches a second surface
 * lighting up as well as the right one going dark.
 */
function paneBorders(testRenderer: TestRendererSetup): Record<string, string> {
  const borders: Record<string, string> = {};
  for (const line of testRenderer.captureSpans().lines) {
    for (const span of line.spans) {
      // All three corner glyphs a titled box can open with: rounded, the
      // heavy one a focused pane would draw, and square. Square is in the set
      // because an overlay keeps an outer fill and so draws a square frame
      // (tui-conventions.md), and the narrow-terminal visualization is drawn
      // through the overlay while it is also the performance pane.
      for (const match of span.text.matchAll(/[╭┏┌][─━]([^─━╮┓┐]+)[─━]/g)) {
        borders[(match[1] ?? '').trim()] = rgbToHex(span.fg).toLowerCase();
      }
    }
  }
  return borders;
}

/**
 * The titles wearing the focus marker, which is never more than one: the marker
 * says where the keys go, and the keys go to one surface.
 */
function markedPanes(borders: Record<string, string>): string[] {
  return Object.keys(borders).filter(title => title.startsWith('▸'));
}

/** The frame glyphs a pane draws, in either border style. */
const FRAME_VERTICAL = /[│┃]$/;
const FRAME_VERTICALS = /[│┃]/g;
const FRAME_BOTTOM_RIGHT = /[╯┛]$/;

/**
 * The column a pane's frame starts at, or null when the pane is not on screen.
 *
 * Focus swaps the frame's glyphs and the marker in the title's reserved
 * gutter, so a test about layout has to match on neither.
 */
function paneFrameColumn(frame: string, label: string): number | null {
  return paneFrameCorner(frame, label)?.column ?? null;
}

/** The frame's top-left glyph and column, which together say how it is drawn. */
function paneFrameCorner(
  frame: string,
  label: string,
): {glyph: string; column: number} | undefined {
  const top = new RegExp(`[╭┏][─━] [▸ ] ${label} `);
  for (const line of frame.split('\n')) {
    const match = top.exec(line);
    if (match !== null) return {glyph: match[0][0] ?? '', column: match.index};
  }
  return undefined;
}

function spanColors(
  testRenderer: TestRendererSetup,
  needle: string,
): {fg: string; bg: string} | undefined {
  for (const line of testRenderer.captureSpans().lines) {
    for (const span of line.spans) {
      if (span.text.includes(needle)) {
        return {fg: rgbToHex(span.fg).toLowerCase(), bg: rgbToHex(span.bg).toLowerCase()};
      }
    }
  }
  return undefined;
}

/** A run long enough that building every card would block the first frame. */
function hugeTranscriptState(entries: number): SessionState {
  const initial = initialSessionState();
  return {
    ...initial,
    core: {
      ...initial.core,
      transcript: Array.from({length: entries}, (_, index) => ({
        id: `entry-${index}`,
        kind: 'status' as const,
        content: `event ${index}`,
      })),
    },
  };
}

/** One typed tool turn, the shape `tool_call`/`tool_result` events fold into. */
function toolCallState(
  entry: Omit<SessionState['core']['transcript'][number], 'id' | 'kind' | 'label' | 'content'> & {
    content?: string;
  },
): SessionState {
  const initial = initialSessionState();
  return {
    ...initial,
    core: {
      ...initial.core,
      transcript: [
        {id: 'tool', kind: 'tool' as const, label: 'implementer · round 1', content: '', ...entry},
      ],
    },
  };
}

function registerCleanup(
  renderer: Awaited<ReturnType<typeof createTestRenderer>>['renderer'],
  app: OpenTuiApp,
): void {
  cleanup.push(() => {
    app.destroy();
    renderer.destroy();
  });
}

function clipboardReturning(
  result: ClipboardCopyResult,
): SelectionClipboard & {readonly calls: number} {
  let calls = 0;
  return {
    get calls(): number {
      return calls;
    },
    copySelection(): ClipboardCopyResult {
      calls += 1;
      return result;
    },
  };
}

describe('modal scrim', () => {
  const helpOverlay = {kind: 'help' as const, content: 'Available commands'};

  function behindTheModal(themeName: ThemeName): SessionState {
    const initial = initialSessionState(themeName);
    return {
      ...initial,
      core: {
        ...initial.core,
        status: 'running',
        transcript: [
          {id: 'behind', kind: 'assistant', label: 'implementer', content: 'transcript behind it'},
        ],
      },
    };
  }

  it.each(
    THEME_NAMES.map(name => [name] as const),
  )('%s dims every cell around the modal and leaves the modal at full contrast', async (themeName: ThemeName) => {
    const theme = resolveTheme(themeName);
    const {color, strength} = scrim(theme);
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const controller = new FakeController(behindTheModal(themeName));
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('transcript behind it'));
    await testRenderer.waitForVisualIdle();
    const closed = frameCells(testRenderer);
    const headerBefore = requireSpanColors(testRenderer, 'VibeSys');

    controller.publish({...controller.state, overlay: helpOverlay});
    await testRenderer.waitForFrame(value => value.includes('Available commands'));
    await testRenderer.waitForVisualIdle();
    const open = frameCells(testRenderer);

    const box = boxOf(testRenderer, 'overlay');
    // Every band around the modal, so a scrim confined to the box region
    // fails here rather than passing on the rows it happens to cover.
    const dimmed = {above: 0, below: 0, left: 0, right: 0};
    const moved: number[] = [];
    for (const [row, column, before, after] of cellsOutside(closed, open, box)) {
      if (before.char !== after.char) {
        moved.push(row);
        continue;
      }
      // A blank cell has no visible foreground, and the scrim claims it.
      if (before.char !== ' ') {
        expect(channelDistance(after.fg, mix(before.fg, color, strength))).toBeLessThanOrEqual(1);
      }
      expect(channelDistance(after.bg, mix(before.bg, color, strength))).toBeLessThanOrEqual(1);
      if (after.fg === before.fg && after.bg === before.bg) continue;
      if (row < box.y) dimmed.above += 1;
      else if (row >= box.y + box.height) dimmed.below += 1;
      else if (column < box.x) dimmed.left += 1;
      else dimmed.right += 1;
    }
    expect(dimmed.above).toBeGreaterThan(0);
    expect(dimmed.below).toBeGreaterThan(0);
    expect(dimmed.left).toBeGreaterThan(0);
    expect(dimmed.right).toBeGreaterThan(0);
    // The header gains its Escape hint, so its own rows are allowed to change
    // characters. Nothing else outside the box moves: the scrim repaints cells,
    // it does not lay anything out. The header sits in its own housing since
    // #571, so its rows are read off the frame rather than assumed to be row 0.
    const headerFrame = boxOf(testRenderer, 'header-frame');
    const headerRows = new Set(
      Array.from({length: headerFrame.height}, (_, offset) => headerFrame.y + offset),
    );
    expect(moved.filter(row => !headerRows.has(row))).toEqual([]);

    const floor = themeName.startsWith('high-contrast') ? 7 : 4.5;
    const modalBody = requireSpanColors(testRenderer, 'Available commands');
    // The modal keeps a fill and squares its border (#642), and that fill is
    // now the one background the theme has (#574), so this reads `canvas`
    // where it used to read the separate elevated surface. The property is
    // unchanged: the modal is the one surface the scrim does not paint over.
    expect(modalBody).toEqual({fg: theme.textPrimary, bg: theme.canvas});
    expect(contrastRatio(modalBody.fg, modalBody.bg)).toBeGreaterThanOrEqual(floor);
    // Background text recedes below the floor the theme guarantees, and stays
    // above the point where the run behind the modal would be erased.
    const headerAfter = requireSpanColors(testRenderer, 'VibeSys');
    expect(contrastRatio(headerBefore.fg, headerBefore.bg)).toBeGreaterThanOrEqual(floor);
    const recessed = contrastRatio(headerAfter.fg, headerAfter.bg);
    expect(recessed).toBeLessThan(floor);
    expect(recessed).toBeGreaterThanOrEqual(1.9);
  });

  it('restores the frame exactly when the modal closes', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const controller = new FakeController(behindTheModal('dark'));
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('transcript behind it'));
    await testRenderer.waitForVisualIdle();
    const before = frameCells(testRenderer);

    controller.publish({...controller.state, overlay: helpOverlay});
    await testRenderer.waitForFrame(value => value.includes('Available commands'));
    await testRenderer.waitForVisualIdle();
    controller.publish({...controller.state, overlay: null});
    await testRenderer.waitForFrame(value => !value.includes('Available commands'));
    await testRenderer.waitForVisualIdle();

    expect(frameCells(testRenderer)).toEqual(before);
  });

  it('raises one scrim under whichever modals are open, and never over them', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const controller = new FakeController(behindTheModal('dark'));
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('transcript behind it'));
    const scrimBox = boxOf(testRenderer, 'overlay-scrim');
    // One scrim under every modal: two of them stacked must not dim each other,
    // and a command ack over the chat still needs the background held down.
    expect(scrimBox.zIndex).toBeLessThan(boxOf(testRenderer, 'chat-overlay').zIndex);
    expect(scrimBox.zIndex).toBeLessThan(boxOf(testRenderer, 'overlay').zIndex);
    expect(scrimBox.zIndex).toBeLessThan(boxOf(testRenderer, 'theme-picker').zIndex);
    expect(scrimBox.visible).toBe(false);

    for (const modal of [
      {chatOpen: true},
      {overlay: helpOverlay},
      {themePicker: {selected: 'dark' as const}},
    ]) {
      controller.publish({...behindTheModal('dark'), ...modal});
      await testRenderer.flush();
      expect(scrimBox.visible).toBe(true);
      // The whole terminal, so the dim never stops at the pane the modal is in.
      expect([scrimBox.x, scrimBox.y, scrimBox.width, scrimBox.height]).toEqual([0, 0, 100, 24]);
      controller.publish(behindTheModal('dark'));
      await testRenderer.flush();
      expect(scrimBox.visible).toBe(false);
    }
  });
});

/** The renderable behind a screen region, for asserting what a scrim covers. */
function boxOf(testRenderer: TestRendererSetup, id: string): Renderable {
  const found = testRenderer.renderer.root.findDescendantById(id);
  if (found === undefined) throw new Error(`no renderable with id ${id}`);
  return found;
}

/** The colors of the span carrying `needle`, which the caller expects on screen. */
function requireSpanColors(
  testRenderer: TestRendererSetup,
  needle: string,
): {fg: string; bg: string} {
  const colors = spanColors(testRenderer, needle);
  if (colors === undefined) throw new Error(`no rendered span contains ${needle}`);
  return colors;
}

/** The captured frame as a grid of cells, for asserting what a scrim repaints. */
function frameCells(
  testRenderer: TestRendererSetup,
): Array<Array<{char: string; fg: string; bg: string}>> {
  return testRenderer.captureSpans().lines.map(line => {
    const row: Array<{char: string; fg: string; bg: string}> = [];
    for (const span of line.spans) {
      const fg = rgbToHex(span.fg).toLowerCase();
      const bg = rgbToHex(span.bg).toLowerCase();
      for (const char of span.text) row.push({char, fg, bg});
    }
    return row;
  });
}

type Cell = {char: string; fg: string; bg: string};

/** Every screen cell the modal does not cover, paired across the two frames. */
function* cellsOutside(
  closed: Cell[][],
  open: Cell[][],
  box: Renderable,
): Generator<[number, number, Cell, Cell]> {
  for (const [row, cells] of closed.entries()) {
    for (const [column, before] of cells.entries()) {
      const covered =
        row >= box.y && row < box.y + box.height && column >= box.x && column < box.x + box.width;
      const after = open[row]?.[column];
      if (covered || after === undefined) continue;
      yield [row, column, before, after];
    }
  }
}

/** The largest per-channel gap between two hex colors. */
function channelDistance(left: string, right: string): number {
  const channels = (hex: string): number[] =>
    [1, 3, 5].map(at => Number.parseInt(hex.slice(at, at + 2), 16));
  const [first, second] = [channels(left), channels(right)];
  return Math.max(...first.map((value, index) => Math.abs(value - (second[index] ?? 0))));
}

class FakeController implements SessionController {
  readonly #listeners = new Set<(state: SessionState) => void>();
  readonly submissions: string[] = [];
  readonly chatSubmissions: string[] = [];
  /** Chat-scoped help the composer answered unknown slash input with. */
  readonly chatHelpShown: string[] = [];
  readonly createdThreads: ChatThreadSettings[] = [];
  readonly clearedSettings: (ChatThreadSettings | null)[] = [];
  /** Stands in for the backend's `query.chat_options` response. */
  chatOptions: ChatOptions = {
    providers: [
      {
        provider: 'codex',
        models: [
          {model: 'gpt-run', source: 'run', default: true},
          {model: 'gpt-5.6-sol', source: 'suggested', default: false},
        ],
      },
      {provider: 'claude', models: [{model: 'claude-opus-5', source: 'suggested', default: false}]},
    ],
  };
  liveCalls = 0;
  /** How many times the reveal path asked for history the client does not hold. */
  historyLoads = 0;

  /**
   * Tests that exercise the transcript start past the landing view. The
   * experiment log has its own tests that open it explicitly.
   */
  constructor(state: SessionState) {
    this.state = {...state, experimentLog: null};
  }

  state: SessionState;

  publish(state: SessionState): void {
    // The real controller normalizes focus on every state change; the fake has
    // to as well, or tests pass on a focus the client could never be in.
    this.state = normalizeFocus(state);
    for (const listener of this.#listeners) listener(this.state);
  }

  start(): Promise<void> {
    return Promise.resolve();
  }
  stop(): Promise<void> {
    return Promise.resolve();
  }
  submitCommand(value: string): Promise<void> {
    if (!value.trim().startsWith('/')) {
      this.publish(
        reportError(this.state, 'Commands start with /. Use Experiment chat for questions.', {
          scope: 'input',
        }),
      );
      return Promise.resolve();
    }
    this.submissions.push(value);
    if (value.trim() === '/chat') {
      this.state = {...this.state, chatOpen: true, overlay: null};
      this.#notify();
    }
    if (value.trim() === '/theme') this.openThemePicker();
    return Promise.resolve();
  }
  closeChat(): void {
    this.state = {...this.state, chatOpen: false};
    this.#notify();
  }
  switchChatThread(threadId: string): void {
    this.publish(switchChatThread(this.state, threadId));
  }
  openChatResumeMenu(): void {
    this.publish(openChatResumeMenu(this.state));
  }
  openChatModelMenu(): Promise<void> {
    // Mocked protocol response: the client renders exactly what it receives.
    this.publish(setChatModelMenuOptions(openChatModelMenu(this.state), this.chatOptions));
    return Promise.resolve();
  }
  clearChatThread(): Promise<void> {
    this.clearedSettings.push(activeChatThreadSettings(this.state));
    return Promise.resolve();
  }
  moveChatMenuSelection(delta: number): void {
    this.publish(moveChatMenuSelection(this.state, delta));
  }
  confirmChatMenu(): Promise<void> {
    const row = selectedChatMenuRow(this.state);
    if (row === null) return Promise.resolve();
    if (row.kind === 'thread') {
      this.switchChatThread(row.threadId);
      return Promise.resolve();
    }
    if (row.kind !== 'model' && row.kind !== 'custom') return Promise.resolve();
    const model = row.kind === 'custom' ? chatMenuCustomModel(this.state).trim() : row.model;
    if (model === '') return Promise.resolve();
    this.createdThreads.push({provider: row.provider, model});
    this.publish(closeChatMenu(this.state));
    return Promise.resolve();
  }
  closeChatMenu(): void {
    this.publish(closeChatMenu(this.state));
  }
  typeChatMenuCustomModel(text: string): void {
    this.publish(setChatMenuCustomModel(this.state, chatMenuCustomModel(this.state) + text));
  }
  backspaceChatMenuCustomModel(): void {
    this.publish(setChatMenuCustomModel(this.state, chatMenuCustomModel(this.state).slice(0, -1)));
  }
  setTheme(themeName: ThemeName): void {
    this.state = {...this.state, themeName};
    this.#notify();
  }
  openThemePicker(): void {
    this.publish({...this.state, overlay: null, themePicker: {selected: this.state.themeName}});
  }
  moveThemeSelection(delta: number): void {
    this.publish(moveThemeSelection(this.state, delta));
  }
  applySelectedTheme(): void {
    const picker = this.state.themePicker;
    if (picker !== null) this.publish(setTheme(this.state, picker.selected));
  }
  closeThemePicker(): void {
    this.publish(closeThemePicker(this.state));
  }
  /** Records the reveal path asking the backend for history it does not hold. */
  loadOlderHistory(): Promise<boolean> {
    this.historyLoads += 1;
    return Promise.resolve(this.state.core.historyAfterSequence > 0);
  }
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
        this.chatHelpShown.push(chatHelpText());
        return Promise.resolve();
      default:
        return this.submitCommand(text);
    }
  }

  sendChat(value: string): Promise<void> {
    this.chatSubmissions.push(value);
    this.state = {
      ...this.state,
      chatConversation: [
        ...this.state.chatConversation,
        {id: 'chat-user', kind: 'user', label: 'You', content: value},
        {
          id: 'chat-analysis',
          kind: 'analysis',
          label: 'Chat analysis',
          content: 'Inspecting configuration events',
        },
        {
          id: 'chat-tool',
          kind: 'tool',
          label: 'Chat tool',
          content: '→ Read(run-events.jsonl)\nFound config_load_failed',
          toolCall: '→ Read(run-events.jsonl)\n',
          toolResponse: 'Found config_load_failed',
        },
        {
          id: 'chat-answer',
          kind: 'assistant',
          label: 'Answer',
          content: 'Recorded diagnostic: agent.toml was not found.',
        },
      ],
    };
    this.#notify();
    return Promise.resolve();
  }
  live(): void {
    this.liveCalls += 1;
    this.state = {...this.state, overlay: null, selectedRound: null, selectedAgentKind: null};
    for (const listener of this.#listeners) listener(this.state);
  }
  selectNextAgent(): void {
    const current = this.state.selectedAgentKind;
    const visibleRound =
      this.state.selectedRound ??
      this.state.core.rounds.find(round => round.status === 'active')?.number ??
      null;
    const phases = this.state.core.phases.filter(phase => phase.roundNumber === visibleRound);
    const index = current === null ? -1 : phases.findIndex(phase => phase.kind === current);
    const next = phases[(index + 1 + phases.length) % phases.length];
    this.state = {...this.state, selectedAgentKind: next?.kind ?? null, overlay: null};
    for (const listener of this.#listeners) listener(this.state);
  }
  selectPreviousAgent(): void {
    this.selectNextAgent();
  }
  selectNextRound(): void {
    this.publish(selectNextRound(this.state));
  }
  selectPreviousRound(): void {
    this.publish(selectPreviousRound(this.state));
  }
  selectAgent(kind: string): void {
    this.publish(selectAgent(this.state, kind));
  }
  selectNextEntry(delta: number, id?: string): void {
    this.publish(selectNextEntry(this.state, delta, id));
  }
  clearEntrySelection(): void {
    this.publish(clearEntrySelection(this.state));
  }
  clearAgentSelection(): void {
    this.publish(clearAgentSelection(this.state));
  }
  focusRound(focus: RoundFocus): void {
    this.publish(focusRound(this.state, focus));
  }
  selectNextTodo(delta: number): void {
    this.publish(selectNextTodo(this.state, delta));
  }
  selectRound(roundNumber: number): void {
    this.state = {...this.state, selectedRound: roundNumber, selectedAgentKind: null};
    for (const listener of this.#listeners) listener(this.state);
  }
  #promptToggle: (() => void) | null = null;
  onTogglePrompt(handler: () => void): void {
    this.#promptToggle = handler;
  }
  togglePrompt(): void {
    this.#promptToggle?.();
  }
  toggleTodos(): void {
    this.publish({...this.state, todosExpanded: !this.state.todosExpanded});
  }

  /** Rows the fake server returns for query.experiments. */
  experiments: HypothesisEntry[] = [];

  openExperimentLog(): Promise<void> {
    this.publish(setExperiments(openExperimentLog(this.state), this.experiments));
    return Promise.resolve();
  }
  moveExperimentSelection(delta: number): void {
    this.publish(moveExperimentSelection(this.state, delta));
  }
  openHypothesisDetail(entryKey?: string): void {
    this.publish(openHypothesisDetail(this.state, entryKey));
  }
  moveHypothesisRoundSelection(delta: number): void {
    this.publish(moveHypothesisRoundSelection(this.state, delta));
  }
  selectExperimentActivity(): void {
    this.publish(selectExperimentActivity(this.state));
  }
  enterExperimentDrilldown(): void {
    this.publish(enterExperimentDrilldown(this.state));
  }
  openPane(view: PaneView): Promise<void> {
    this.publish(setPaneContent(openPane(this.state, view), view, this.paneContent));
    return Promise.resolve();
  }
  closePane(): void {
    this.publish(closePane(this.state));
  }
  closeOverlays(): void {
    this.publish(closeOverlays(this.state));
  }
  dismissErrorBanner(): void {
    this.publish(dismissErrorBanner(this.state));
  }
  cyclePaneFocus(): void {
    this.publish(cyclePaneFocus(this.state));
  }
  focusPane(focus: PaneFocus): void {
    this.publish(focusPane(this.state, focus));
  }
  togglePaneZoom(): void {
    this.publish(togglePaneZoom(this.state));
  }
  setChatDockFits(fits: boolean): void {
    this.publish(setChatDockFits(this.state, fits));
  }

  /** Content the fake server returns for whichever visualization is opened. */
  paneContent = 'Performance · median_tok_per_sec\n  1200 ┤●';

  openRound(roundNumber?: number): void {
    if (roundNumber === undefined) {
      this.enterExperimentDrilldown();
      return;
    }
    const scoped = enterExperimentRound(this.state, roundNumber);
    this.publish(scoped ?? enterUnownedExperimentRound(this.state, roundNumber) ?? this.state);
  }
  leaveExperimentDrilldown(): void {
    this.publish(leaveExperimentDrilldown(this.state));
  }
  leaveHypothesisDetail(): void {
    this.publish(leaveHypothesisDetail(this.state));
  }

  subscribe(listener: (state: SessionState) => void): () => void {
    this.#listeners.add(listener);
    listener(this.state);
    return () => this.#listeners.delete(listener);
  }

  #notify(): void {
    for (const listener of this.#listeners) listener(this.state);
  }
}

describe('round focus on hidden panes', () => {
  // Key routing consulted `roundFocus` while the border consulted
  // `focusedPane`, so with the agents pane off screen Left could move the keys
  // and an auto-selected agent filter onto a pane that was not there,
  // silently narrowing the transcript.
  function twoAgentRound(): SessionState {
    const base = initialSessionState();
    return {
      ...base,
      selectedRound: 1,
      core: {
        ...base.core,
        rounds: [{number: 1, status: 'active' as const}],
        phases: [
          {
            kind: 'implementer',
            status: 'completed' as const,
            roundNumber: 1,
            roundLabel: 'round-1-impl',
          },
          {kind: 'judge', status: 'active' as const, roundNumber: 1, roundLabel: 'round-1-judge'},
        ],
        transcript: [
          {
            id: 'e1',
            kind: 'assistant' as const,
            label: 'implementer',
            content: 'edited the kernel',
            agentKind: 'implementer',
            roundNumber: 1,
          },
          {
            id: 'e2',
            kind: 'assistant' as const,
            label: 'implementer',
            content: 'guarded the tail tile',
            agentKind: 'implementer',
            roundNumber: 1,
          },
          {
            id: 'e3',
            kind: 'assistant' as const,
            label: 'judge',
            content: 'checking the diff',
            agentKind: 'judge',
            roundNumber: 1,
          },
        ],
      },
    };
  }

  it('keeps Left from filtering the zoomed transcript through the hidden agents pane', async () => {
    const testRenderer = await createTestRenderer({width: 150, height: 26});
    const controller = new FakeController(twoAgentRound());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('edited the kernel'));

    testRenderer.mockInput.pressKey('F4');
    await frameAfter(testRenderer);
    expect(controller.state.layout.zoomedPane).toBe('transcript');

    // Left names the agents pane, but the zoom took it off screen: the keys
    // hold on the transcript and no invisible agent filter appears.
    testRenderer.mockInput.pressKey('ARROW_LEFT');
    const frame = await frameAfter(testRenderer);
    expect(controller.state.roundFocus).toBe('transcript');
    expect(controller.state.selectedAgentKind).toBeNull();
    expect(frame).toContain('edited the kernel');
    expect(frame).toContain('checking the diff');

    // Up still moves the transcript cursor, not an invisible agent selection.
    testRenderer.mockInput.pressKey('ARROW_UP');
    await frameAfter(testRenderer);
    expect(controller.state.selectedEntryId).not.toBeNull();
    expect(controller.state.selectedAgentKind).toBeNull();
  });

  it('still reaches the agents pane with Left while it is on screen', async () => {
    const testRenderer = await createTestRenderer({width: 150, height: 26});
    const controller = new FakeController(twoAgentRound());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('edited the kernel'));

    testRenderer.mockInput.pressKey('ARROW_LEFT');
    const frame = await frameAfter(testRenderer);
    expect(controller.state.roundFocus).toBe('agents');
    // Arriving auto-selects the active agent, with the pane there to show it.
    expect(controller.state.selectedAgentKind).toBe('judge');
    expect(frame).toContain('▸ Agents');

    // Zoomed onto the agents pane, Right has no visible transcript to move
    // to: the keys stay on the one pane that is on screen.
    testRenderer.mockInput.pressKey('F4');
    await frameAfter(testRenderer);
    expect(controller.state.layout.zoomedPane).toBe('agents');
    testRenderer.mockInput.pressKey('ARROW_RIGHT');
    await frameAfter(testRenderer);
    expect(controller.state.roundFocus).toBe('agents');

    // Unzoomed, the same key moves them again.
    testRenderer.mockInput.pressKey('F4');
    testRenderer.mockInput.pressKey('ARROW_RIGHT');
    await frameAfter(testRenderer);
    expect(controller.state.roundFocus).toBe('transcript');
  });

  it('repairs focus and filter parked on the agents pane when a zoom hides it', async () => {
    const testRenderer = await createTestRenderer({width: 150, height: 26});
    const controller = new FakeController(twoAgentRound());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('edited the kernel'));

    // The agents pane holds the keys and an agent filters the transcript.
    testRenderer.mockInput.pressKey('ARROW_LEFT');
    await frameAfter(testRenderer);
    expect(controller.state.roundFocus).toBe('agents');
    expect(controller.state.selectedAgentKind).toBe('judge');

    // A zoom leaves only the transcript on screen while the agents pane held
    // the keys. Normalization moves the keys to the visible pane and turns
    // the filter off with its cue: the transcript shows every agent again and
    // wears the focus border.
    controller.publish({
      ...controller.state,
      layout: {...controller.state.layout, zoomedPane: 'transcript'},
    });
    const frame = await frameAfter(testRenderer);
    expect(controller.state.roundFocus).toBe('transcript');
    expect(controller.state.selectedAgentKind).toBeNull();
    expect(frame).toContain('▸ Transcript');
    expect(frame).toContain('edited the kernel');
    expect(frame).toContain('checking the diff');

    // The keys followed: Up moves the transcript cursor.
    testRenderer.mockInput.pressKey('ARROW_UP');
    await frameAfter(testRenderer);
    expect(controller.state.selectedEntryId).not.toBeNull();
  });
});
