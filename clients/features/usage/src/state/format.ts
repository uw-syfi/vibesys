/** How the Usage page words a figure: dollars, shares and counts. */
import {compactTokens} from '@vibesys/run-state/usage';

const MAX_DECIMALS = 8;

/**
 * Dollars as the page writes them: cents from a dollar up, and two significant digits below a cent,
 * so a cost of a hundredth of a cent is not written as zero.
 */
export function dollars(amount: number): string {
  if (!Number.isFinite(amount) || amount <= 0) return '$0.00';
  const decimals =
    amount >= 0.01 ? 2 : Math.min(MAX_DECIMALS, Math.max(2, 1 - Math.floor(Math.log10(amount))));
  return `$${amount.toFixed(decimals)}`;
}

/** A share from 0 to 1 as a whole percent; a nonzero share under one percent is `<1%`. */
export function percent(share: number): string {
  if (!(share > 0)) return '0%';
  const whole = Math.round(share * 100);
  return whole === 0 ? '<1%' : `${whole}%`;
}

export function tokens(count: number): string {
  return compactTokens(Math.round(count));
}

export function turnsWords(count: number): string {
  return `${count} ${count === 1 ? 'turn' : 'turns'}`;
}

/** US dollars per million tokens, as a price list writes them. */
export function perMillion(rate: number): string {
  return rate === 0
    ? '$0'
    : rate < 0.01
      ? `$${rate.toPrecision(2)}`
      : `$${Number(rate.toFixed(4))}`;
}
