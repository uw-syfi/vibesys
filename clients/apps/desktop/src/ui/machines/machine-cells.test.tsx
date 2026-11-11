import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import type {
  AbsenceCause,
  GpuReading,
  MachineSample,
  MemoryUse,
  Section,
} from '../../state/index.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {MachineReadings} from './machine-readings.js';
import {MACHINE_CHIP_WHYS, machineWhyWord} from './machine-words.js';

installDom();

const GIB = 1024 ** 3;

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

const gaugeNames = (container: HTMLElement): string[] =>
  [...container.querySelectorAll('.vs-gauge')].map(el => el.getAttribute('aria-label') ?? '');

const chips = (container: HTMLElement): HTMLElement[] => [
  ...container.querySelectorAll<HTMLElement>('.vs-chip'),
];

const groups = (container: HTMLElement): HTMLElement[] => [
  ...container.querySelectorAll<HTMLElement>('fieldset.vs-readings-group'),
];

const chipWhy = fc.constantFrom(...MACHINE_CHIP_WHYS);

describe('what a host machine view draws', () => {
  it('draws an empty readings row, and no chip, while it is still waiting', () => {
    const {container} = mount(<MachineReadings view={{_tag: 'Waiting'}} />);
    expect(container.querySelector('.vs-readings')).not.toBeNull();
    expect(gaugeNames(container)).toEqual([]);
    expect(chips(container)).toHaveLength(0);
  });

  it('says nothing more than the status chip when the link is down', () => {
    const {container} = mount(<MachineReadings view={{_tag: 'Missing', why: 'disconnected'}} />);
    expect(gaugeNames(container)).toEqual([]);
    expect(chips(container)).toHaveLength(0);
  });

  it('gives a missing reading one chip, in its own words, for any why but disconnected', () => {
    fc.assert(
      fc.property(chipWhy, why => {
        const {container} = mount(<MachineReadings view={{_tag: 'Missing', why}} />);
        const drawn = chips(container);
        expect(drawn).toHaveLength(1);
        const word = machineWhyWord(why);
        const chip = drawn[0];
        if (chip === undefined) throw new Error('one chip expected');
        expect(chip.textContent).toBe(word.label);
        expect(chip.getAttribute('data-tone')).toBe(word.tone);
        expect(chip.getAttribute('aria-description')).toBe(word.hint);
        expect(gaugeNames(container)).toEqual([]);
      }),
      {numRuns: MACHINE_CHIP_WHYS.length},
    );
  });

  it('draws CPU and memory gauges, undimmed and chipless, for a current reading', () => {
    const {container} = mount(
      <MachineReadings view={{_tag: 'Shown', sample: sampleOf(), stale: null}} />,
    );
    expect(gaugeNames(container)).toEqual(['CPU', 'Memory']);
    for (const el of container.querySelectorAll('.vs-gauge')) {
      expect(el.hasAttribute('data-dim')).toBe(false);
    }
    expect(chips(container)).toHaveLength(0);
  });

  it('dims a stale reading and chips why, but never a second status for a lost link', () => {
    fc.assert(
      fc.property(chipWhy, stale => {
        const {container} = mount(
          <MachineReadings view={{_tag: 'Shown', sample: sampleOf(), stale}} />,
        );
        for (const el of container.querySelectorAll('.vs-gauge')) {
          expect(el.hasAttribute('data-dim')).toBe(true);
        }
        const drawn = chips(container);
        expect(drawn).toHaveLength(1);
        expect(drawn[0]?.textContent).toBe(machineWhyWord(stale).label);
      }),
      {numRuns: MACHINE_CHIP_WHYS.length},
    );
  });

  it('dims a stale reading that lost its link, and still draws no chip', () => {
    const {container} = mount(
      <MachineReadings view={{_tag: 'Shown', sample: sampleOf(), stale: 'disconnected'}} />,
    );
    for (const el of container.querySelectorAll('.vs-gauge')) {
      expect(el.hasAttribute('data-dim')).toBe(true);
    }
    expect(chips(container)).toHaveLength(0);
  });

  it('says "No GPUs" in words for a machine the host says has none', () => {
    const {container} = mount(
      <MachineReadings view={{_tag: 'Shown', sample: sampleOf({gpus: read([])}), stale: null}} />,
    );
    expect(container.textContent).toContain('No GPUs');
    // The empty list is the host's word, not a reading that could not be made: no GPU gauge.
    expect(gaugeNames(container)).not.toContain('GPU 0');
    expect(groups(container)).toHaveLength(0);
  });

  it('draws an unknown gauge, not "No GPUs", when the GPU section could not be read', () => {
    const {container} = mount(
      <MachineReadings
        view={{_tag: 'Shown', sample: sampleOf({gpus: absent('no-tool')}), stale: null}}
      />,
    );
    expect(container.textContent).not.toContain('No GPUs');
    const gpuGauge = [...container.querySelectorAll('.vs-gauge')].find(el =>
      (el.getAttribute('aria-label') ?? '').startsWith('GPU'),
    );
    expect(gpuGauge?.getAttribute('role')).toBe('img');
    expect(gpuGauge?.getAttribute('aria-label')).toContain('unknown');
  });

  it('draws an unknown gauge for a CPU the host could not read', () => {
    const {container} = mount(
      <MachineReadings
        view={{_tag: 'Shown', sample: sampleOf({cpuPercent: absent('timed-out')}), stale: null}}
      />,
    );
    const cpu = [...container.querySelectorAll('.vs-gauge')].find(el =>
      (el.getAttribute('aria-label') ?? '').startsWith('CPU'),
    );
    expect(cpu?.getAttribute('role')).toBe('img');
    expect(cpu?.getAttribute('aria-label')).toContain('unknown');
  });

  it('groups each GPU under its own name, one row per GPU, for any count', () => {
    const count = fc.integer({min: 0, max: 6});
    fc.assert(
      fc.property(count, n => {
        const gpus = Array.from({length: n}, (_, i) => gpu(i));
        const {container} = mount(
          <MachineReadings
            view={{_tag: 'Shown', sample: sampleOf({gpus: read(gpus)}), stale: null}}
          />,
        );
        const drawn = groups(container);
        expect(drawn).toHaveLength(n);
        drawn.forEach((group, i) => {
          const legend = group.querySelector('legend');
          expect(legend?.textContent).toBe(`GPU ${i}`);
          // Each group holds the GPU's own gauge and its memory gauge.
          const named = [...group.querySelectorAll('.vs-gauge')].map(
            el => el.getAttribute('aria-label') ?? '',
          );
          expect(named).toEqual([`GPU ${i}`, 'Memory']);
        });
      }),
      {numRuns: 20},
    );
  });
});
