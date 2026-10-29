/** The large scenario is canonical data, using the same native projection as the small ones. */
import source from './data/weeks-600-ideas.json' with {type: 'json'};
import {parseFixture} from './fixture.js';

let parsed: ReturnType<typeof parseFixture> | undefined;
export function weeksRun() {
  parsed ??= parseFixture(source);
  return parsed;
}
