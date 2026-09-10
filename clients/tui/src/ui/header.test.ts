import {describe, expect, it} from 'bun:test';
import type {RunEvent} from '@vibesys/backend-client';
import {applyRunMapEvent, type CoreRunStatus} from '@vibesys/core-state';
import {
  initialSessionState,
  runStatusLabel,
  type SessionState,
  selectAgent,
} from '../session-model.js';
import {
  type HeaderSpan,
  type HeaderSpanRole,
  headerBackground,
  headerSpanStyle,
  MAX_HEADER_SPANS,
  MIN_WIDTH,
  renderHeader,
  runStateText,
  usageText,
} from './header.js';
import {displayWidth} from './text-width.js';
import {contrastRatio, listThemes, resolveTheme, type Theme} from './theme.js';

const WIDE = 200;
const NARROW = 100;

/**
 * The spans read back as the line they draw. `renderHeader` returns spans so
 * each role can carry a colour of its own; what the operator sees is their
 * concatenation, and every width and dropping assertion below is about that.
 */
function line(state: SessionState, showLog: boolean, width: number): string {
  return renderHeader(state, showLog, width)
    .map(span => span.text)
    .join('');
}

/** The text drawn for one role, or null when the header dropped it. */
function spanText(spans: HeaderSpan[], role: HeaderSpanRole): string | null {
  return spans.find(span => span.role === role)?.text ?? null;
}

function stateWith(
  overrides: Partial<SessionState> = {},
  core: Partial<SessionState['core']> = {},
) {
  const base = initialSessionState();
  return {...base, ...overrides, core: {...base.core, ...core}};
}

/** The live example from #517, as the backend actually reports it. */
function runningImplementer(): SessionState {
  return stateWith(
    {
      hypothesisScope: {
        id: 'H-01',
        label: 'Preallocated lock-free SPSC ring · r1',
        title: 'Preallocated lock-free SPSC ring',
        rounds: [1],
      },
    },
    {
      status: 'running',
      agentKind: 'implementer',
      roundLabel: 'round-1-retry-2-implementer',
      usage: {inputTokens: 223_000, contextWindow: 400_000, model: 'claude-opus-5'},
    },
  );
}

/** The same run, with a claim of a chosen length. */
function withTitle(title: string): SessionState {
  const base = runningImplementer();
  return {
    ...base,
    hypothesisScope: {id: 'H-01', label: `${title} · r1`, title, rounds: [1]},
  };
}

/**
 * The plain loop starting its measurement phase, as `loop.py` labels it.
 *
 * Fed to the run map rather than asserted on: what matters is the phase set the
 * projection seeds from it, which is where `perf_eval` becomes selectable.
 */
function perfEvalStart(): RunEvent {
  return {
    sequence: 1,
    timestamp: '2026-01-01T00:00:01Z',
    type: 'agent_execution_started',
    execution_id: 'exec-1',
    invocation_id: 'exec-1',
    agent_kind: 'perf_eval',
    round_label: 'perf_eval iter 4',
  };
}

/** How many times `needle` appears in `text`, for grapheme-integrity checks. */
function occurrences(text: string, needle: string): number {
  return text.split(needle).length - 1;
}

