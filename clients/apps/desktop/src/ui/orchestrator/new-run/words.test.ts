import {describe, expect, it} from '@effect/vitest';
import {TaskDomain} from '@vibesys/orchestrator-proto';
import {domainWord} from './words.js';

const DEFINED = Object.values(TaskDomain).filter(
  (domain): domain is TaskDomain => typeof domain === 'number' && domain !== TaskDomain.UNSPECIFIED,
);

describe('what a domain is called on the New run screen', () => {
  it('has a name of its own for every domain the contract defines', () => {
    const words = DEFINED.map(domainWord);
    expect(words).not.toContain(domainWord(TaskDomain.UNSPECIFIED));
    expect(new Set(words).size).toBe(DEFINED.length);
  });

  it('names the command-line tool and kernel domains', () => {
    expect(domainWord(TaskDomain.CLI_TOOLS)).toBe('Command-line tools');
    expect(domainWord(TaskDomain.KERNEL_WRITING)).toBe('Kernels');
  });

  it('calls a domain a newer host sends, which this client does not know, Other', () => {
    expect(domainWord(99 as TaskDomain)).toBe('Other');
    expect(domainWord(TaskDomain.UNSPECIFIED)).toBe('Other');
  });
});
