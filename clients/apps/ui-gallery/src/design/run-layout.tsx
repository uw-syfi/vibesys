/**
 * Design prototype for the Run screen layout (desktop redesign, run-layout). Not a product screen:
 * it composes `@vibesys/ui` components over fixed fake data so the layout can be judged with the
 * real components. Open with `?design=run-layout` plus the zoom in the query (see `zoomFromQuery`).
 *
 * One structure: the run, its candidates (a lineage tree), a candidate's steps (agent turns and
 * gates), a step's items (tool calls, log lines). The frame shows the list you chose from and what
 * you chose; the detail pane's mode control switches what you see of the selected thing.
 */
import {
  AppFrame,
  Breadcrumb,
  Button,
  Chart,
  Chip,
  type Crumb,
  DotLegend,
  Gantt,
  type GanttRow,
  type GanttSegment,
  type GanttView,
  type Guide,
  KeyValue,
  LogPane,
  Menu,
  NewBelow,
  Pane,
  Panes,
  PickList,
  PickRow,
  Row,
  type RowBar,
  Screen,
  Scrubber,
  SegmentedControl,
  SidebarItem,
  SidebarSection,
  Stack,
  Text,
  ToolCall,
} from '@vibesys/ui';
import {type JSX, useEffect, useState} from 'react';
import {BUILDER_LOWER} from '../role-words.js';
import {
  AGENTS,
  type Agent,
  type Attempt,
  BEST,
  CANDIDATES,
  type Candidate,
  type Entry,
  gateLog,
  INPUT,
  NARRATION,
  NARRATIVE,
  OUTCOME_DOT,
  POINTS,
  REPLY_FACTS,
  ratio,
  STEPS_LINE,
  type Step,
  TURN_ENTRIES,
} from './run-data.js';
import {type Turn, TurnNarrative} from './turn-narrative.js';

const TURN: Turn = {beats: NARRATIVE, entries: TURN_ENTRIES};

type Mode = 'main' | 'timeline' | 'agents' | 'logs';

interface Zoom {
  readonly c: string | null;
  readonly step: string | null;
  readonly item: string | null;
  readonly mode: Mode;
  /** The agent opened from the Agents face (its turns as narrative). */
  readonly agent: string | null;
}

export interface DesignFlags {
  readonly ended: boolean;
  readonly narrow: boolean;
  readonly hover: string | null;
  /** A tool group opened in place (`open=g2`), for the expanded-turn screenshot. */
  readonly open: string | null;
}

function modeOf(value: string | null): Mode {
  return value === 'logs' || value === 'timeline' || value === 'agents' ? value : 'main';
}

/** `c` (candidate), `step`, `item`, `mode`, `agent`; `ended` and `narrow` are flags. */
export function zoomFromQuery(search: string): {zoom: Zoom; flags: DesignFlags} {
  const query = new URLSearchParams(search);
  return {
    zoom: {
      c: query.get('c'),
      step: query.get('step'),
      item: query.get('item'),
      mode: modeOf(query.get('mode')),
      agent: query.get('agent'),
    },
    flags: {
      ended: query.has('ended'),
      narrow: query.has('narrow'),
      hover: query.get('hover'),
      open: query.get('open'),
    },
  };
}

const TOP: Zoom = {c: null, step: null, item: null, mode: 'main', agent: null};

function up(zoom: Zoom): Zoom {
  if (zoom.agent !== null) return {...zoom, agent: null};
  if (zoom.item !== null) return {...zoom, item: null, mode: 'main'};
  if (zoom.step !== null) return {...zoom, step: null, mode: 'main'};
  if (zoom.c !== null) return {...zoom, c: null, mode: 'main'};
  return {...zoom, mode: 'main'};
}

const noop = (): void => undefined;

function candidate(id: string | null): Candidate | null {
  return CANDIDATES.find(c => c.id === id) ?? null;
}

/** The ideas (candidates), newest first: a flat list, no tree. */
function ideas(): readonly Candidate[] {
  return [...CANDIDATES].reverse().sort((a, b) => b.startedAt - a.startedAt);
}

/** The passed candidate with the best value: the end of the winning path. */
function best(): Candidate {
  const passed = CANDIDATES.filter(c => c.outcome === 'passed' && c.value !== null);
  return passed.reduce((top, c) => ((c.value ?? 0) > (top.value ?? 0) ? c : top));
}

