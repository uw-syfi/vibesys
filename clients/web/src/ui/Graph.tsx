import {titleCase} from '../derive.js';
import type {GraphColumn} from '../model.js';
import './Graph.css';

export interface GraphProps {
  round: number | null;
  columns: GraphColumn[];
}

/**
 * The selected round's agent pipeline, above the log and outside its scroller: a column per agent
 * kind in loop order, the agents of a kind stacked inside it, and an arrow to the next kind toned
 * by how that handover went. The arrows are drawn in CSS, so they stay out of the accessibility
 * tree; the nodes are a status display and take no focus.
 */
export function Graph({round, columns}: GraphProps) {
  if (round === null || columns.length === 0) return null;
  return (
    <section className="graph">
      <div className="col">
        <ol
          className="gflow"
          aria-label={`Round ${round} agents`}
          // biome-ignore lint/a11y/noNoninteractiveTabindex: the row scrolls sideways, and not every browser focuses a scroll container on its own.
          tabIndex={0}
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
                    <span className="grole">{node.role}</span>
                    <span className="gstat">{titleCase(node.status)}</span>
                    {node.runtime === null ? null : (
                      <span className="grun mono">{node.runtime}</span>
                    )}
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
