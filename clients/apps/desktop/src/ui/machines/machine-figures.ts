/** How a machine's readings are written in the table: short figures that keep their width. */
import type {GpuReading, MemoryUse} from '../../state/index.js';
import {formatFigure} from '../figures.js';

/** A reading the host never made: distinct from a zero, which would claim one. */
export const UNREADABLE = '–';
/** Wide enough for every percent this topic sends (0 to 100, four significant figures). */
export const PERCENT_WIDTH = 6;
/** The width of the widest memory figure (`1.797 / 2 TiB`), so every row's figures end at one edge. */
export const MEMORY_WIDTH = 14;
/** A memory reading at or past this share of its total reads as hot. */
export const HOT_SHARE = 0.9;

const GIB = 1024 ** 3;
const TIB = 1024 ** 4;

export const percentFigure = (percent: number): string => `${formatFigure(percent)}%`;

/** `usedBytes` of `totalBytes`, in GiB, or TiB once the total reaches 1000 GiB. */
export function memoryFigure(memory: MemoryUse): string {
  const inTib = memory.totalBytes >= 1000 * GIB;
  const unit = inTib ? TIB : GIB;
  return `${formatFigure(memory.usedBytes / unit)} / ${formatFigure(memory.totalBytes / unit)} ${inTib ? 'TiB' : 'GiB'}`;
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