/** The agents as a tree (depth first, parents before children), with their guides. */
function agentTree(): {readonly a: Agent; readonly guides: Guide[]}[] {
  const out: {a: Agent; guides: Guide[]}[] = [];
  const walk = (parent: string | null, prefix: Guide[]): void => {
    const children = AGENTS.filter(a => a.parent === parent);
    children.forEach((child, index) => {
      const last = index === children.length - 1;
      out.push({a: child, guides: parent === null ? [] : [...prefix, last ? 'elbow' : 'tee']});
      walk(child.id, parent === null ? [] : [...prefix, last ? 'blank' : 'pipe']);
    });
  };
  walk(null, []);
  return out;
}

function agentName(a: Agent): string {
  return `${a.role} · ${a.subject}`;
}

function figureOf(c: Candidate): {figure: string; tone: 'ok' | 'err' | 'muted' | 'default'} {
  if (c.outcome === 'running') return {figure: c.took, tone: 'muted'};
  if (c.value === null) return {figure: c.verdict ?? '–', tone: 'err'};
  if (c.outcome === 'failed') return {figure: ratio(c.value), tone: 'err'};
  return {figure: ratio(c.value), tone: c.id === 'input' ? 'muted' : 'ok'};
}

function settled(c: Candidate, ended: boolean): Candidate {
  if (!ended || c.outcome !== 'running') return c;
  const value = c.id === 'spin-backoff' ? 12_900_000 : 11_480_000;
  return {...c, outcome: 'failed', value, verdict: null, fresh: false};
}

/** What a candidate's dot means, in the words its tooltip shows. */
function statusOf(c: Candidate): {tone: 'ok' | 'err' | 'live'; label: string} {
  if (c.outcome === 'running') return {tone: 'live', label: 'running'};
  if (c.outcome === 'passed') return {tone: 'ok', label: c.id === 'input' ? 'input' : 'passed'};
  return {
    tone: 'err',
    label: c.value === null ? 'failed · not measured' : 'failed · turned down',
  };
}

/** The ids from `id` back to the input, `id` included. */
function pathOf(id: string | null): Set<string> {
  const out = new Set<string>();
  let at = candidate(id);
  while (at !== null) {
    out.add(at.id);
    at = candidate(at.parent);
  }
  return out;
}

function legend(ended: boolean): {tone: 'ok' | 'err' | 'live'; label: string}[] {
  return ended
    ? [
        {tone: 'ok', label: '2 passed'},
        {tone: 'err', label: '4 failed'},
      ]
    : [
        {tone: 'ok', label: '2 passed'},
        {tone: 'err', label: '2 failed'},
        {tone: 'live', label: '2 running'},
      ];
}

const KICKER = 'ideas';

/** Minutes in a duration like `1m 40s`, `32s` or `6m`. */
function minutes(took: string): number {
  const m = /(\d+)m/.exec(took);
  const sec = /(\d+)s/.exec(took);
  return (m === null ? 0 : Number(m[1])) + (sec === null ? 0 : Number(sec[1]) / 60);
}

function now(flags: DesignFlags): number {
  return flags.ended ? 22 : 14;
}

function axisTicks(flags: DesignFlags): {at: number; label: string}[] {
  return [0, 5, 10, 15, 20].filter(m => m <= now(flags)).map(m => ({at: m, label: `${m}m`}));
}

function stepKind(step: Step): GanttSegment['kind'] {
  if (step.kind === 'gate') return 'hatched';
  return step.role === 'judge' ? 'grey' : 'solid';
}

/** A candidate's bar: its lifetime on the run's clock, split by its steps in proportion to their durations. */
function ganttRow(
  c: Candidate,
  flags: DesignFlags,
  guides: readonly Guide[],
  selected: boolean,
  open: () => void,
  openStep: (step: string) => void,
): GanttRow {
  const start = c.startedAt;
  const end = c.outcome === 'running' ? now(flags) : start + minutes(c.took);
  const total = c.steps.reduce((sum, step) => sum + minutes(step.took), 0);
  let at = start;
  const segments = c.steps.map(step => {
    const length = ((end - start) * minutes(step.took)) / total;
    const segment: GanttSegment = {
      id: step.id,
      start: at,
      end: at + length,
      kind: stepKind(step),
      label: `${step.role} · ${step.headline} · ${step.took}`,
      press: () => openStep(step.id),
    };
    at += length;
    return segment;
  });
  const fig = figureOf(c);
  return {
    id: c.id,
    label: c.id,
    guides,
    start,
    end,
    outcome:
      c.outcome === 'running'
        ? 'live'
        : c.outcome === 'passed'
          ? c.id === 'input'
            ? 'muted'
            : 'ok'
          : 'err',
    figure: c.outcome === 'running' ? '' : fig.figure,
    segments,
    selected,
    fresh: c.fresh === true,
    press: open,
  };
}

