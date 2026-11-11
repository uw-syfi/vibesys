/** How a machine's readings are written in the table: short figures that keep their width. */
import type {GpuReading, MemoryUse} from '../../state/index.js';

/** The width of the widest percent (`100%`, `37.5%`), so every row's figures start after their bars alike. */
export const PERCENT_WIDTH = 5;
/** The width of a wide memory figure (`41.5 / 128 GiB`); a wider one only takes more room. */
export const MEMORY_WIDTH = 15;
/** A memory reading at or past this share of its total reads as hot. */
export const HOT_SHARE = 0.9;

const GIB = 1024 ** 3;
const TIB = 1024 ** 4;

/** `value` with at most one decimal, and none when it is whole: `41`, `1.8`. */
const short = (value: number): string => String(Number(value.toFixed(1)));

export const percentFigure = (percent: number): string => `${short(percent)}%`;

/** `usedBytes` of `totalBytes`, in GiB, or TiB once the total reaches 1000 GiB: one decimal at most in either. */
export function memoryFigure(memory: MemoryUse): string {
  const inTib = memory.totalBytes >= 1000 * GIB;
  const unit = inTib ? TIB : GIB;
  return `${short(memory.usedBytes / unit)} / ${short(memory.totalBytes / unit)} ${inTib ? 'TiB' : 'GiB'}`;
}

export const memoryShare = (memory: MemoryUse): number =>
  memory.totalBytes > 0 ? memory.usedBytes / memory.totalBytes : 0;

/** Each GPU on a line of its own, for a hover: its utilization and its memory. */
export const gpuLines = (gpus: readonly GpuReading[]): string =>
  gpus
    .map(
      gpu =>
        `GPU ${gpu.index}: ${percentFigure(gpu.utilizationPercent)}, ${memoryFigure(gpu.memory)}`,
    )
    .join('\n');
