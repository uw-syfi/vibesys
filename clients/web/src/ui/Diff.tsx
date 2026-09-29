import type {DiffLine} from '../model.js';

const MARK: Record<DiffLine['tone'], string> = {add: '+', del: '−', ctx: '', hunk: '', meta: ''};

/**
 * Rows of a diff: content with line number and mark. The line numbers carry position, so a hunk
 * header is only a thin break between hunks, its raw text in the hint.
 */
export function Diff({lines}: {lines: DiffLine[]}) {
  return (
    <div className="diff">
      {lines.map((line, index) =>
        line.tone === 'hunk' ? (
          // biome-ignore lint/suspicious/noArrayIndexKey: rows of one diff never reorder.
          <div key={index} className="hunk" aria-hidden title={line.text} />
        ) : (
          // biome-ignore lint/suspicious/noArrayIndexKey: rows of one diff never reorder.
          <div key={index} className={line.tone}>
            {line.tone === 'meta' ? (
              line.text
            ) : (
              <>
                <span className="ln">{line.line ?? ''}</span>
                <span className="mk">{MARK[line.tone]}</span>
                {line.text}
              </>
            )}
          </div>
        ),
      )}
    </div>
  );
}
