/** How the Usage page words a figure: dollars, shares and counts. */
import {compactTokens} from '@vibesys/run-state/usage';

const MAX_DECIMALS = 8;

const GROUPED = new Map<number, Intl.NumberFormat>();

/** `amount` with `decimals` places and its thousands grouped, the same in every locale. */
function grouped(amount: number, decimals: number): string {
  let format = GROUPED.get(decimals);
  if (format === undefined) {
    format = new Intl.NumberFormat('en-US', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
    GROUPED.set(decimals, format);
  }
  return format.format(amount);
}

/**
 * Dollars as the page writes them: cents from a dollar up, two significant digits below a cent,
 * so a cost of a hundredth of a cent is not written as zero, and thousands grouped.
 */
export function dollars(amount: number): string {
  if (!Number.isFinite(amount) || amount <= 0) return '$0.00';
  const decimals =
    amount >= 0.01 ? 2 : Math.min(MAX_DECIMALS, Math.max(2, 1 - Math.floor(Math.log10(amount))));
  return `$${grouped(amount, decimals)}`;
}

/** A whole number of dollars without its cents, for an axis; anything else as `dollars`. */
export function axisDollars(amount: number): string {
  return Number.isInteger(amount) ? `$${grouped(amount, 0)}` : dollars(amount);
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

/**
 * A name a person can read for an id nobody gave a name to: its words capitalized and its version
 * numbers joined (`claude-sonnet-5-5` is `Claude Sonnet 5.5`), without a bracketed variant or a
 * date snapshot. Used only where the machine's catalogue has no name for the id, so a screen
 * never shows the id itself.
 */
export function plainName(id: string): string {
  const words = id
    .trim()
    .replace(/\[[^\]]*\]$/, '')
    .split(/[-_\s/]+/)
    .filter(word => word !== '' && !/^\d{8}$/.test(word));
  const out: string[] = [];
  for (const word of words) {
    const last = out.at(-1);
    if (/^\d+$/.test(word) && last !== undefined && /^\d+(\.\d+)*$/.test(last)) {
      out[out.length - 1] = `${last}.${word}`;
    } else {
      out.push(/^[a-z]/.test(word) ? `${word.charAt(0).toUpperCase()}${word.slice(1)}` : word);
    }
  }
  return out.join(' ') || id;
}
