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

/**
 * The selected round's agent pipeline, above the log and outside its scroller: a column per agent
 * kind in loop order, the agents of a kind stacked inside it, and an arrow to the next kind toned
 * by how that handover went. Adjacent agents that say the same thing arrive as one card with a
 * count. The arrows and the overflow fade are drawn in CSS, so they stay out of the accessibility
 * tree; the nodes are a status display and take no focus.
 */
export function Graph({round, columns}: GraphProps) {
  const row = useRef<HTMLOListElement>(null);
  const [more, setMore] = useState<More>(undefined);

  const measure = useCallback(() => {
    const node = row.current;
    if (node === null) return;
    const room = node.scrollWidth - node.clientWidth;
    const at = node.scrollLeft;
    setMore(room <= 1 ? undefined : at <= 1 ? 'end' : at >= room - 1 ? 'start' : 'both');
  }, []);

  // The columns change with the round and with the events that start and finish an agent.
  useLayoutEffect(measure);
  // A width change does not: a resize inside a breakpoint, or the drawer, renders nothing at all.
  useLayoutEffect(() => {
    const node = row.current;
    if (node === null) return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [measure]);

  if (round === null || columns.length === 0) return null;
  return (
    <section className="graph">
      <div className="col">
        <ol
          ref={row}
          className="gflow"
          data-more={more}
          aria-label={`Round ${round} agents`}
          // biome-ignore lint/a11y/noNoninteractiveTabindex: the row scrolls sideways, and not every browser focuses a scroll container on its own.
          tabIndex={0}
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
                      {node.count > 1 ? <span className="gn">{` ×${node.count}`}</span> : null}
                    </span>
                    <span className="gstat">{titleCase(node.status)}</span>
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