describe('header', () => {
  it('replaces the raw label line from the issue', () => {
    const header = line(runningImplementer(), false, WIDE);
    expect(header).toBe(
      'VibeSys · running · implementing · attempt 2 · Preallocated lock-free SPSC ring · 223k/400k context',
    );
  });

  it('never renders a backend round label, in any loop mode', () => {
    const labels = [
      'round-1-plan',
      'round-1-pre',
      'round-1-retry-1-implementer',
      'round-2-retry-3-judge',
      'gen-2-cand-1-mutator',
      'impl issue #7 att2',
      'perf_eval iter 4',
    ];
    for (const roundLabel of labels) {
      const header = line(stateWith({}, {roundLabel, status: 'running'}), false, WIDE);
      expect(header).not.toContain(roundLabel);
      expect(header).not.toMatch(/round-\d|gen-\d|cand-\d|retry-\d|att\d/);
    }
  });

  it('fits 100 columns whole, where the old header clipped the title', () => {
    const header = line(runningImplementer(), false, NARROW);
    expect(header.length).toBeLessThanOrEqual(NARROW);
    expect(header).toContain('Preallocated lock-free SPSC ring');
    expect(header).not.toEndWith('…');
  });

  it('gives up the token meter before the hypothesis title', () => {
    // 80 columns cannot hold both; the claim is the one worth keeping.
    const header = line(runningImplementer(), false, 80);
    expect(header.length).toBeLessThanOrEqual(80);
    expect(header).toContain('Preallocated lock-free SPSC ring');
    expect(header).not.toContain('context');
    expect(header).not.toEndWith('…');
  });

  it('drops the dialog hint before the selected agent, and both before the title', () => {
    const state = {
      ...runningImplementer(),
      selectedAgentKind: 'implementer',
      overlay: {kind: 'detail' as const, content: 'x'},
    };
    expect(line(state, false, WIDE)).toContain('Esc: close dialog');
    const squeezed = line(state, false, NARROW);
    expect(squeezed).not.toContain('Esc: close dialog');
    expect(squeezed).not.toContain('filtered to');
    expect(squeezed).toContain('Preallocated lock-free SPSC ring');
  });

  it('shortens a long title only once nothing cheaper is left to drop', () => {
    // The real title from the recorded run: too long for 60 columns even with
    // every weaker segment already gone, so it shortens rather than vanishing.
    const state = withTitle(
      'Preallocated lock-free SPSC ring replaces the Mutex<VecDeque> hot path',
    );
    const header = line(state, false, 60);
    expect(header.length).toBeLessThanOrEqual(60);
    expect(header).not.toContain('context');
    expect(header).toContain('Preallocated');
    expect(header).toEndWith('…');
  });

  it('stops shortening the title at the point it stops identifying anything', () => {
    const state = withTitle(
      'Preallocated lock-free SPSC ring replaces the Mutex<VecDeque> hot path',
    );
    // 34 columns leaves the title exactly its floor of twelve cells.
    expect(line(state, false, 34)).toBe('VibeSys · running · Preallocated…');
    // 22 leaves it less than that, so it goes entirely rather than becoming
    // `Prealloc…`, and the run stays legible without it.
    expect(line(state, false, 22)).toBe('VibeSys · running');
  });

  it('keeps the brand and the run state whole down to the minimum width', () => {
    for (const width of [200, 100, 60, 34, MIN_WIDTH]) {
      const header = line(runningImplementer(), false, width);
      expect(header.startsWith('VibeSys · running')).toBe(true);
      expect(displayWidth(header)).toBeLessThanOrEqual(width);
    }
  });

  it('holds the minimum width for every run state it can name', () => {
    // MIN_WIDTH is sized for the widest of these, so none of them is cut.
    const states: CoreRunStatus[] = [
      'running',
      'pausing',
      'paused',
      'completed',
      'failed',
      'connecting',
    ];
    for (const status of states) {
      const header = line(stateWith({}, {status}), false, MIN_WIDTH);
      expect(header).toBe(`VibeSys · ${runStatusLabel(status)}`);
    }
    const dropped = stateWith({eventStreamAvailable: false}, {status: 'running'});
    expect(line(dropped, false, MIN_WIDTH)).toBe('VibeSys · disconnected');
  });

  it('names a selected phase in words, for every kind the plain loop seeds', () => {
    // The kinds come from the projection rather than from a literal here. The
    // plain loop seeds `perf_eval` alongside `implementer` and `judge`, and
    // `selectAgent` stores the phase kind verbatim, so an operator selecting
    // the measurement phase put `selected perf_eval` on the curated header:
    // the backend identifier this header exists to remove.
    const seeded = applyRunMapEvent(
      {outerLoop: 'plain', expectedRoles: null, rounds: [], phases: [], lastEventTimestamp: null},
      perfEvalStart(),
    );
    const kinds = seeded.phases.map(phase => phase.kind);
    expect(kinds).toContain('perf_eval');

    for (const kind of kinds) {
      const header = line(selectAgent(runningImplementer(), kind), false, WIDE);
      expect({kind, leaks: header.includes(kind)}).toEqual({kind, leaks: false});
      expect({kind, named: header.includes('filtered to ')}).toEqual({kind, named: true});
    }
    // The same table the phase segment reads, so the two cannot drift apart.
    expect(line(selectAgent(runningImplementer(), 'perf_eval'), false, WIDE)).toContain(
      'filtered to measuring',
    );
  });

  it('says nothing about a selected kind it has no word for', () => {
    // A kind added to the backend after this was written. There is no phrase
    // to fall back to that is not the identifier itself, and this note is the
    // second cheapest segment on the line, so it goes.
    const header = line(selectAgent(runningImplementer(), 'verifier'), false, WIDE);
    expect(header).not.toContain('verifier');
    expect(header).not.toContain('filtered to');
    expect(header).toContain('Preallocated lock-free SPSC ring');
  });

  it('cuts the line below the minimum width instead of dropping the state', () => {
    // Narrower than MIN_WIDTH the brand and the state do not fit together, so
    // the guarantee stops there and the line is cut and marked. Dropping the
    // state instead would leave a header that says only `VibeSys`.
    const header = line(runningImplementer(), false, MIN_WIDTH - 6);
    expect(displayWidth(header)).toBeLessThanOrEqual(MIN_WIDTH - 6);
    expect(header).toBe('VibeSys · runni…');
    expect(line(runningImplementer(), false, 0)).toBe('');
  });
});

