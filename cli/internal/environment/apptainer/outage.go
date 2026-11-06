package apptainer

import (
	"context"
	"errors"
	"fmt"
	"io"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
)

// DefaultUnreachableTimeout bounds how long a wait of the engine on the cluster (an allocation in
// the queue, a kept allocation's state, the step that holds an allocation, the reading of its node)
// goes on while every call to the cluster loses its connection, when Config sets no bound. The
// queue's own bound still applies.
const DefaultUnreachableTimeout = 30 * time.Minute

// outage follows the cluster's unreachability through the calls of one wait. A call whose
// connection to the cluster was lost (ErrConnectionLost) is made again after the poll pause until
// the cluster has been unreachable for longer than bound, counted from the first loss in a row: an
// ssh connection that the login node resets, or a login node down for minutes, is no reason to give
// up a place that took hours to reach in Slurm's queue. Its start and its end go to log once each.
// Any other failure is the call's own, and passes.
type outage struct {
	e     *Engine
	log   io.Writer
	what  string        // the wait, for a person: "the wait for the allocation 1001"
	bound time.Duration // how long the cluster may stay unreachable
	since time.Time     // when the first call of this outage was lost; zero while the cluster answers
	fired <-chan struct{}
	stop  func()
}

// outage starts following the cluster's reachability for the wait named by what, said to log, which
// the cluster may fail to answer for up to bound. Call close when the wait ends.
func (e *Engine) outage(log io.Writer, what string, bound time.Duration) *outage {
	return &outage{e: e, log: log, what: what, bound: bound, stop: func() {}}
}

// seen takes the error of a call of the wait, and reports whether the call was lost and is to be
// made again after the poll pause. It returns the error when the call failed otherwise, the
// context's when the wait was cancelled, and a failure saying so, with the last loss, when the
// cluster has been unreachable for longer than the bound.
func (o *outage) seen(ctx context.Context, err error) (bool, error) {
	if err != nil && ctx.Err() != nil {
		// The wait was cancelled: that is its ending, whatever the call said.
		return false, ctx.Err()
	}
	if err == nil || !errors.Is(err, ErrConnectionLost) {
		if err == nil && !o.since.IsZero() {
			_, _ = fmt.Fprintf(o.log, "[apptainer] the cluster answers again, after %v unreachable; %s goes on\n",
				o.e.cfg.Clock.Now().Sub(o.since).Round(time.Second), o.what)
			o.since = time.Time{}
			o.stop()
			o.fired, o.stop = nil, func() {}
		}
		return false, err
	}
	if o.since.IsZero() {
		o.since = o.e.cfg.Clock.Now()
		o.fired, o.stop = o.e.cfg.Timers.Timeout(ctx, o.bound)
		_, _ = fmt.Fprintf(o.log, "[apptainer] the cluster is unreachable since %s (%v); still waiting: %s goes on, "+
			"for up to %v while the cluster stays unreachable\n", o.since.Format(time.RFC3339), err, o.what, o.bound)
	}
	select {
	case <-o.fired:
		return false, unavailable("the cluster was unreachable from %s for longer than %v, and %s was given up; the last failure: %v",
			o.since.Format(time.RFC3339), o.bound, o.what, err)
	default:
		return true, nil
	}
}

// close releases the outage's bound.
func (o *outage) close() { o.stop() }

// patiently makes call until it answers, again after each poll pause while it loses its connection
// to the cluster, for up to bound of the cluster unreachable (outage). Each call is one that may be
// made again: a read, or a cancel by name.
func (e *Engine) patiently(ctx context.Context, log io.Writer, what string, bound time.Duration, call func(context.Context) error) error {
	o := e.outage(log, what, bound)
	defer o.close()
	for {
		again, err := o.seen(ctx, call(ctx))
		if !again {
			return err
		}
		if !clock.Sleep(ctx, e.cfg.Timers, e.cfg.Poll) {
			return ctx.Err()
		}
	}
}
