/** A metric's name in words, for text a person reads. */

/**
 * A metric id in words, for a header a person reads: `TOTAL_OPS_PER_SEC` reads `Total ops/sec`.
 * The run's objective names its metric by id and sends no label, so the words are made from the
 * id: its parts lowercased, "X per Y" joined as `X/Y`, the first letter capitalized.
 */
export function metricLabel(metric: string): string {
  const parts = metric
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .split(/[\s_-]+/)
    .filter(part => part !== '')
    .map(part => part.toLowerCase());
  const words: string[] = [];
  for (let at = 0; at < parts.length; at += 1) {
    const [word, next] = [parts[at] ?? '', parts[at + 1]];
    const previous = words.at(-1);
    if (word === 'per' && previous !== undefined && next !== undefined) {
      words[words.length - 1] = `${previous}/${next}`;
      at += 1;
    } else {
      words.push(word);
    }
  }
  const text = words.join(' ');
  return text === '' ? text : `${text.charAt(0).toUpperCase()}${text.slice(1)}`;
}

/**
 * A unit as a person reads it. An evaluator may report its metric's own key as the unit
 * (`output_tokens_per_s`); that reads as words like the metric does (`output tokens/s`). A unit
 * already written for people (`ops/s`, `ms`) is kept.
 */
export function unitLabel(unit: string): string {
  return unit.includes('_') ? metricLabel(unit).toLowerCase() : unit;
}
