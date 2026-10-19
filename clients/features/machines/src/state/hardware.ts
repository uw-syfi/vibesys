/**
 * How the Machines page writes GPUs: one display name per model and one `8× H100` form, whichever
 * place the model came from (a machine's own report spells it `H100`, a cluster's settings
 * `nvidia_h100` or `mi300x`). The only place a raw model id becomes words.
 */

/** Vendor prefixes a scheduler puts before the model (`nvidia_h100`); the name is what follows. */
const VENDOR_PREFIX = /^(nvidia|amd|intel)[_\-\s]+/i;

/** A GPU model as people say it: `H100`, `MI300X`, `A100 80GB`; never the raw id. */
export function gpuName(model: string): string {
  return model.trim().replace(VENDOR_PREFIX, '').replaceAll('_', ' ').toUpperCase();
}

/** `count` GPUs of `models` as `8× H100`; the count is left out for one GPU. */
export function gpuLine(count: number, models: readonly string[]): string {
  const names = [...new Set(models.map(gpuName))].join(', ');
  return count > 1 && names !== '' ? `${count}× ${names}` : names;
}
