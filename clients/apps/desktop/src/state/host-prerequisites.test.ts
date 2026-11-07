import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {
  HOST_PREREQUISITES,
  missingPrerequisites,
  PREREQUISITE_SCRIPT,
  prerequisiteMessage,
} from './host-prerequisites.js';

const names = HOST_PREREQUISITES.map(({name}) => name);
const subset = fc.subarray(names);

/** What the script prints on a host that lacks `lacking`: one line per lacking item. */
const printed = (lacking: readonly string[]): string =>
  lacking.map(name => `missing ${name}\n`).join('');

describe('host prerequisites', () => {
  it('the script checks every item of the table, and prints its name when the check fails', () => {
    for (const {name} of HOST_PREREQUISITES)
      expect(PREREQUISITE_SCRIPT).toContain(`printf 'missing ${name}\\n'`);
  });

  it('reads back exactly what a host lacks, in table order, whatever else the output holds', () => {
    fc.assert(
      fc.property(subset, fc.array(fc.string({maxLength: 20})), (lacking, noise) => {
        const unrelated = noise.filter(line => !line.trim().startsWith('missing '));
        const output = [...unrelated, printed(lacking)].join('\n');
        expect(missingPrerequisites(output)).toEqual(names.filter(name => lacking.includes(name)));
      }),
    );
  });

  it('a host that lacks nothing reports nothing', () => {
    expect(missingPrerequisites('')).toEqual([]);
  });

  it('ignores a report of something the table does not hold', () => {
    expect(missingPrerequisites('missing flux-capacitor\n')).toEqual([]);
  });

  it('the refusal names every missing item', () => {
    fc.assert(
      fc.property(subset, lacking => {
        const message = prerequisiteMessage(lacking);
        for (const name of lacking) expect(message).toContain(name);
      }),
    );
  });
});
