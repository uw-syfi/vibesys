import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {labelFromName, VIEWER_ID_KEY, VIEWER_LABEL_KEY, viewerMetadata} from './viewer.js';

/** A user name as an operating system can have one: any text, accents, symbols and controls included. */
const names = fc.oneof(fc.string({unit: 'binary', maxLength: 300}), fc.string({maxLength: 300}));

describe('a label made from a name', () => {
  it('is always one a header can carry and the contract accepts: printable ASCII, 1 to 128 characters', () => {
    fc.assert(
      fc.property(names, name => {
        expect(labelFromName(name)).toMatch(/^[!-~]([ -~]{0,126}[!-~])?$/);
      }),
      {numRuns: 300},
    );
  });

  it('is the name itself when it already is printable ASCII, as long as it fits', () => {
    fc.assert(
      fc.property(fc.stringMatching(/^[!-~]([ -~]{0,100}[!-~])?$/), name => {
        expect(labelFromName(name)).toBe(name);
      }),
    );
  });

  it('keeps the printable part of a name and marks each character it cannot carry', () => {
    expect(labelFromName('Renée')).toBe('Ren?e');
    expect(labelFromName('  ana\t')).toBe('ana?');
    expect(labelFromName('é')).toBe('?');
  });

  it('is never empty, even for a name with nothing in it', () => {
    expect(labelFromName('')).toBe('this install');
    expect(labelFromName('   ')).toBe('this install');
  });
});

describe('the metadata that says who is calling', () => {
  it('is both fields, with the id and the label as they are, and nothing else', () => {
    expect(viewerMetadata({id: 'install-1', label: 'ana'})).toEqual({
      [VIEWER_ID_KEY]: 'install-1',
      [VIEWER_LABEL_KEY]: 'ana',
    });
    expect(VIEWER_ID_KEY).toBe('vibesys-viewer-id');
    expect(VIEWER_LABEL_KEY).toBe('vibesys-viewer-label');
  });
});
