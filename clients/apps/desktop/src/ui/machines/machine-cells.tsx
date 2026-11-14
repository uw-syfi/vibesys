/**
 * What a machine's row says of its CPU, memory and GPU: each a meter with its figure directly after
 * it, set in the column that names it, so no cell repeats its own name. A machine with no current
 * reading says why in one line across the three cells (`No stats · not supported on this host`, or
 * `Waiting for the first reading`), with the longer account on hover. A reading that is no longer
 * current stays, dimmed, with why on hover; a section the host could not read says `unknown`, muted,
 * with its cause on hover (never a bar, which would read as a zero). `disconnected` only dims: the
 * row's status chip already says the link is down. The load meters share one accent fill; only a
 * memory near its total is drawn as a warning.
 */
import {Gauge, Row, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type GpuReading,
  gpuOverview,
  type MachineSample,
  type MachineView,
  type MemoryUse,
  type Section,
} from '../../state/index.js';
import {UNKNOWN} from '../plain-words.js';
import {
  gpuLines,
  HOT_SHARE,
  MEMORY_WIDTH,
  memoryFigure,
  memoryShare,
  PERCENT_WIDTH,
  percentFigure,
} from './machine-figures.js';
import {absenceNote, machineWhyWord, whyHint, whyShort} from './machine-words.js';

/** The three columns of readings. */
export type ReadingPart = 'cpu' | 'memory' | 'gpu';

/** What `Reading` needs to draw a part of a sample: whether it is dimmed, and the hover and note that say why. */
interface Currency {
  readonly dim: boolean;
  /** Why the reading is not current, for the hover; empty while it is. */
  readonly hover: string;
  /** The same, as the meter's spoken note. */
  readonly note: string | undefined;
}

function currencyOf(view: Extract<MachineView, {readonly _tag: 'Shown'}>): Currency {
  const {stale} = view;
  if (stale === null) return {dim: false, hover: '', note: undefined};
  return {
    dim: true,
    hover: whyHint(stale),
    note: stale === 'disconnected' ? 'not current' : machineWhyWord(stale).label.toLowerCase(),
  };
}

const hovered = (...lines: readonly string[]): string | undefined => {
  const text = lines.filter(line => line !== '').join('\n');
  return text === '' ? undefined : text;
};

/** A section the host could not read: the word `unknown`, quiet, and the cause on hover. */
function Unread(props: {
  readonly label: string;
  readonly cause: Parameters<typeof absenceNote>[0];
  readonly currency: Currency;
}): JSX.Element {
  return (
    <Text
      variant="small"
      tone="muted"
      title={hovered(`${props.label}: ${absenceNote(props.cause)}`, props.currency.hover) ?? ''}
    >
      {UNKNOWN}
    </Text>
  );
}

function CpuReading(props: {
  readonly section: Section<number>;
  readonly currency: Currency;
}): JSX.Element {
  const {section, currency} = props;
  if (section._tag === 'Absent') {
    return <Unread label="CPU" cause={section.cause} currency={currency} />;
  }
  return (
    <span title={hovered(currency.hover)}>
      <Gauge
        bare
        label="CPU"
        share={section.value / 100}
        figure={percentFigure(section.value)}
        figureWidth={PERCENT_WIDTH}
        tone="accent"
        dim={currency.dim}
        {...(currency.note === undefined ? {} : {note: currency.note})}
      />
    </span>
  );
}

function MemoryReading(props: {
  readonly section: Section<MemoryUse>;
  readonly currency: Currency;
}): JSX.Element {
  const {section, currency} = props;
  if (section._tag === 'Absent') {
    return <Unread label="Memory" cause={section.cause} currency={currency} />;
  }
  const figure = memoryFigure(section.value);
  const share = memoryShare(section.value);
  return (
    <span title={hovered(currency.hover)}>
      <Gauge
        bare
        label="Memory"
        share={share}
        figure={figure}
        figureWidth={Math.max(MEMORY_WIDTH, figure.length)}
        tone={share >= HOT_SHARE ? 'warn' : 'accent'}
        dim={currency.dim}
        {...(currency.note === undefined ? {} : {note: currency.note})}
      />
    </span>
  );
}

/**
 * The GPUs as one meter: the mean of their utilization, with how many there are when more than one.
 * Each GPU's own utilization and memory is on hover, which is also where their memory is read.
 */
function GpuReadings(props: {
  readonly section: Section<readonly GpuReading[]>;
  readonly currency: Currency;
}): JSX.Element {
  const {section, currency} = props;
  if (section._tag === 'Absent') {
    return <Unread label="GPU" cause={section.cause} currency={currency} />;
  }
  const overview = gpuOverview(section.value);
  if (overview === null) {
    return (
      <Text variant="small" tone="muted">
        No GPUs
      </Text>
    );
  }
  const many = overview.count > 1;
  const hover = hovered(gpuLines(section.value), currency.hover);
  return (
    <Row gap="space-2">
      <span title={hover}>
        <Gauge
          bare
          label={many ? 'GPU average' : 'GPU'}
          share={overview.meanUtilizationPercent / 100}
          figure={percentFigure(overview.meanUtilizationPercent)}
          figureWidth={PERCENT_WIDTH}
          tone="accent"
          dim={currency.dim}
          {...(currency.note === undefined ? {} : {note: currency.note})}
        />
      </span>
      {many ? (
        <Text variant="small" tone="muted" {...(hover === undefined ? {} : {title: hover})}>
          {`· ${overview.count} GPUs`}
        </Text>
      ) : null}
    </Row>
  );
}

const part = (sample: MachineSample, which: ReadingPart, currency: Currency): JSX.Element => {
  switch (which) {
    case 'cpu':
      return <CpuReading section={sample.cpuPercent} currency={currency} />;
    case 'memory':
      return <MemoryReading section={sample.memory} currency={currency} />;
    case 'gpu':
      return <GpuReadings section={sample.gpus} currency={currency} />;
  }
};

/** How many cells the readings of `view` cover from the first: all three when there is no reading to put in them. */
export const readingSpan = (view: MachineView | null): number =>
  view === null || view._tag === 'Shown' ? 1 : READING_PARTS.length;

const READING_PARTS: readonly ReadingPart[] = ['cpu', 'memory', 'gpu'];

/**
 * One cell of a machine row: `which` part of its readings. `view` is null for a window that does not
 * read machine stats at all, which draws nothing. A view with no reading is one line in the first
 * cell, which the row's table lets span the three (`readingSpan`).
 */
export function Reading(props: {
  readonly view: MachineView | null;
  readonly which: ReadingPart;
}): JSX.Element | null {
  const {view, which} = props;
  if (view === null) return null;
  if (view._tag === 'Shown') return part(view.sample, which, currencyOf(view));
  if (which !== 'cpu') return null;
  if (view._tag === 'Waiting') {
    return (
      <Text variant="small" tone="muted" title="The first reading has not arrived yet.">
        Waiting for the first reading
      </Text>
    );
  }
  return (
    <Text variant="small" tone="muted" title={whyHint(view.why)}>
      {`No stats · ${whyShort(view.why)}`}
    </Text>
  );
}
