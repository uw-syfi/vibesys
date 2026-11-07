/**
 * The one place the app turns an instant into words for a person. Every formatter here takes the
 * display zone as data: a `DisplayClock` is made for one IANA zone and reads no other zone, no
 * machine setting and no clock. The shell reads the system's zone once at its edge and hands it
 * in (`display-clock.tsx`); nothing else in the app names a zone or formats a time.
 */

const SECOND = 1000;
const DAY_MS = 86_400_000;

/** The clocks of one zone, by what each screen writes. Offsets are milliseconds ahead of UTC. */
export interface DisplayClock {
  readonly zone: string;
  /** How far the zone's clock is ahead of UTC at an instant (negative: behind), by the zone's rules. */
  readonly offsetMs: (atMs: number) => number;
  /** `2:40 AM`. */
  readonly time: (atMs: number) => string;
  /** `Mon 2:40 AM`, for moments that may be on different days. */
  readonly dayTime: (atMs: number) => string;
  /** `14:05`: the 24-hour time of day. */
  readonly time24: (atMs: number) => string;
  /** `Nov 2, 4:10 PM PST`: the date, the time and the zone's own name. */
  readonly datedMoment: (atMs: number) => string;
  /** `Nov 1, 17:00 PST`: the date, the 24-hour time and the zone's own name. */
  readonly datedMoment24: (atMs: number) => string;
  /**
   * The label of a tick on an axis over `spanMs`: seconds when the axis is minutes long, the time
   * when it is hours long, and the day when it is days long (a tick at midnight is the date alone).
   */
  readonly axisLabel: (atMs: number, spanMs: number) => string;
  /** `2:40 AM` with a no-break space: the same text in every runtime, never broken before AM. */
  readonly reportTime: (atMs: number) => string;
  /** `Mon 2:40 AM` with a no-break space, as the run report writes a moment. */
  readonly reportMoment: (atMs: number) => string;
  /** An axis label for a span under a day and a half: the time; else with its day. */
  readonly reportAxis: (atMs: number, spanMs: number) => string;
}

/** A formatter of `parts` in `zone`; an unknown zone fails here, naming it. */
function formatterIn(zone: string, parts: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  try {
    return new Intl.DateTimeFormat('en-US', {timeZone: zone, ...parts});
  } catch (error) {
    throw new RangeError(`unknown display time zone ${JSON.stringify(zone)}`, {cause: error});
  }
}

/**
 * The runtimes' date formatters put a narrow no-break space (U+202F) or a plain space before AM or
 * PM, depending on the version of their ICU data. One no-break space (U+00A0) in its place gives the
 * same text in every runtime.
 */
const settled = (text: string): string => text.replace(/[  ]/g, ' ');

const partsOf = (format: Intl.DateTimeFormat, atMs: number): Map<string, string> =>
  new Map(format.formatToParts(atMs).map(each => [each.type, each.value]));

/** The clocks of `zone`, an IANA name such as `Europe/London`. */
export function makeDisplayClock(zone: string): DisplayClock {
  const hourMinute = {hour: 'numeric', minute: '2-digit'} as const;
  const clock = formatterIn(zone, hourMinute);
  const clockSeconds = formatterIn(zone, {...hourMinute, second: '2-digit'});
  const dayClock = formatterIn(zone, {weekday: 'short', ...hourMinute});
  const dayDate = formatterIn(zone, {weekday: 'short', month: 'short', day: 'numeric'});
  const clock24 = formatterIn(zone, {hourCycle: 'h23', hour: '2-digit', minute: '2-digit'});
  const dated = formatterIn(zone, {
    month: 'short',
    day: 'numeric',
    ...hourMinute,
    timeZoneName: 'short',
  });
  const dated24 = formatterIn(zone, {
    month: 'short',
    day: 'numeric',
    hourCycle: 'h23',
    hour: '2-digit',
    minute: '2-digit',
    timeZoneName: 'short',
  });
  const wall = formatterIn(zone, {
    hourCycle: 'h23',
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
    hour: 'numeric',
    minute: 'numeric',
    second: 'numeric',
  });

  const offsetMs = (atMs: number): number => {
    const whole = Math.floor(atMs / SECOND) * SECOND;
    const part = partsOf(wall, whole);
    const at = (type: string): number => Number(part.get(type));
    const shown = Date.UTC(
      at('year'),
      at('month') - 1,
      at('day'),
      at('hour'),
      at('minute'),
      at('second'),
    );
    return shown - whole;
  };

  const dateOf = (atMs: number): string => {
    const part = partsOf(dayDate, atMs);
    return `${part.get('weekday')} ${part.get('month')} ${part.get('day')}`;
  };
  const reportMoment = (atMs: number): string => {
    const [day = '', ...rest] = dayClock.format(atMs).split(' ');
    return `${day} ${settled(rest.join(' '))}`;
  };

  return {
    zone,
    offsetMs,
    time: atMs => clock.format(atMs),
    dayTime: atMs => dayClock.format(atMs),
    time24: atMs => {
      const part = partsOf(clock24, atMs);
      return `${part.get('hour')}:${part.get('minute')}`;
    },
    datedMoment: atMs => dated.format(atMs),
    datedMoment24: atMs => {
      const part = partsOf(dated24, atMs);
      return `${part.get('month')} ${part.get('day')}, ${part.get('hour')}:${part.get('minute')} ${part.get('timeZoneName')}`;
    },
    axisLabel: (atMs, spanMs) => {
      if (spanMs < 600_000) return clockSeconds.format(atMs);
      if (spanMs < DAY_MS) return clock.format(atMs);
      return (atMs + offsetMs(atMs)) % DAY_MS === 0 ? dateOf(atMs) : dayClock.format(atMs);
    },
    reportTime: atMs => settled(clock.format(atMs)),
    reportMoment,
    reportAxis: (atMs, spanMs) =>
      spanMs < 1.5 * DAY_MS ? settled(clock.format(atMs)) : reportMoment(atMs),
  };
}

/** The machine's own zone. Read once, by the shell at its edge, and handed in as data. */
export const systemDisplayZone = (): string => Intl.DateTimeFormat().resolvedOptions().timeZone;