describe('width in terminal cells', () => {
  it('budgets a CJK title in cells rather than code units', () => {
    // 30 ideographs are 30 code units and 60 terminal cells. Budgeted by
    // `String.length` the whole title reads as fitting 60 columns with room to
    // spare, and lays out over 80.
    const title = '\u74b0'.repeat(30);
    const header = line(withTitle(title), false, 60);
    expect(displayWidth(header)).toBeLessThanOrEqual(60);
    expect(header.length).toBeLessThan(60);
    expect(header).toEndWith('\u2026');
  });

  it('never cuts an emoji in half', () => {
    const state = withTitle(`${'\u{1f680}'.repeat(20)} to orbit`);
    for (const width of [30, 44, 45, 60]) {
      const header = line(state, false, width);
      expect(displayWidth(header)).toBeLessThanOrEqual(width);
      // With the `u` flag this matches only an unpaired surrogate, which is
      // what a code-unit slice through a rocket leaves behind.
      expect(header).not.toMatch(/[\ud800-\udfff]/u);
    }
  });

  it('keeps a joined emoji sequence whole', () => {
    // One grapheme, eight code units. A cut inside it leaves separate people
    // or a dangling joiner.
    const family = '\u{1f468}\u200d\u{1f469}\u200d\u{1f467}';
    const state = withTitle(`${family.repeat(12)} crew`);
    for (const width of [30, 40, 55]) {
      const header = line(state, false, width);
      expect(displayWidth(header)).toBeLessThanOrEqual(width);
      expect(header).not.toMatch(/[\ud800-\udfff]/u);
      expect(header).not.toContain('\u200d\u2026');
      // Every family carries one of each figure, so unequal counts mean a cut
      // landed inside a cluster.
      expect(occurrences(header, '\u{1f468}')).toBe(occurrences(header, '\u{1f467}'));
    }
  });

  it('drops a wide grapheme that would straddle the budget', () => {
    // 44 columns leaves the title 23 cells, which is 11 rockets and a half.
    // The half goes, so the line comes out a cell under rather than a cell
    // over.
    const header = line(withTitle('\u{1f680}'.repeat(20)), false, 44);
    expect(displayWidth(header)).toBe(43);
  });
});

