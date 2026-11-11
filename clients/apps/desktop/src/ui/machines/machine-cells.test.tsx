import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import type {
  AbsenceCause,
  GpuReading,
  MachineSample,
  MachineView,
  MemoryUse,
  Section,
} from '../../state/index.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {Reading, type ReadingPart, readingSpan} from './machine-cells.js';
import {
  ABSENCE_CAUSES,
  absenceNote,
  MACHINE_CHIP_WHYS,
  machineWhyWord,
  whyHint,
  whyShort,
} from './machine-words.js';

installDom();

const GIB = 1024 ** 3;
const PARTS: readonly ReadingPart[] = ['cpu', 'memory', 'gpu'];

const read = <T,>(value: T): Section<T> => ({_tag: 'Read', value});
const absent = <T,>(cause: AbsenceCause): Section<T> => ({_tag: 'Absent', cause});

const memory = (usedGib: number, totalGib: number): MemoryUse => ({
  usedBytes: usedGib * GIB,
  totalBytes: totalGib * GIB,
});

const gpu = (index: number, over: Partial<GpuReading> = {}): GpuReading => ({
  index,
  name: `GPU ${index}`,
  utilizationPercent: 40,
  memory: memory(8, 24),
  ...over,
});

const sampleOf = (over: Partial<MachineSample> = {}): MachineSample => ({
  cpuPercent: read(12.5),
  memory: read(memory(4, 16)),
  gpus: read([]),
  ...over,
});

const view = (
  sample: MachineSample,
  stale: Extract<MachineView, {_tag: 'Shown'}>['stale'] = null,
) => ({_tag: 'Shown', sample, stale}) as const;

/** The three cells of a row, as the table draws them: one `Reading` for each column. */
const cells = (v: MachineView | null) =>
  PARTS.map(which => mount(<Reading view={v} which={which} />).container);

const gauges = (container: HTMLElement): HTMLElement[] => [
  ...container.querySelectorAll<HTMLElement>('.vs-gauge'),
];

