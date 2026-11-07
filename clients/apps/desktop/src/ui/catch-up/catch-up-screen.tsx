import {
  BarRows,
  Breadcrumb,
  Button,
  Card,
  Chip,
  EmptyState,
  Grid,
  Markdown,
  PickList,
  PickRow,
  Row,
  Split,
  Stack,
  Stat,
  Text,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import type {CatchUpPress, CatchUpRefusal, CheckInRead, FaultKind} from '../../state/index.js';
import {formatDayTime, formatSpan} from '../agents/time.js';
import {NOT_REPORTED} from '../cost/words.js';
import {
  type CatchUpModel,
  type CatchUpWords,
  catchUpModel,
  costFigure,
  type ObjectiveFigures,
  type ReportWords,
  type SummaryModel,
  type Tiles,
} from './catch-up-model.js';

/** What the page says of a press that did not make a check-in. */
const REFUSALS = {
  'run-not-found': 'Run not found',
  'no-viewer': 'No viewer',
  'not-permitted': 'Not permitted',
  unexpected: 'Refused',
} as const satisfies Record<CatchUpRefusal, string>;

export interface CatchUpPageWords extends CatchUpWords {
  /** Why a call failed, in a sentence a person can act on. */
  readonly fault: (kind: FaultKind) => string;
}

/** The press of Catch me up as a line beside its button: nothing once the check-in is made. */
function PressLine(props: {
  readonly press: CatchUpPress | undefined;
  readonly words: CatchUpPageWords;
  readonly again: () => void;
}): JSX.Element | null {
  const {press} = props;
  if (press === undefined || press._tag === 'Made') return null;
  switch (press._tag) {
    case 'Pressing':
      return <Chip label="Sending" tone="live" dot />;
    case 'Refused':
      return <Chip label={REFUSALS[press.reason]} tone="err" dot />;
    case 'Unconfirmed':
      return (
        <Row gap="space-3" wrap>
          <Chip label="Not confirmed" tone="warn" dot hint={press.fault.detail} />
          <Text variant="small" tone="muted">
            {props.words.fault(press.fault.kind)}
          </Text>
          <Button text="Try again" size="sm" press={props.again} />
        </Row>
      );
    case 'Failed':
      return (
        <Row gap="space-3" wrap>
          <Chip label="Not sent" tone="err" dot hint={press.fault.detail} />
          <Text variant="small" tone="muted">
            {props.words.fault(press.fault.kind)}
          </Text>
        </Row>
      );
  }
}

function CatchUpButton(props: {
  readonly press: CatchUpPress | undefined;
  readonly catchUp: () => void;
}): JSX.Element {
  return (
    <Button
      text="Catch me up"
      variant="primary"
      press={props.catchUp}
      {...(props.press?._tag === 'Pressing' ? {disabledReason: 'Sending.', quietReason: true} : {})}
    />
  );
}

function ReportBlock(props: {readonly report: ReportWords}): JSX.Element {
  const {report} = props;
  return (
    <Stack gap="space-3">
      <Text variant="heading">{report.headline}</Text>
      {report.parts.map(part => (
        <Stack key={part.label} gap="space-1">
          <Text variant="section">{part.label}</Text>
          <Markdown text={part.text} />
        </Stack>
      ))}
    </Stack>
  );
}

/** The lead's words for the window: Writing until they arrive, the report, or the reports posted inside it. */
function SummaryCard(props: {readonly summary: SummaryModel}): JSX.Element | null {
  const {summary} = props;
  if (summary.kind === 'reports' && summary.reports.length === 0) return null;
  return (
    <Card section="Lead summary">
      <Stack gap="space-4">
        {summary.kind === 'writing' ? <Chip label="Writing…" tone="live" dot /> : null}
        {summary.kind === 'report' ? <ReportBlock report={summary.report} /> : null}
        {summary.kind === 'reports'
          ? summary.reports.map((report, at) => (
              // The reports of a window are in the order posted and never reorder.
              // biome-ignore lint/suspicious/noArrayIndexKey: position is the identity of a posted report.
              <ReportBlock key={at} report={report} />
            ))
          : null}
      </Stack>
    </Card>
  );
}

function ObjectiveLine(props: {readonly objective: ObjectiveFigures}): JSX.Element {
  const {objective} = props;
  return (
    <Stack gap="space-1">
      <Text variant="small" tone="muted">
        {objective.metric}
      </Text>
      <span title={objective.source}>
        <Row gap="space-3" align="baseline">
          {objective.start === null ? null : (
            <Text variant="metric" tone="muted">
              {objective.start}
            </Text>
          )}
          {objective.start === null ? null : <Text tone="muted">→</Text>}
          <Text variant="metric">{objective.end}</Text>
        </Row>
      </span>
    </Stack>
  );
}

/** The window's figures, a card for each kind: what the host counted and nothing it did not. */
function TileCards(props: {readonly tiles: Tiles}): JSX.Element {
  const {tiles} = props;
  return (
    <Grid min="tile" gap="space-4">
      <Card section="Best">
        {tiles.objectives.length === 0 ? (
          <Text tone="muted">{NOT_REPORTED}</Text>
        ) : (
          <Stack gap="space-4">
            {tiles.objectives.map(objective => (
              <ObjectiveLine key={objective.metric} objective={objective} />
            ))}
          </Stack>
        )}
      </Card>
      <Card section="Ideas">
        <Row gap="space-7" rowGap="space-4" wrap>
          <Stat label="Created" value={tiles.ideas.created} />
          <Stat label="Changed" value={tiles.ideas.changed} />
        </Row>
      </Card>
      <Card section="Agents">
        <Row gap="space-7" rowGap="space-4" wrap>
          <Stat label="Started" value={tiles.agents.started} />
          <Stat label="Ended" value={tiles.agents.ended} />
        </Row>
      </Card>
      <Card section="Jobs">
        <Stat label="Measured" value={tiles.jobsMeasured} />
      </Card>
      <Card section="Spend">
        <Stack gap="space-4">
          <Row gap="space-4" align="baseline" wrap>
            <Text variant="hero" tone={tiles.spend.total === null ? 'muted' : 'default'}>
              {tiles.spend.total ?? NOT_REPORTED}
            </Text>
            {tiles.spend.unpriced === null ? null : (
              <Text variant="small" tone="muted">{`${tiles.spend.unpriced} unpriced`}</Text>
            )}
          </Row>
          {tiles.spend.byType.length === 0 ? null : (
            <BarRows
              label="Spend by agent type"
              nameHeader="Type"
              barLabel="Share of the largest cost"
              columns={[{header: 'Turns'}, {header: 'Cost', strong: true}]}
              rows={tiles.spend.byType}
            />
          )}
        </Stack>
      </Card>
    </Grid>
  );
}

/** The window the page shows: its title, how long it is, where its facts stand, and what it holds. */
function Window(props: {readonly model: CatchUpModel}): JSX.Element {
  const {model} = props;
  return (
    <Stack gap="space-4">
      <Stack gap="space-2">
        <Text as="h2" variant="title">
          {model.title}
        </Text>
        <Row gap="space-3" wrap>
          <Chip label={model.span} />
          <Text variant="small" tone="muted">
            {model.until}
          </Text>
          {model.standing.kind === 'working' ? <Chip label="Working…" tone="live" dot /> : null}
          {model.standing.kind === 'failed' ? <Chip label="Failed" tone="err" dot /> : null}
        </Row>
        {model.standing.kind === 'failed' ? (
          <Text variant="small" tone="err">
            {model.standing.reason}
          </Text>
        ) : null}
      </Stack>
      <SummaryCard summary={model.summary} />
      {model.tiles === null ? null : <TileCards tiles={model.tiles} />}
    </Stack>
  );
}

/** One check-in as a row of the list: when it was made, how long it covers, and what it cost. */
function CheckInRow(props: {readonly read: CheckInRead; readonly select: () => void}) {
  const {read} = props;
  const figure = costFigure(read);
  const state =
    read.facts.kind === 'ready'
      ? ({tone: 'ok', label: 'Ready'} as const)
      : read.facts.kind === 'failed'
        ? ({tone: 'err', label: 'Failed'} as const)
        : ({tone: 'live', label: 'Working'} as const);
  return (
    <PickRow
      id={read.checkIn.id}
      title={formatDayTime(read.atMs)}
      summary={formatSpan(read.atMs - read.sinceMs)}
      figure={figure ?? (read.facts.kind === 'failed' ? 'Failed' : 'Working…')}
      figureTone={figure === null ? 'muted' : 'default'}
      state={state}
      select={props.select}
    />
  );
}

/**
 * Catch me up for one run: what changed since the viewer last checked in, and the check-ins before
 * it. `rows` are this viewer's check-ins in the run, newest first, as the host stored them; the page
 * shows the newest until one is picked, and a picked one reads the same every time because its
 * facts were frozen when it was made. It computes none of the figures.
 */
export function CatchUpScreen(props: {
  /** The run's name, for the breadcrumb. */
  readonly run: string;
  readonly rows: readonly CheckInRead[];
  readonly press: CatchUpPress | undefined;
  readonly catchUp: () => void;
  readonly goRuns: () => void;
  readonly goRun: () => void;
  readonly words: CatchUpPageWords;
}): JSX.Element {
  const [picked, setPicked] = useState<string | null>(null);
  const shown = props.rows.find(read => read.checkIn.id === picked) ?? props.rows[0];
  return (
    <Stack gap="space-4">
      <Row gap="space-3" wrap apart>
        <Breadcrumb
          label="Where you are"
          items={[
            {id: 'runs', label: 'Runs', press: props.goRuns},
            {id: 'run', label: props.run, press: props.goRun},
            {id: 'catch-up', label: 'Catch me up'},
          ]}
        />
        <CatchUpButton press={props.press} catchUp={props.catchUp} />
      </Row>
      <div role="status" aria-live="polite" aria-label="Catch me up: outcome">
        <PressLine press={props.press} words={props.words} again={props.catchUp} />
      </div>
      {shown === undefined ? (
        <EmptyState title="No check-ins yet" />
      ) : (
        <Split
          asideFirst
          aside={
            <Card section="Check-ins" flush>
              <PickList
                label="Check-ins"
                selected={shown.checkIn.id}
                header={{start: 'When', end: [{label: 'Cost', fixed: 'figure'}]}}
              >
                {props.rows.map(read => (
                  <CheckInRow
                    key={read.checkIn.id}
                    read={read}
                    select={() => setPicked(read.checkIn.id)}
                  />
                ))}
              </PickList>
            </Card>
          }
          main={<Window model={catchUpModel(shown, props.words)} />}
        />
      )}
    </Stack>
  );
}