function Sidebar(): JSX.Element {
  return (
    <SidebarSection title="Live">
      <SidebarItem
        label="queue-rs · mpmc"
        detail="host-b"
        status={{tone: 'live', label: 'Running'}}
        active
        press={noop}
      />
      <SidebarItem
        label="kv-cache · decode"
        detail="host-b"
        status={{tone: 'ok', label: 'Finished'}}
        press={noop}
      />
      <SidebarItem
        label="queue-rs · mpmc"
        detail="host-b"
        status={{tone: 'neutral', label: 'Stopped'}}
        press={noop}
      />
    </SidebarSection>
  );
}

function Header(props: {readonly flags: DesignFlags; readonly compactHero: boolean}): JSX.Element {
  const {ended} = props.flags;
  return (
    <Stack gap="space-3">
      <Row justify="between" align="center">
        <Row gap="space-4" wrap>
          <Text as="h1" variant="heading">
            queue-rs · mpmc
          </Text>
          <Row gap="space-3" wrap>
            <Chip label={ended ? 'Finished' : 'Running'} tone={ended ? 'ok' : 'live'} dot />
            <Text variant="small" tone="muted">
              host-b · model-a · {ended ? '22m' : '14m / 1h'}
            </Text>
          </Row>
        </Row>
        <Row gap="space-5">
          {props.compactHero ? (
            <Row gap="space-3" align="baseline">
              <Text variant="metric" tone="ok">
                {ratio(BEST)}
              </Text>
              <Text variant="small" tone="muted">
                13.3M ops/s
              </Text>
            </Row>
          ) : null}
          <Row gap="space-3">
            {ended ? (
              <Button size="sm" variant="secondary" text="Run again" press={noop} />
            ) : (
              <>
                <Button size="sm" variant="secondary" text="Pause" press={noop} />
                <Button size="sm" variant="danger" text="Stop" press={noop} />
              </>
            )}
            <Menu
              icon="more"
              label="More actions"
              variant="quiet"
              align="end"
              items={[{id: 'objective', label: 'Objective', press: noop}]}
            />
          </Row>
        </Row>
      </Row>
      <Scrubber
        label="Run time"
        value={ended ? 22 : 14}
        last={ended ? 22 : 14}
        live={!ended}
        pastLabel="Replay"
        {...(ended ? {latestLabel: 'End'} : {})}
        position={ended ? '22m' : '14m'}
        ticks={axisTicks(props.flags)}
        change={noop}
        backToLive={noop}
      />
    </Stack>
  );
}

function Hero(props: {
  readonly flags: DesignFlags;
  readonly pick: (id: string) => void;
}): JSX.Element {
  return (
    <Row gap="space-6" align="center">
      <Stack gap="space-1">
        <Text variant="section">total_ops_per_sec ↑</Text>
        <Row gap="space-3" align="baseline">
          <Text variant="hero" tone="ok">
            {ratio(BEST)}
          </Text>
          <Stack gap="space-0">
            <Text variant="code">13,318,503 ops/s</Text>
            <Text variant="code" tone="muted">
              input {INPUT.toLocaleString('en-US')}
            </Text>
          </Stack>
        </Row>
      </Stack>
      <Stack gap="space-0" grow>
        <Chart
          label="Best total_ops_per_sec against minutes into the run; each measured candidate is a point"
          size="strip"
          points={POINTS}
          pick={props.pick}
          steps={STEPS_LINE}
          reference={{y: INPUT, label: 'input'}}
          playhead={now(props.flags)}
          yTicks={[5_000_000, 10_000_000].map(value => ({value, label: `${value / 1_000_000}M`}))}
          xTicks={axisTicks(props.flags).map(t => ({value: t.at, label: t.label}))}
        />
      </Stack>
    </Row>
  );
}

function Timeline(props: {
  readonly flags: DesignFlags;
  readonly go: (zoom: Zoom) => void;
}): JSX.Element {
  const [view, setView] = useState<GanttView>({from: 0, to: now(props.flags)});
  return (
    <Gantt
      label="Candidates over time"
      size="fill"
      view={view}
      changeView={setView}
      nameLabel="idea"
      figureLabel="vs input"
      from={0}
      to={now(props.flags)}
      playhead={now(props.flags)}
      ticks={axisTicks(props.flags)}
      rows={ideas().map(c =>
        ganttRow(
          settled(c, props.flags.ended),
          props.flags,
          [],
          false,
          () => props.go({...TOP, c: c.id}),
          step => props.go({...TOP, c: c.id, step}),
        ),
      )}
    />
  );
}

