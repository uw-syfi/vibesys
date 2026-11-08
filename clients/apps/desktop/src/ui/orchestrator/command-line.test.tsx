import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {CommandSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {renderToStaticMarkup} from 'react-dom/server';
import {type CommandProgress, FAULT_KINDS, type Fault} from '../../state/index.js';
import {CommandLine} from './command-line.js';

const command = create(CommandSchema, {id: 'c1'});

/** The markup's visible text, without the attributes meant for a hover or a reader. */
const visibleText = (html: string): string =>
  html
    .replace(/<[^>]*>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();

const render = (progress: CommandProgress): string =>
  renderToStaticMarkup(<CommandLine progress={progress} done="Start" retry={() => undefined} />);

describe('a command outcome line', () => {
  it('shows a fault detail the host sent, under the label', () => {
    const html = render({
      _tag: 'Failed',
      command,
      fault: {kind: 'Unavailable', detail: 'the host is not answering', retryable: false},
    });
    expect(visibleText(html)).toContain('Details: the host is not answering');
  });

  it('never writes a bare "Details:" when the fault carries no detail', () => {
    fc.assert(
      fc.property(
        fc.constantFrom<CommandProgress['_tag'] & ('Failed' | 'Unconfirmed')>(
          'Failed',
          'Unconfirmed',
        ),
        fc.constantFrom(...FAULT_KINDS),
        fc.boolean(),
        (tag, kind, retryable) => {
          const fault: Fault = {kind, detail: '', retryable};
          const html = render({_tag: tag, command, fault} as CommandProgress);
          expect(visibleText(html)).not.toContain('Details:');
        },
      ),
      {seed: 51},
    );
  });
});
