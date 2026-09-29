import type {DiffLine} from '../model.js';

const MARK: Record<DiffLine['tone'], string> = {add: '+', del: '−', ctx: '', hunk: '', meta: ''};

/** Rows of a diff: hunk headers as they are, content with line number and mark. */
export function Diff({lines}: {lines: DiffLine[]}) {
  return (
    <div className="diff">
      {lines.map((line, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: rows of one diff never reorder.
        <div key={index} className={line.tone}>
          {line.tone === 'hunk' || line.tone === 'meta' ? (
            line.text
          ) : (
            <>
              <span className="ln">{line.line ?? ''}</span>
              <span className="mk">{MARK[line.tone]}</span>
              {line.text}
            </>
          )}
        </div>
      ))}
    </div>
  );
}