describe('token meter', () => {
  it('shows a ratio only when the count is inside the window', () => {
    const state = stateWith(
      {},
      {usage: {inputTokens: 118_000, contextWindow: 400_000, model: null}},
    );
    expect(usageText(state)).toBe('118k/400k context');
  });

  it('drops the denominator rather than reporting over 100 percent', () => {
    // The issue's observation: 472k/400k rendered as 118 percent and read as
    // an overflow error.
    const state = stateWith(
      {},
      {usage: {inputTokens: 472_000, contextWindow: 400_000, model: null}},
    );
    expect(usageText(state)).toBe('472k tokens');
    expect(usageText(state)).not.toContain('/');
  });

  it('shows a bare count when no window is known', () => {
    const state = stateWith({}, {usage: {inputTokens: 5_400, contextWindow: null, model: null}});
    expect(usageText(state)).toBe('5k tokens');
  });

  it('says nothing before any usage is reported', () => {
    expect(usageText(initialSessionState())).toBeNull();
    const zero = stateWith({}, {usage: {inputTokens: 0, contextWindow: 400_000, model: null}});
    expect(usageText(zero)).toBeNull();
  });
});

describe('run state', () => {
  it('reports the backend status by default', () => {
    expect(runStateText(stateWith({}, {status: 'running'}))).toBe('running');
    expect(runStateText(stateWith({}, {status: 'completed'}))).toBe('completed');
  });

  it('spells out a pause the backend has requested but not yet applied', () => {
    // `/pause` lands at the next invocation boundary, so the call already in
    // flight keeps running. The backend publishes that wait as `pausing`, and
    // the header says so rather than claiming the run has stopped.
    const pausing = stateWith({}, {status: 'pausing'});
    expect(runStateText(pausing)).toBe('pausing…');
    const paused = stateWith({}, {status: 'paused'});
    expect(runStateText(paused)).toBe('paused');
    expect(line(pausing, false, WIDE)).toContain('pausing…');
  });

  it('shows a lost event stream while the run is still going', () => {
    const dropped = stateWith({eventStreamAvailable: false}, {status: 'running'});
    expect(runStateText(dropped)).toBe('disconnected');
    // A finished run whose socket closed is not a disconnected run.
    const finished = stateWith({eventStreamAvailable: false}, {status: 'completed'});
    expect(runStateText(finished)).toBe('completed');
  });
});

/**
 * The floor every token but `textSubtle` is held to, restated here rather than
 * read off `theme.minContrast` so this is an independent check.
 *
 * It is measured against `headerBackground` rather than `theme.canvas` because
 * that function is what decides which cell the text lands on, and a tone that
 * clears the floor against a background nothing draws is not a readable
 * header. Since #574 the two are the same colour, which the test below pins.
 */
function minContrast(theme: Theme): number {
  return theme.name.startsWith('high-contrast') ? 7 : 4.5;
}

/** The lower floor `theme.ts` holds `textSubtle` to. Only separators use it. */
const SUBTLE_FLOOR = 3;

/** Every role a span can carry, which is every role that needs a tone. */
const SPAN_ROLES: readonly HeaderSpanRole[] = [
  'brand',
  'state',
  'phase',
  'title',
  'usage',
  'selection',
  'hint',
  'separator',
];

/**
 * Every run state `runStateText` can produce, plus one it cannot.
 *
 * The statuses go through `runStatusLabel`, which is what puts them on the
 * line, so this covers the labels rather than the raw status words. There is no
 * `interrupted`: `RunStatus` has no such member, and `run_interrupted` ends the
 * run as `failed`.
 */
const RUN_STATES = [
  ...(
    ['starting', 'running', 'pausing', 'paused', 'completed', 'failed', 'connecting'] as const
  ).map(runStatusLabel),
  'disconnected',
  // A status the backend adds after this was written, which has no verdict here.
  'reticulating',
];