/** One line naming the winning path, each name a link: `input → mpmc-ring-v1 → mpmc-ring-padding 5.72×`. */
function WinningPath(props: {readonly open: (id: string) => void}): JSX.Element {
  const top = best();
  const chain = [...pathOf(top.id)].reverse();
  return (
    <Row gap="space-2" align="center">
      <Text variant="small" tone="muted">
        best path
      </Text>
      <Breadcrumb
        label="Best path"
        items={chain.map(id => ({
          id,
          label: id === top.id && top.value !== null ? `${id} ${ratio(top.value)}` : id,
          press: () => props.open(id),
        }))}
      />
    </Row>
  );
}

function barOf(c: Candidate): RowBar | undefined {
  if (c.value === null) return undefined;
  const tone = c.outcome === 'passed' && c.id !== 'input' ? 'ok' : 'muted';
  return {value: c.value / BEST, tone, label: `${c.value.toLocaleString('en-US')} ops/s`};
}

function badgesOf(c: Candidate): {label: string; tone: 'ok' | 'neutral'}[] {
  const attempts = c.attempts?.length ?? 1;
  return [
    ...(c.id === best().id ? [{label: 'best', tone: 'ok' as const}] : []),
    ...(attempts > 1 ? [{label: `×${attempts}`, tone: 'neutral' as const}] : []),
  ];
}

function CandidateRow(props: {
  readonly c: Candidate;
  readonly compact: boolean;
  readonly stripe: boolean;
  readonly open: (id: string) => void;
}): JSX.Element {
  const {c} = props;
  const fig = figureOf(c);
  const bar = props.compact ? undefined : barOf(c);
  const parent = c.parent;
  return (
    <PickRow
      id={c.id}
      title={c.id}
      mono
      {...(props.compact ? {} : {summary: c.hypothesis})}
      state={statusOf(c)}
      badges={badgesOf(c)}
      {...(bar === undefined ? {} : {bar})}
      figure={fig.figure}
      figureTone={fig.tone}
      {...(props.compact
        ? {}
        : {
            trail:
              parent === null
                ? {label: ''}
                : {label: `from ${parent}`, press: () => props.open(parent)},
          })}
      stripe={props.stripe}
      fresh={c.fresh === true}
      select={() => props.open(c.id)}
    />
  );
}

function CandidateList(props: {
  readonly selected: string | null;
  readonly compact: boolean;
  readonly ended: boolean;
  readonly open: (id: string) => void;
}): JSX.Element {
  const winning = pathOf(best().id);
  return (
    <PickList
      label="Ideas"
      size="fill"
      {...(props.compact
        ? {}
        : {
            header: {
              start: 'idea',
              end: [
                {label: 'vs best', bar: true},
                {label: 'vs input', fixed: 'figure'},
                {label: 'from', fixed: 'trail'},
              ],
            },
          })}
      {...(props.selected === null ? {} : {selected: props.selected})}
    >
      {ideas().map(raw => (
        <CandidateRow
          key={raw.id}
          c={settled(raw, props.ended)}
          compact={props.compact}
          stripe={winning.has(raw.id)}
          open={props.open}
        />
      ))}
    </PickList>
  );
}

/** The run's agents as a tree: who started whom, what each is doing, how many turns it has taken. */
function AgentList(props: {
  readonly selected: string | null;
  readonly compact: boolean;
  readonly open: (id: string) => void;
}): JSX.Element {
  return (
    <PickList
      label="Agents"
      size="fill"
      {...(props.compact
        ? {}
        : {
            header: {
              start: 'agent · started by the row above · now',
              end: [
                {label: 'turns', fixed: 'figure'},
                {label: 'took', fixed: 'trail'},
              ],
            },
          })}
      {...(props.selected === null ? {} : {selected: props.selected})}
    >
      {agentTree().map(({a, guides}) => {
        const also = AGENTS.find(other => other.id === a.alsoUnder);
        return (
          <PickRow
            key={a.id}
            id={a.id}
            guides={guides}
            lead={a.role}
            title={a.subject}
            mono
            {...(props.compact ? {} : {summary: a.activity})}
            state={{tone: a.tone, label: a.state}}
            badges={
              also === undefined
                ? []
                : [
                    {
                      label: props.compact ? '+1 parent' : `+ also under ${also.role}`,
                      tone: 'neutral' as const,
                    },
                  ]
            }
            figure={`${a.turns}`}
            figureTone="muted"
            {...(props.compact ? {} : {trail: {label: a.took}})}
            select={() => props.open(a.id)}
          />
        );
      })}
    </PickList>
  );
}

