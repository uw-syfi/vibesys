import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {
  completionsOf,
  type FolderEntry,
  type FolderListing,
  listingKey,
  splitPath,
} from './directory-listing.js';

const name = fc.string({
  minLength: 1,
  maxLength: 6,
  unit: fc.constantFrom('a', 'b', 'c', '.', '-'),
});
const entry: fc.Arbitrary<FolderEntry> = fc.record({
  name,
  isDirectory: fc.boolean(),
  isGitRepository: fc.boolean(),
  hasTasks: fc.boolean(),
});
const typed = fc.string({maxLength: 12, unit: fc.constantFrom('/', '~', 'a', 'b', '.', ' ')});

describe('splitting typed text at its last slash', () => {
  it('gives a folder that ends in a slash and a name with none, which together are the text', () => {
    fc.assert(
      fc.property(typed, text => {
        const split = splitPath(text);
        if (split === undefined) return;
        const whole = text === '~' ? '~/' : text;
        expect(split.folder + split.prefix).toBe(whole);
        expect(split.folder.endsWith('/')).toBe(true);
        expect(split.prefix.includes('/')).toBe(false);
        expect(split.hidden).toBe(split.prefix.startsWith('.'));
      }),
    );
  });

  it('names a folder only for an absolute path or one under the home folder', () => {
    fc.assert(
      fc.property(typed, text => {
        const rooted = text.startsWith('/') || text.startsWith('~/') || text === '~';
        expect(splitPath(text) !== undefined).toBe(rooted);
      }),
    );
    expect(splitPath('/home/dev/co')).toEqual({folder: '/home/dev/', prefix: 'co', hidden: false});
    expect(splitPath('~')).toEqual({folder: '~/', prefix: '', hidden: false});
    expect(splitPath('code/app')).toBeUndefined();
  });
});

describe('the completions of typed text', () => {
  const listed = (entries: readonly FolderEntry[]): FolderListing => ({
    _tag: 'Listed',
    entries,
    truncated: false,
  });

  it('are the listed folders whose names start with the typed name, each ending in a slash', () => {
    fc.assert(
      fc.property(fc.array(entry, {maxLength: 12}), name, (entries, prefix) => {
        const split = {folder: '/x/', prefix, hidden: prefix.startsWith('.')};
        const got = completionsOf(split, listed(entries)).map(each => each.value);
        const want = entries
          .filter(each => each.isDirectory && each.name.startsWith(prefix))
          .map(each => `/x/${each.name}/`);
        expect(got).toEqual(want);
      }),
    );
  });

  it('are none until the folder is listed, and none for a folder the host refused', () => {
    const split = {folder: '/x/', prefix: '', hidden: false};
    for (const listing of [undefined, {_tag: 'Loading'}, {_tag: 'Failed', reason: 'no'}] as const)
      expect(completionsOf(split, listing)).toEqual([]);
  });

  it('say what a folder is: tasks beat git, and a plain folder says nothing', () => {
    const split = {folder: '/x/', prefix: '', hidden: false};
    const kinds = completionsOf(
      split,
      listed([
        {name: 'a', isDirectory: true, isGitRepository: true, hasTasks: true},
        {name: 'b', isDirectory: true, isGitRepository: true, hasTasks: false},
        {name: 'c', isDirectory: true, isGitRepository: false, hasTasks: false},
      ]),
    ).map(each => each.detail);
    expect(kinds).toEqual(['tasks', 'git', undefined]);
  });

  it('keep hidden and visible listings of one folder apart', () => {
    expect(listingKey('/x/', true)).not.toBe(listingKey('/x/', false));
  });
});