describe('the cells of a machine row', () => {
  it('draw nothing for a window that does not read machine stats', () => {
    for (const cell of cells(null)) expect(cell.textContent).toBe('');
  });

  it('say Waiting in the first cell only, which spans all three, while nothing has been read', () => {
    expect(readingSpan({_tag: 'Waiting'})).toBe(3);
    const [cpu, mem, gpus] = cells({_tag: 'Waiting'});
    expect(cpu?.textContent).toBe('Waiting for the first reading');
    expect(mem?.textContent).toBe('');
    expect(gpus?.textContent).toBe('');
  });

  it('say No stats and why in one line across the cells, with the longer account on hover, for any why', () => {
    const whys = fc.constantFrom(...MACHINE_CHIP_WHYS, 'disconnected' as const);
    fc.assert(
      fc.property(whys, why => {
        const [cpu, mem, gpus] = cells({_tag: 'Missing', why});
        expect(readingSpan({_tag: 'Missing', why})).toBe(3);
        expect(cpu?.textContent).toBe(`No stats · ${whyShort(why)}`);
        expect(cpu?.querySelector('[title]')?.getAttribute('title')).toBe(whyHint(why));
        expect(mem?.textContent).toBe('');
        expect(gpus?.textContent).toBe('');
        // The words say it is a hint, never a second status chip.
        expect(cpu?.querySelector('.vs-chip')).toBeNull();
      }),
      {numRuns: MACHINE_CHIP_WHYS.length + 1},
    );
  });

  it('hint every chip word in a whole sentence, and disconnected in plain words', () => {
    for (const why of MACHINE_CHIP_WHYS) {
      expect(whyHint(why)).toBe(`${machineWhyWord(why).label}. ${machineWhyWord(why).hint}`);
    }
    expect(whyHint('disconnected')).toContain('link');
  });

  it('draw a bare meter for CPU and memory, undimmed, for a current reading', () => {
    const [cpu, mem] = cells(view(sampleOf()));
    expect(cpu && gauges(cpu).map(each => each.getAttribute('aria-valuetext'))).toEqual(['12.5%']);
    expect(mem && gauges(mem).map(each => each.getAttribute('aria-valuetext'))).toEqual([
      '4 / 16 GiB',
    ]);
    for (const cell of [cpu, mem]) {
      // The column header names the reading: the cell draws no label of its own.
      expect(cell?.textContent?.includes('CPU')).toBe(false);
      for (const gauge of gauges(cell as HTMLElement))
        expect(gauge.hasAttribute('data-dim')).toBe(false);
    }
  });

  it('draw a memory near its total as a warning', () => {
    const [, mem] = cells(view(sampleOf({memory: read(memory(15, 16))})));
    expect(mem?.querySelector('.vs-meter')?.getAttribute('data-tone')).toBe('warn');
    const [, calm] = cells(view(sampleOf({memory: read(memory(4, 16))})));
    // Load is one fill; green is not drawn for health, so it never means two things.
    expect(calm?.querySelector('.vs-meter')?.getAttribute('data-tone')).toBe('accent');
    const [cpu] = cells(view(sampleOf({cpuPercent: read(99)})));
    expect(cpu?.querySelector('.vs-meter')?.getAttribute('data-tone')).toBe('accent');
  });

  it('dim a stale reading and say why on hover, with no chip, for any why', () => {
    const whys = fc.constantFrom(...MACHINE_CHIP_WHYS, 'disconnected' as const);
    fc.assert(
      fc.property(whys, stale => {
        for (const cell of cells(view(sampleOf({gpus: read([gpu(0)])}), stale))) {
          expect(gauges(cell)).toHaveLength(1);
          expect(gauges(cell)[0]?.hasAttribute('data-dim')).toBe(true);
          expect(cell.querySelector('[title]')?.getAttribute('title')).toContain(whyHint(stale));
          expect(cell.querySelector('.vs-chip')).toBeNull();
        }
      }),
      {numRuns: MACHINE_CHIP_WHYS.length + 1},
    );
  });

  it('say "No GPUs" in words for a machine the host says has none, and draw no meter', () => {
    const [, , gpus] = cells(view(sampleOf({gpus: read([])})));
    expect(gpus?.textContent).toBe('No GPUs');
    expect(gauges(gpus as HTMLElement)).toHaveLength(0);
  });

  it('say unknown, quietly and with the cause on hover, never a bar, for a section the host could not read', () => {
    const causes = fc.constantFrom(...ABSENCE_CAUSES);
    fc.assert(
      fc.property(causes, cause => {
        const v = view(
          sampleOf({cpuPercent: absent(cause), memory: absent(cause), gpus: absent(cause)}),
        );
        for (const cell of cells(v)) {
          expect(cell.textContent).toBe('unknown');
          expect(gauges(cell)).toHaveLength(0);
          expect(cell.querySelector('.vs-meter')).toBeNull();
          expect(cell.querySelector('[title]')?.getAttribute('title')).toContain(
            absenceNote(cause),
          );
        }
      }),
      {numRuns: ABSENCE_CAUSES.length},
    );
  });

  it('say unknown for a GPU section that is missing and No GPUs for a machine with none, never alike', () => {
    const missing = cells(view(sampleOf({gpus: absent('no-tool')})))[2];
    const none = cells(view(sampleOf({gpus: read([])})))[2];
    expect(missing?.textContent).toBe('unknown');
    expect(none?.textContent).toBe('No GPUs');
    expect(missing?.querySelector('.vs-meter')).toBeNull();
    expect(none?.querySelector('.vs-meter')).toBeNull();
  });

  it('show one GPU as its own utilization, and many as their mean with how many there are', () => {
    const one = cells(view(sampleOf({gpus: read([gpu(0, {utilizationPercent: 92})])})))[2];
    expect(one && gauges(one).map(each => each.getAttribute('aria-valuetext'))).toEqual(['92%']);
    expect(one?.textContent).not.toContain('GPUs');
    const two = cells(
      view(
        sampleOf({
          gpus: read([gpu(0, {utilizationPercent: 92}), gpu(1, {utilizationPercent: 0})]),
        }),
      ),
    )[2];
    expect(two && gauges(two).map(each => each.getAttribute('aria-valuetext'))).toEqual(['46%']);
    expect(two?.textContent).toContain('· 2 GPUs');
    // The count is set apart from the percent by its mark, so no two figures touch.
    expect(two?.textContent).not.toMatch(/%\d/);
  });

  it('keep each GPU’s own utilization and memory on hover, one line each, for any count', () => {
    fc.assert(
      fc.property(fc.integer({min: 1, max: 8}), count => {
        const gpus = Array.from({length: count}, (_, index) => gpu(index));
        const cell = cells(view(sampleOf({gpus: read(gpus)})))[2];
        const hover = cell?.querySelector('[title]')?.getAttribute('title') ?? '';
        expect(hover.split('\n')).toHaveLength(count);
        gpus.forEach((_, index) => {
          expect(hover).toContain(`GPU ${index}: 40%, 8 / 24 GiB`);
        });
      }),
      {numRuns: 20},
    );
  });
});