/** One agent opened: its latest turn as narrative. */
function AgentDetail(props: {readonly agent: Agent}): JSX.Element {
  const {agent} = props;
  const parent = AGENTS.find(a => a.id === agent.parent);
  const also = AGENTS.find(a => a.id === agent.alsoUnder);
  return (
    <Pane
      kicker={`agent · ${agent.role}`}
      title={agent.subject}
      width="fill"
      enterKey={`agent ${agent.id}`}
      meta={
        <>
          <Chip label={agent.state} tone={agent.tone} dot />
          <Text variant="code" tone="muted">
            {`${agent.turns} turns · ${agent.took}`}
            {parent === undefined ? '' : ` · started by ${parent.role}`}
            {also === undefined ? '' : ` · also fed by ${agentName(also)}`}
          </Text>
        </>
      }
    >
      <Text variant="section">{`turn ${agent.turns} of ${agent.turns}`}</Text>
      <TurnNarrative turn={TURN} selected={null} initiallyOpen={null} open={noop} />
    </Pane>
  );
}

function AttemptList(props: {readonly attempts: readonly Attempt[]}): JSX.Element {
  const latest = props.attempts[props.attempts.length - 1]?.n;
  return (
    <PickList label="Attempts" selected={`attempt-${latest}`}>
      {props.attempts.map(attempt => (
        <PickRow
          key={attempt.n}
          id={`attempt-${attempt.n}`}
          lead={`attempt ${attempt.n}`}
          title={attempt.value === null ? attempt.result : ratio(attempt.value)}
          {...(attempt.value === null
            ? {}
            : {summary: `${attempt.value.toLocaleString('en-US')} ops/s`})}
          mono
          state={
            attempt.value === null
              ? {tone: 'err', label: attempt.result}
              : {tone: 'ok', label: 'measured'}
          }
          figure={attempt.took}
          figureTone="muted"
          select={noop}
        />
      ))}
    </PickList>
  );
}

function Running(props: {readonly open: (c: string, step: string) => void}): JSX.Element {
  return (
    <Pane kicker="running" title="2" width="list">
      <PickList label="Running">
        <PickRow
          id="now-1"
          title="spin-backoff"
          mono
          summary="gate · 2/3"
          figure="1m 05s"
          figureTone="muted"
          state={{tone: 'live', label: 'Running'}}
          fresh
          select={() => props.open('spin-backoff', 'gate')}
        />
        <PickRow
          id="now-2"
          title="slot-arena"
          mono
          summary={`${BUILDER_LOWER} · editing src/lib.rs`}
          figure="46s"
          figureTone="muted"
          state={{tone: 'live', label: 'Running'}}
          select={() => props.open('slot-arena', 'impl-2')}
        />
      </PickList>
    </Pane>
  );
}

function ModeSwitch(props: {
  readonly mode: Mode;
  readonly options: readonly {readonly value: Mode; readonly label: string}[];
  readonly change: (mode: Mode) => void;
}): JSX.Element {
  return (
    <SegmentedControl
      label="Show"
      size="sm"
      options={props.options}
      value={props.mode}
      change={props.change}
    />
  );
}

const MAIN_LOGS = (main: string): {value: Mode; label: string}[] => [
  {value: 'main', label: main},
  {value: 'logs', label: 'Logs'},
];

function StepList(props: {
  readonly c: Candidate;
  readonly selected: string | null;
  readonly open: (step: string) => void;
}): JSX.Element {
  return (
    <PickList
      label={`${props.c.id} steps`}
      {...(props.selected === null ? {} : {selected: props.selected})}
    >
      {props.c.steps.map(step => (
        <PickRow
          key={step.id}
          id={step.id}
          lead={step.role}
          title={step.headline}
          figure={step.took}
          figureTone="muted"
          state={{tone: step.tone, label: step.state}}
          select={() => props.open(step.id)}
        />
      ))}
    </PickList>
  );
}