describe('header hierarchy', () => {
  it('draws every segment and separator as a span of its own', () => {
    const spans = renderHeader(runningImplementer(), false, WIDE);
    expect(spans.map(span => span.role)).toEqual([
      'brand',
      'separator',
      'state',
      'separator',
      'phase',
      'separator',
      'title',
      'separator',
      'usage',
    ]);
    expect(spanText(spans, 'title')).toBe('Preallocated lock-free SPSC ring');
    expect(spanText(spans, 'usage')).toBe('223k/400k context');
    expect(spanText(spans, 'hint')).toBeNull();
  });

  it('never returns more spans than a caller sized its row for', () => {
    // Every role at once: phase, title, token meter, selected agent and hint.
    const state = {
      ...runningImplementer(),
      selectedAgentKind: 'implementer',
      overlay: {kind: 'detail' as const, content: 'x'},
    };
    const spans = renderHeader(state, false, WIDE);
    // Tight, not just an upper bound: a row allocated at this size is fully
    // used by the widest header, so the constant is neither short nor padding.
    expect(spans).toHaveLength(MAX_HEADER_SPANS);
    for (const width of [WIDE, NARROW, 80, 60, 34, MIN_WIDTH, 16, 1, 0]) {
      expect(renderHeader(state, false, width).length).toBeLessThanOrEqual(MAX_HEADER_SPANS);
    }
  });

  it('gives the brand the accent, the content body text, and metadata a grey', () => {
    const theme = resolveTheme('dark');
    const tone = (role: HeaderSpanRole): {fg: string; bold: boolean} =>
      headerSpanStyle(theme, {text: 'x', role});
    expect(tone('brand')).toEqual({fg: theme.accent, bold: true});
    expect(tone('phase')).toEqual({fg: theme.textPrimary, bold: false});
    expect(tone('title')).toEqual({fg: theme.textPrimary, bold: false});
    expect(tone('usage')).toEqual({fg: theme.textMuted, bold: false});
    expect(tone('selection')).toEqual({fg: theme.textMuted, bold: false});
    expect(tone('hint')).toEqual({fg: theme.textMuted, bold: false});
    expect(tone('separator')).toEqual({fg: theme.textSubtle, bold: false});
  });

  it('bolds only the two segments no width can drop', () => {
    const theme = resolveTheme('dark');
    const bold = SPAN_ROLES.filter(role => headerSpanStyle(theme, {text: 'x', role}).bold);
    expect(bold).toEqual(['brand', 'state']);
  });

  it('colours the run state by the verdict the word carries', () => {
    const theme = resolveTheme('dark');
    const tone = (text: string): string => headerSpanStyle(theme, {text, role: 'state'}).fg;
    expect(tone('completed')).toBe(theme.success);
    expect(tone('failed')).toBe(theme.error);
    expect(tone('paused')).toBe(theme.warning);
    // The label, not the status: `pausing` is on the line as `pausing…`.
    expect(tone(runStatusLabel('pausing'))).toBe(theme.warning);
    expect(tone('pausing')).toBe(theme.textPrimary);
    expect(tone('disconnected')).toBe(theme.warning);
    // Not verdicts. Bold is what sets these apart, and the word is spelled out.
    expect(tone('running')).toBe(theme.textPrimary);
    expect(tone('starting')).toBe(theme.textPrimary);
    expect(tone('connecting')).toBe(theme.textPrimary);
    // A status the backend adds later reads as body text rather than as a
    // verdict this file guessed at.
    expect(tone('reticulating')).toBe(theme.textPrimary);
  });

  it('tracks the state the header actually rendered, not the backend status', () => {
    const theme = resolveTheme('dark');
    const stateTone = (state: SessionState): string => {
      const spans = renderHeader(state, false, WIDE);
      const span = spans.find(candidate => candidate.role === 'state');
      if (span === undefined) throw new Error('the run state is never dropped');
      return headerSpanStyle(theme, span).fg;
    };
    // A pause the backend has not applied yet, which renders as `pausing…`
    // rather than as the status word the tone table would otherwise be read
    // against.
    expect(stateTone(stateWith({}, {status: 'pausing'}))).toBe(theme.warning);
    expect(stateTone(stateWith({}, {status: 'paused'}))).toBe(theme.warning);
    // A lost stream, over a status that still says the run is going.
    expect(stateTone(stateWith({eventStreamAvailable: false}, {status: 'running'}))).toBe(
      theme.warning,
    );
    // An ended run, whose status is not restated as a lost stream.
    expect(stateTone(stateWith({eventStreamAvailable: false}, {status: 'failed'}))).toBe(
      theme.error,
    );
    expect(stateTone(stateWith({eventStreamAvailable: false}, {status: 'completed'}))).toBe(
      theme.success,
    );
  });

  it('keeps each role on its own span through a shortened title and a cut', () => {
    const state = withTitle(
      'Preallocated lock-free SPSC ring replaces the Mutex<VecDeque> hot path',
    );
    // The title shortens rather than losing its role to the ellipsis.
    const shortened = renderHeader(state, false, 60);
    expect(spanText(shortened, 'title')).toEndWith('…');
    // Below MIN_WIDTH the line is cut, and the cut span keeps its role, so the
    // half-drawn run state is still coloured as the run state.
    const cut = renderHeader(runningImplementer(), false, MIN_WIDTH - 6);
    expect(cut).toEqual([
      {text: 'VibeSys', role: 'brand'},
      {text: ' · ', role: 'separator'},
      {text: 'runni…', role: 'state'},
    ]);
    // One cell holds nothing but the mark that the rest was cut.
    expect(renderHeader(runningImplementer(), false, 1)).toEqual([{text: '…', role: 'brand'}]);
    expect(renderHeader(runningImplementer(), false, 0)).toEqual([]);
  });
});

