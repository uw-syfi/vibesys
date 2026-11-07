import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {renderToStaticMarkup} from 'react-dom/server';
import {shortCommit, withShortCommits} from './commit-text.js';

const hex40 = fc.stringMatching(/^[0-9a-f]{40}$/);

describe('commit ids in text', () => {
  it('are cut to the short form, with the whole id on hover, wherever they stand in the words', () => {
    fc.assert(
      fc.property(hex40, fc.string({maxLength: 12}), (commit, around) => {
        const text = `${around} landed ${commit}, then ${commit}.`;
        const html = renderToStaticMarkup(withShortCommits(text));
        expect(html).not.toContain(`>${commit}<`);
        expect(html.split(`title="${commit}"`)).toHaveLength(3);
        expect(html).toContain(shortCommit(commit));
      }),
      {seed: 71, numRuns: 30},
    );
  });

  it('leave text without one as it is', () => {
    expect(renderToStaticMarkup(withShortCommits('Fused dispatch landed'))).toBe(
      'Fused dispatch landed',
    );
  });
});