function CandidateDetail(props: {
  readonly c: Candidate;
  readonly mode: Mode;
  readonly setMode: (mode: Mode) => void;
  readonly openStep: (step: string) => void;
  readonly openParent: (id: string) => void;
  readonly flags: DesignFlags;
}): JSX.Element {
  const {c} = props;
  const dot = OUTCOME_DOT[c.outcome];
  const parent = candidate(c.parent);
  return (
    <Pane
      kicker="candidate"
      title={c.hypothesis}
      width="fill"
      enterKey={`c ${c.id}`}
      meta={
        <>
          <Chip label={dot.label} tone={dot.tone} dot />
          {c.value === null ? null : (
            <Text variant="metric" tone={c.outcome === 'passed' ? 'ok' : 'err'}>
              {ratio(c.value)}
            </Text>
          )}
          <Text variant="code" tone="muted">
            {c.value === null ? (c.verdict ?? '') : `${c.value.toLocaleString('en-US')} ops/s`}
            {parent === null || parent.value === null || c.value === null
              ? ''
              : ` · ${(c.value / parent.value).toFixed(2)}× ${parent.id}`}
            {` · ${c.took}`}
          </Text>
          {parent === null ? null : (
            <Row gap="space-1" align="center">
              <Text variant="small" tone="muted">
                built on
              </Text>
              <Button
                size="sm"
                variant="quiet"
                text={parent.id}
                press={() => props.openParent(parent.id)}
              />
            </Row>
          )}
        </>
      }
      actions={<ModeSwitch mode={props.mode} options={MAIN_LOGS('Steps')} change={props.setMode} />}
    >
      {props.mode === 'logs' ? (
        <LogPane size="fill" label={`${c.id} logs`} lines={gateLog(c)} />
      ) : (
        <>
          <KeyValue
            items={[
              {key: 'changed', value: 'src/lib.rs +38 −12', mono: true},
              ...(c.round === undefined
                ? []
                : [{key: 'round', value: String(c.round), mono: true}]),
            ]}
          />
          {c.attempts === undefined ? null : (
            <>
              <Text variant="section">attempts</Text>
              <AttemptList attempts={c.attempts} />
              <Text variant="section">{`attempt ${c.attempts.length} · steps`}</Text>
            </>
          )}
          <Gantt
            label={`${c.id} steps over time`}
            strip
            from={c.startedAt}
            to={c.outcome === 'running' ? now(props.flags) : c.startedAt + minutes(c.took)}
            rows={[ganttRow(c, props.flags, [], false, noop, props.openStep)]}
          />
          <StepList c={c} selected={null} open={props.openStep} />
        </>
      )}
    </Pane>
  );
}

function TurnDetail(props: {
  readonly c: Candidate;
  readonly step: Step;
  readonly mode: Mode;
  readonly setMode: (mode: Mode) => void;
  readonly openEntry: (id: string) => void;
  readonly flags: DesignFlags;
}): JSX.Element {
  const running = props.step.tone === 'live';
  return (
    <Pane
      kicker={`agent turn · ${props.step.role}`}
      title={props.step.headline}
      width="fill"
      enterKey={`turn ${props.c.id} ${props.step.id}`}
      meta={
        <>
          <Chip label={props.step.state} tone={props.step.tone} dot />
          <Text variant="code" tone="muted">
            {props.step.took} · {props.step.tools ?? 0} tools · 1 failed
          </Text>
        </>
      }
      actions={<ModeSwitch mode={props.mode} options={MAIN_LOGS('Turn')} change={props.setMode} />}
    >
      {props.mode === 'logs' ? (
        <LogPane size="fill" label="Turn log" lines={gateLog(props.c).slice(0, 4)} />
      ) : (
        <>
          <KeyValue items={REPLY_FACTS} />
          <TurnNarrative
            turn={TURN}
            selected={null}
            initiallyOpen={props.flags.open}
            open={props.openEntry}
          />
          {running ? <NewBelow text="3 new" press={noop} /> : null}
        </>
      )}
    </Pane>
  );
}

function GateDetail(props: {readonly c: Candidate; readonly step: Step}): JSX.Element {
  const running = props.c.outcome === 'running';
  const lines = gateLog(props.c);
  return (
    <Pane
      kicker="gate"
      title={props.step.headline}
      width="fill"
      enterKey={`gate ${props.c.id}`}
      meta={
        <>
          <Chip label={props.step.state} tone={props.step.tone} dot />
          <Text variant="code" tone="muted">
            {props.step.took}
          </Text>
        </>
      }
    >
      <KeyValue
        items={[
          {key: 'build', value: '18s', mono: true},
          {key: 'accuracy', value: '100/100', mono: true},
          {
            key: 'benchmark',
            value: running ? '14.10M · 14.30M · …' : '13.20M · 13.32M · 13.46M',
            mono: true,
          },
          {key: 'warnings', value: '1', mono: true},
        ]}
      />
      <LogPane size="fill" label="Gate log" lines={lines} />
    </Pane>
  );
}