describe('header contrast', () => {
  it('clears the floor of every theme for every role, in all eight', () => {
    const themes = listThemes();
    expect(themes).toHaveLength(8);
    for (const theme of themes) {
      for (const role of SPAN_ROLES) {
        // Only the state's tone depends on its text; the rest ignore it.
        const texts = role === 'state' ? RUN_STATES : ['x'];
        for (const text of texts) {
          const {fg} = headerSpanStyle(theme, {text, role});
          const floor = role === 'separator' ? SUBTLE_FLOOR : minContrast(theme);
          expect({
            theme: theme.name,
            role,
            text,
            ratio: contrastRatio(fg, headerBackground(theme)) >= floor,
          }).toEqual({theme: theme.name, role, text, ratio: true});
        }
      }
    }
  });

  it('paints the canvas, so the top line wears no colour of its own', () => {
    // #574: the header filled `elevatedSurface` while the frame behind it
    // filled `canvas`, so the top line agreed with the panes and disagreed with
    // the strip of frame showing around them. The collapse kept the header's
    // own value and moved `canvas` onto it; this pins that the two names now
    // mean one colour. The token is gone, so no theme can reintroduce a second
    // shade, but `headerBackground` is still the single place that chooses the
    // fill, so the choice is pinned rather than left to the next reader.
    for (const theme of listThemes()) {
      expect({theme: theme.name, background: headerBackground(theme)}).toEqual({
        theme: theme.name,
        background: theme.canvas,
      });
    }
  });

  it('draws metadata below content and separators below both, in every theme', () => {
    // What "greyed out" has to mean to hold in eight palettes: an ordering
    // against the surface the header paints, not a particular grey.
    for (const theme of listThemes()) {
      const against = (role: HeaderSpanRole): number =>
        contrastRatio(headerSpanStyle(theme, {text: 'x', role}).fg, headerBackground(theme));
      expect({theme: theme.name, dimmer: against('usage') < against('title')}).toEqual({
        theme: theme.name,
        dimmer: true,
      });
      expect({theme: theme.name, dimmer: against('separator') < against('usage')}).toEqual({
        theme: theme.name,
        dimmer: true,
      });
    }
  });
});
