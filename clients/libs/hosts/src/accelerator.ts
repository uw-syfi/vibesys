import type {MachineDescription} from '@vibesys/contracts/host-helper';

type Accelerator = MachineDescription['hardware']['accelerator'];

/**
 * How a machine's accelerator reads: the vendor's own spelling (CUDA, ROCm, Metal), never the wire
 * id (`cuda`). A record over the wire's closed set, so a new accelerator fails the type check until
 * it has a name.
 */
const NAMES: Readonly<Record<Accelerator, string>> = {
  cpu: 'CPU only',
  cuda: 'CUDA',
  rocm: 'ROCm',
  metal: 'Metal',
};

export function acceleratorName(accelerator: Accelerator): string {
  return NAMES[accelerator];
}