function EntryInspector(props: {readonly entry: Entry}): JSX.Element {
  const {entry} = props;
  const [open, setOpen] = useState(true);
  return (
    <Pane
      kicker={`tool call · ${entry.name}`}
      title={entry.line}
      width="inspector"
      enterKey={`entry ${entry.id}`}
      meta={
        <>
          <Chip
            label={entry.state === 'failed' ? 'Failed' : 'Passed'}
            tone={entry.state === 'failed' ? 'err' : 'ok'}
            dot
          />
          <Text variant="code" tone="muted">
            {entry.meta ?? ''}
          </Text>
        </>
      }
      actions={<Button size="sm" variant="quiet" text="Copy" press={noop} />}
    >
      <Text variant="code">test result: ok. 18 passed; 0 failed</Text>
      <ToolCall
        name="execute"
        state={entry.state}
        summary={entry.line}
        input={entry.input ?? entry.line}
        output={entry.output ?? ''}
        open={open}
        toggle={setOpen}
      />
    </Pane>
  );
}

function RunLogs(): JSX.Element {
  return <LogPane size="fill" label="Run log" lines={NARRATION} />;
}

interface LevelProps {
  readonly zoom: Zoom;
  readonly flags: DesignFlags;
  readonly go: (zoom: Zoom) => void;
}

function AgentLevel(props: LevelProps & {readonly agent: Agent}): JSX.Element {
  const {zoom, flags, go, agent} = props;
  return (
    <Panes>
      {flags.narrow ? null : (
        <Pane kicker="agents" title={`${AGENTS.length}`} width="list">
          <AgentList selected={agent.id} compact open={id => go({...zoom, agent: id})} />
        </Pane>
      )}
      <AgentDetail key={agent.id} agent={agent} />
    </Panes>
  );
}

function ItemLevel(props: LevelProps & {readonly step: Step; readonly entry: Entry}): JSX.Element {
  const {zoom, flags, go, step, entry} = props;
  return (
    <Panes>
      {flags.narrow ? null : (
        <Pane kicker={`agent turn · ${step.role}`} title={step.headline} width="list">
          <TurnNarrative
            turn={TURN}
            key={entry.id}
            selected={entry.id}
            initiallyOpen={null}
            open={id => go({...zoom, item: id})}
          />
        </Pane>
      )}
      <EntryInspector entry={entry} />
    </Panes>
  );
}

function StepLevel(props: LevelProps & {readonly c: Candidate; readonly step: Step}): JSX.Element {
  const {zoom, flags, go, c, step} = props;
  return (
    <Panes>
      {flags.narrow ? null : (
        <Pane kicker="steps" title={c.id} width="list">
          <StepList c={c} selected={step.id} open={id => go({...zoom, step: id, mode: 'main'})} />
        </Pane>
      )}
      {step.kind === 'gate' ? (
        <GateDetail c={c} step={step} />
      ) : (
        <TurnDetail
          c={c}
          step={step}
          mode={zoom.mode}
          setMode={mode => go({...zoom, mode})}
          openEntry={id => go({...zoom, item: id})}
          flags={flags}
        />
      )}
    </Panes>
  );
}

function CandidateLevel(props: LevelProps & {readonly c: Candidate}): JSX.Element {
  const {zoom, flags, go, c} = props;
  return (
    <Panes>
      {flags.narrow ? null : (
        <Pane kicker={KICKER} title={`${CANDIDATES.length}`} width="list">
          <CandidateList
            selected={c.id}
            compact
            ended={flags.ended}
            open={id => go({...TOP, c: id})}
          />
        </Pane>
      )}
      <CandidateDetail
        c={settled(c, flags.ended)}
        flags={flags}
        openParent={id => go({...TOP, c: id})}
        mode={zoom.mode}
        setMode={mode => go({...zoom, mode})}
        openStep={id => go({...zoom, step: id, mode: 'main'})}
      />
    </Panes>
  );
}

