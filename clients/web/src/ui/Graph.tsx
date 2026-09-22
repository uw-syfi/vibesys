import type {AgentPhaseStatus} from '@vibesys/core-state';
import {useCallback, useLayoutEffect, useRef, useState} from 'react';
import {titleCase} from '../derive.js';
import type {GraphColumn, GraphNode} from '../model.js';
import './Graph.css';

export interface GraphProps {
  round: number | null;
  columns: GraphColumn[];
}

/** The edge the row has more columns past, which the fade marks; undefined when it all fits. */
type More = 'start' | 'end' | 'both' | undefined;

/** The live agent is "Running", the word the rail's tooltip and the live region already use. */
function statusWord(status: AgentPhaseStatus): string {
  return status === 'active' ? 'Running' : titleCase(status);
}

/**
 * The selected round's agent pipeline, above the log and outside its scroller: a column per agent
 * kind in loop order, the agents of a kind stacked inside it, and an arrow to the next kind toned
 * by how that handover went. Adjacent agents that say the same thing arrive as one card with a
 * count. The arrows and the overflow fade are drawn in CSS, so they stay out of the accessibility
 * tree; the nodes are a status display and take no focus.
 */
export function Graph({round, columns}: GraphProps) {
  const row = useRef<HTMLOListElement | null>(null);
  const [more, setMore] = useState<More>(undefined);

  const measure = useCallback(() => {
    const node = row.current;
    if (node === null) return;
    const room = node.scrollWidth - node.clientWidth;
    const at = node.scrollLeft;
    setMore(room <= 1 ? undefined : at <= 1 ? 'end' : at >= room - 1 ? 'start' : 'both');
  }, []);

  // A callback ref, not an effect: the row exists only once a round has agents, which is later
  // than the first commit, and an effect with stable deps runs before that and never again.
  const attach = useCallback(
    (node: HTMLOListElement | null) => {
      row.current = node;
      if (node === null) return;
      const observer = new ResizeObserver(measure);
      observer.observe(node);
      return () => {
        observer.disconnect();
        row.current = null;
      };
    },
    [measure],
  );

  // scrollWidth follows the column and card counts, and neither changes the row's own size, so
  // the observer never fires for them. A new `columns` is the only other thing that can.
  useLayoutEffect(() => {
    if (columns.length > 0) measure();
  }, [columns, measure]);

  if (round === null || columns.length === 0) return null;
  return (
    <section className="graph">
      <div className="col">
        <ol
          ref={attach}
          className="gflow"
          data-more={more}
          aria-label={`Round ${round} agents`}
          // A tab stop only while it scrolls: not every browser focuses a scroll container on
          // its own, and a row that fits has nothing for the keyboard to do.
          tabIndex={more === undefined ? -1 : 0}
          onScroll={measure}
        >
          {columns.map(column => (
            <li key={column.kind} className="gcol" data-edge={column.edge ?? undefined}>
              <ol className="gstack">
                {column.nodes.map(node => (
                  <li
                    key={node.id}
                    className="gnode"
                    data-status={node.status}
                    aria-current={node.status === 'active' ? 'step' : undefined}
                  >
                    <span className="grole trunc">
                      {node.role}
                      {node.count > 1 ? (
                        <>
                          {/* U+00D7 is punctuation: a screen reader may drop it or say "times",
                              so the count is a word of its own for them. */}
                          <span className="sr-only">{` ${node.count} runs`}</span>
                          <span className="gn" aria-hidden="true">{` ×${node.count}`}</span>
                        </>
                      ) : null}
                    </span>
                    <span className="gstat">{statusWord(node.status)}</span>
                    <Runtime node={node} />
                  </li>
                ))}
              </ol>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}

/**
 * The model the agent ran on, which is the half that varies between agents. When a harness is
 * recorded too the card has no room for it, so the pair goes to the tooltip and to the name a
 * screen reader announces.
 */
function Runtime({node}: {node: GraphNode}) {
  if (node.runtime === null) return null;
  if (node.runtimeTip === null) return <span className="grun mono trunc">{node.runtime}</span>;
  return (
    <>
      <span className="sr-only">{node.runtimeTip}</span>
      <span className="grun mono trunc" data-tip={node.runtimeTip} aria-hidden="true">
        {node.runtime}
      </span>
    </>
  );
}
