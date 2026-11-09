/**
 * What a host's `MachineView` shows: CPU, memory and each GPU as gauges, a plain "No GPUs" for a
 * machine the host says has none (never a gauge, which would read as a reading GPU 0 could not
 * give), and a chip for why a reading is not current or absent. `disconnected` draws only the dim:
 * the host's one status chip already says the link is down, and these readings never repeat it.
 */
import {Chip, Gauge, Readings, ReadingsGroup, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {GpuReading, MachineView, MachineWhy, MemoryUse, Section} from '../../state/index.js';
import {formatFigure} from '../figures.js';
import {absenceNote, type MachineWhyWord, machineWhyWord} from './machine-words.js';

/** A reading the host never made: distinct from a zero, which would claim one. */
const UNREADABLE = '–';
/** Wide enough for every percent this topic sends (0 to 100, four significant figures). */
const PERCENT_WIDTH = 6;
/** A floor under a memory figure's width, so a small machine's figure does not look cramped. */
const MEMORY_WIDTH = 10;
/** A memory reading at or past this share of its total reads as hot. */
const HOT_SHARE = 0.9;

const GIB = 1024 ** 3;
const TIB = 1024 ** 4;

/** `usedBytes` of `totalBytes`, in GiB, or TiB once the total reaches 1000 GiB. */
function memoryFigure(usedBytes: number, totalBytes: number): string {
  const inTib = totalBytes >= 1000 * GIB;
  const unit = inTib ? TIB : GIB;
  return `${formatFigure(usedBytes / unit)} / ${formatFigure(totalBytes / unit)} ${inTib ? 'TiB' : 'GiB'}`;
}

const memoryShare = (memory: MemoryUse): number =>
  memory.totalBytes > 0 ? memory.usedBytes / memory.totalBytes : 0;

/** A gauge of a section that may be absent, dimmed while `dim` and noting why when it is absent. */
function SectionGauge<T>(props: {
  readonly label: string;
  readonly section: Section<T>;
  readonly dim: boolean;
  readonly figuresOf: (value: T) => {
    readonly share: number;
    readonly figure: string;
    readonly figureWidth: number;
    readonly tone?: 'ok' | 'warn';
  };
}): JSX.Element {
  const {label, section, dim} = props;
  if (section._tag === 'Absent') {
    return (
      <Gauge
        label={label}
        share={null}
        figure={UNREADABLE}
        figureWidth={Math.max(label.length, UNREADABLE.length)}
        dim={dim}
        note={absenceNote(section.cause)}
      />
    );
  }
  const {share, figure, figureWidth, tone} = props.figuresOf(section.value);
  return (
    <Gauge
      label={label}
      share={share}
      figure={figure}
      figureWidth={figureWidth}
      dim={dim}
      {...(tone === undefined ? {} : {tone})}
    />
  );
}

const CpuGauge = (props: {
  readonly section: Section<number>;
  readonly dim: boolean;
}): JSX.Element => (
  <SectionGauge
    label="CPU"
    section={props.section}
    dim={props.dim}
    figuresOf={value => ({
      share: value / 100,
      figure: `${formatFigure(value)}%`,
      figureWidth: PERCENT_WIDTH,
    })}
  />
);

const MemoryGauge = (props: {
  readonly label: string;
  readonly section: Section<MemoryUse>;
  readonly dim: boolean;
}): JSX.Element => (
  <SectionGauge
    label={props.label}
    section={props.section}
    dim={props.dim}
    figuresOf={memory => {
      const figure = memoryFigure(memory.usedBytes, memory.totalBytes);
      const share = memoryShare(memory);
      return {
        share,
        figure,
        figureWidth: Math.max(MEMORY_WIDTH, figure.length),
        tone: share >= HOT_SHARE ? 'warn' : 'ok',
      };
    }}
  />
);

/** One GPU's utilization and memory, grouped under its own name for assistive technology. */
function GpuRow(props: {readonly gpu: GpuReading; readonly dim: boolean}): JSX.Element {
  const {gpu, dim} = props;
  return (
    <ReadingsGroup label={`GPU ${gpu.index}`}>
      <Readings>
        <Gauge
          label={`GPU ${gpu.index}`}
          share={gpu.utilizationPercent / 100}
          figure={`${formatFigure(gpu.utilizationPercent)}%`}
          figureWidth={PERCENT_WIDTH}
          dim={dim}
        />
        <Gauge
          label="Memory"
          share={memoryShare(gpu.memory)}
          figure={memoryFigure(gpu.memory.usedBytes, gpu.memory.totalBytes)}
          figureWidth={Math.max(
            MEMORY_WIDTH,
            memoryFigure(gpu.memory.usedBytes, gpu.memory.totalBytes).length,
          )}
          dim={dim}
          tone={memoryShare(gpu.memory) >= HOT_SHARE ? 'warn' : 'ok'}
        />
      </Readings>
    </ReadingsGroup>
  );
}

/** The one line that shows a machine has no GPU, or that the GPU section could not be read. */
function GpuAbsence(props: {
  readonly section: Section<readonly GpuReading[]>;
  readonly dim: boolean;
}): JSX.Element | null {
  const {section, dim} = props;
  if (section._tag === 'Absent') {
    return (
      <Gauge
        label="GPU"
        share={null}
        figure={UNREADABLE}
        figureWidth={Math.max('GPU'.length, UNREADABLE.length)}
        dim={dim}
        note={absenceNote(section.cause)}
      />
    );
  }
  if (section.value.length > 0) return null;
  return (
    <Text variant="small" tone="muted">
      No GPUs
    </Text>
  );
}

const whyChip = (why: MachineWhy): JSX.Element | null => {
  if (why === 'disconnected') return null;
  const word: MachineWhyWord = machineWhyWord(why);
  return <Chip label={word.label} tone={word.tone} hint={word.hint} />;
};

/**
 * CPU, memory and each GPU of a host's machine, or the chip that says why there is no reading.
 * `Waiting` and a disconnected `Missing` draw an empty row, so the surface around it keeps its
 * height without saying anything the host's status chip has not already said.
 */
export function MachineReadings(props: {readonly view: MachineView}): JSX.Element {
  const {view} = props;
  if (view._tag === 'Waiting') return <Readings />;
  if (view._tag === 'Missing') {
    const chip = whyChip(view.why);
    return <Readings>{chip}</Readings>;
  }
  const {sample, stale} = view;
  const dim = stale !== null;
  return (
    <>
      <Readings>
        <CpuGauge section={sample.cpuPercent} dim={dim} />
        <MemoryGauge label="Memory" section={sample.memory} dim={dim} />
        <GpuAbsence section={sample.gpus} dim={dim} />
        {stale === null ? null : whyChip(stale)}
      </Readings>
      {sample.gpus._tag === 'Read' &&
        sample.gpus.value.map(gpu => <GpuRow key={gpu.index} gpu={gpu} dim={dim} />)}
    </>
  );
}