/** The face of the run's list pane that the mode names. */
function RunFace(props: LevelProps): JSX.Element {
  const {zoom, flags, go} = props;
  if (zoom.mode === 'logs') return <RunLogs />;
  if (zoom.mode === 'timeline') return <Timeline flags={flags} go={go} />;
  if (zoom.mode === 'agents') {
    return (
      <AgentList
        selected={null}
        compact={false}
        open={id => go({...TOP, mode: 'agents', agent: id})}
      />
    );
  }
  return (
    <>
      <WinningPath open={id => go({...TOP, c: id})} />
      <CandidateList
        selected={null}
        compact={false}
        ended={flags.ended}
        open={id => go({...TOP, c: id})}
      />
    </>
  );
}

function RunLevel(props: LevelProps): JSX.Element {
  const {zoom, flags, go} = props;
  const agents = zoom.mode === 'agents';
  return (
    <>
      <Hero flags={flags} pick={id => go({...TOP, c: id})} />
      <Panes>
        <Pane
          kicker={agents ? 'agents' : KICKER}
          title={agents ? `${AGENTS.length}` : `${CANDIDATES.length}`}
          width="fill"
          meta={agents ? undefined : <DotLegend items={legend(flags.ended)} />}
          actions={
            <ModeSwitch
              mode={zoom.mode}
              options={[
                {value: 'main', label: 'Ideas'},
                {value: 'timeline', label: 'Timeline'},
                {value: 'agents', label: 'Agents'},
                {value: 'logs', label: 'Logs'},
              ]}
              change={mode => go({...zoom, mode})}
            />
          }
        >
          <RunFace {...props} />
        </Pane>
        {flags.ended ? null : <Running open={(cid, sid) => go({...TOP, c: cid, step: sid})} />}
      </Panes>
    </>
  );
}

/** The level the zoom names: the run, an agent, a candidate, a step, or one of its items. */
function Body(props: LevelProps): JSX.Element {
  const agent = AGENTS.find(a => a.id === props.zoom.agent) ?? null;
  if (agent !== null) return <AgentLevel {...props} agent={agent} />;
  const c = candidate(props.zoom.c);
  const step = c?.steps.find(s => s.id === props.zoom.step) ?? null;
  const entry = TURN_ENTRIES.find(e => e.id === props.zoom.item) ?? null;
  if (c === null) return <RunLevel {...props} />;
  if (step === null) return <CandidateLevel {...props} c={c} />;
  if (entry === null) return <StepLevel {...props} c={c} step={step} />;
  return <ItemLevel {...props} step={step} entry={entry} />;
}

function crumbs(zoom: Zoom, go: (zoom: Zoom) => void): Crumb[] {
  const c = candidate(zoom.c);
  const step = c?.steps.find(s => s.id === zoom.step) ?? null;
  const entry = TURN_ENTRIES.find(e => e.id === zoom.item) ?? null;
  const items: {id: string; label: string; to: Zoom}[] = [{id: 'run', label: 'Run', to: TOP}];
  const agent = AGENTS.find(a => a.id === zoom.agent);
  if (agent !== undefined)
    items.push(
      {id: 'agents', label: 'agents', to: {...TOP, mode: 'agents'}},
      {id: 'agent', label: agentName(agent), to: zoom},
    );
  if (c !== null) items.push({id: 'c', label: c.id, to: {...TOP, c: c.id}});
  if (c !== null && step !== null)
    items.push({id: 'step', label: step.role, to: {...TOP, c: c.id, step: step.id}});
  if (c !== null && step !== null && entry !== null)
    items.push({id: 'item', label: entry.line, to: zoom});
  return items.map((item, index) =>
    index === items.length - 1
      ? {id: item.id, label: item.label}
      : {id: item.id, label: item.label, press: () => go(item.to)},
  );
}

/** The Run screen layout prototype at `initial`. */
export function RunLayoutDesign(props: {
  readonly initial: Zoom;
  readonly flags: DesignFlags;
}): JSX.Element {
  const [zoom, setZoom] = useState<Zoom>(props.initial);
  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') setZoom(current => up(current));
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  const zoomed = zoom.c !== null || zoom.agent !== null;
  return (
    <AppFrame label="Runs" nav={<Sidebar />}>
      <Screen density="compact" header={<Header flags={props.flags} compactHero={zoomed} />}>
        {zoomed ? <Breadcrumb label="Zoom" items={crumbs(zoom, setZoom)} /> : null}
        <Body zoom={zoom} flags={props.flags} go={setZoom} />
      </Screen>
    </AppFrame>
  );
}
