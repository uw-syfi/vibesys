// Package engine runs one phase: it sends a prefix of the plan in plan order, closed loop (a fixed
// number of clients) or open loop (operations at arrival times, whatever the responses), grants
// exclusive keys in plan order, and hands every result to a sink and to the summary. Nothing is
// read or validated here; the plan arrives validated and whole, and a sender compiles each
// operation from its bytes as it takes it (before it is due, in the open loop).
package engine

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"time"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/call"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/cpu"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/plan"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/summary"
)

// Load is how a phase offers load: Closed or Open.
type Load interface{ load() }

// Closed is a closed loop of Clients clients.
type Closed struct {
	Clients int
}

// Open is an open loop at Rate operations/s.
type Open struct {
	Rate     float64
	Arrivals pb.Arrivals
	// Seed seeds the Poisson arrival times.
	Seed uint64
	// MaxInFlight bounds concurrent operations; beyond it operations wait (send lag).
	MaxInFlight int
	// MinSustained is the fraction of Rate the offered rate must reach to be sustained.
	MinSustained float64
}

func (Closed) load() {}
func (Open) load()   {}

// Config is one phase.
type Config struct {
	Load    Load
	Warmup  time.Duration
	Window  time.Duration
	Timeout time.Duration
}

// Validate checks a configuration.
func (c Config) Validate() error {
	switch load := c.Load.(type) {
	case Closed:
		if load.Clients < 1 {
			return errors.New("--clients must be at least 1")
		}
	case Open:
		if !(load.Rate > 0) {
			return errors.New("--rate must be positive")
		}
		if load.MaxInFlight < 1 {
			return errors.New("--max-in-flight must be at least 1")
		}
		if load.Arrivals != pb.Arrivals_ARRIVALS_UNIFORM && load.Arrivals != pb.Arrivals_ARRIVALS_POISSON {
			return errors.New("--arrivals must be uniform or poisson")
		}
		if load.MinSustained < 0 || load.MinSustained > 1 {
			return errors.New("--min-sustained must be in [0, 1]")
		}
	default:
		return errors.New("no load model")
	}
	if c.Window <= 0 {
		return errors.New("--secs must be positive")
	}
	if c.Warmup < 0 {
		return errors.New("--warmup must not be negative")
	}
	if c.Timeout <= 0 {
		return errors.New("--timeout must be positive")
	}
	return nil
}

// Sender sends one operation; call.Client is the implementation.
type Sender interface {
	Do(ctx context.Context, op *plan.Op) call.Outcome
}

// Sink receives every result; results.Writer is the implementation. Put must not block.
type Sink interface {
	Put(result *pb.OperationResult)
}

// Outcome is a phase that ran: its aggregated results and what the summary needs besides.
type Outcome struct {
	Aggregator *summary.Aggregator
	Phase      summary.Phase
}

// lead is the time between the start of Run and t0, for the senders to be ready.
const lead = 20 * time.Millisecond

// Run runs one phase. It returns once every operation it took has finished, at most the request
// timeout after the window ends; ctx cancels it early.
func Run(ctx context.Context, config Config, p *plan.Plan, sender Sender, sink Sink) (Outcome, error) {
	if err := config.Validate(); err != nil {
		return Outcome{}, err
	}
	t0 := time.Now().Add(lead)
	window := summary.Window{Start: t0.Add(config.Warmup).UnixNano(), End: t0.Add(config.Warmup + config.Window).UnixNano()}
	deadline := time.Unix(0, window.End).Add(config.Timeout)
	phaseCtx, cancel := context.WithDeadline(ctx, deadline)
	defer cancel()
	r := &runner{
		ctx:    phaseCtx,
		plan:   p,
		sender: sender,
		sink:   sink,
		window: window,
		agg:    summary.New(window, p.Names),
		gates:  make([]chan struct{}, p.Len()),
	}
	for i := range p.Len() {
		if p.Gated(i) {
			r.gates[i] = make(chan struct{})
		}
	}
	meter := startMeter(window)
	defer meter.stop()
	phase := summary.Phase{}
	switch load := config.Load.(type) {
	case Closed:
		phase.Model = pb.LoadModel_LOAD_MODEL_CLOSED
		phase.Clients = uint32(load.Clients)
		phase.Exhausted, phase.ExhaustedAt = r.closed(t0, load.Clients)
	case Open:
		phase.Model = pb.LoadModel_LOAD_MODEL_OPEN
		phase.TargetRate = load.Rate
		phase.MinSustained = load.MinSustained
		phase.Exhausted, phase.Saturated = r.open(t0, load)
	}
	if err := ctx.Err(); err != nil {
		return Outcome{}, err
	}
	phase.CPUFraction = meter.fraction()
	return Outcome{Aggregator: r.agg, Phase: phase}, nil
}

type runner struct {
	ctx    context.Context
	plan   *plan.Plan
	sender Sender
	sink   Sink
	window summary.Window
	gates  []chan struct{}

	mu  sync.Mutex
	agg *summary.Aggregator
}

// closed runs a closed loop and reports whether the plan ran out before the window ended and, if
// it did, when the loop stopped: the end of its last operation, since every client returns once
// its operation is done and it finds the plan empty.
func (r *runner) closed(t0 time.Time, clients int) (bool, int64) {
	var next atomic.Uint64
	var exhausted atomic.Bool
	end := time.Unix(0, r.window.End)
	count := r.plan.Len()
	var wg sync.WaitGroup
	start := make(chan struct{})
	for range clients {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			for r.ctx.Err() == nil {
				now := time.Now()
				if !now.Before(end) {
					return
				}
				index := next.Add(1) - 1
				if index >= count {
					exhausted.Store(true)
					return
				}
				// Taking an operation includes compiling it: it is scheduled once ready to send.
				op := r.plan.Op(index)
				taken := time.Now()
				r.run(op, taken, taken)
			}
		}()
	}
	sleepUntil(r.ctx, t0)
	close(start)
	wg.Wait()
	if !exhausted.Load() {
		return false, 0
	}
	return true, time.Now().UnixNano()
}

type job struct {
	index     uint64
	scheduled time.Time
}

// open runs an open loop and reports whether the plan ran out before the window ended, and how
// many of the window's operations were due while every sender was busy (saturated).
func (r *runner) open(t0 time.Time, load Open) (bool, uint64) {
	var saturated uint64
	arrivals := NewArrivals(load.Arrivals, load.Rate, load.Seed)
	work := make(chan job)
	var wg sync.WaitGroup
	for range load.MaxInFlight {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for item := range work {
				op := r.plan.Op(item.index) // in the time the operation is handed over early
				waitUntil(r.ctx, item.scheduled)
				r.run(op, item.scheduled, time.Now())
			}
		}()
	}
	defer wg.Wait()
	defer close(work)
	end := time.Unix(0, r.window.End)
	count := r.plan.Len()
	for index := range count {
		scheduled := t0.Add(arrivals())
		if !scheduled.Before(end) {
			return false, saturated
		}
		// Hand the operation to a sender a little early; the sender waits for the exact time.
		sleepUntil(r.ctx, scheduled.Add(-ahead))
		item := job{index: index, scheduled: scheduled}
		select {
		case work <- item:
			continue
		default:
		}
		// Every sender is busy with an operation in flight: this one waits for the first to finish.
		if r.window.Contains(scheduled.UnixNano()) {
			saturated++
		}
		select {
		case work <- item:
		case <-r.ctx.Done():
			// The phase's deadline passed with operations still due: they were never sent.
			r.abandon(index, scheduled, arrivals, t0, end)
			return false, saturated
		}
	}
	return t0.Add(arrivals()).Before(end), saturated
}

// abandon records the operations due in the window from index on as never sent.
func (r *runner) abandon(index uint64, scheduled time.Time, arrivals func() time.Duration, t0, end time.Time) {
	now := time.Now().UnixNano()
	for ; index < r.plan.Len() && scheduled.Before(end); index++ {
		r.finish(&pb.OperationResult{
			Index:              index,
			Operation:          r.plan.Operation(index),
			ScheduledUnixNanos: scheduled.UnixNano(),
			EndUnixNanos:       now,
			Error:              pb.ErrorKind_ERROR_KIND_CANCELLED,
		}, 0)
		scheduled = t0.Add(arrivals())
	}
}

// run sends one operation taken at picked (scheduled is when it was due).
func (r *runner) run(op *plan.Op, scheduled, picked time.Time) {
	result := &pb.OperationResult{Index: op.Index, Operation: op.Operation, ScheduledUnixNanos: scheduled.UnixNano()}
	if op.After >= 0 {
		select {
		case <-r.gates[op.After]:
		case <-r.ctx.Done():
			result.EndUnixNanos = time.Now().UnixNano()
			result.Error = pb.ErrorKind_ERROR_KIND_CANCELLED
			r.release(op)
			r.finish(result, 0)
			return
		}
	}
	outcome := r.sender.Do(r.ctx, op)
	r.release(op)
	result.StartUnixNanos = outcome.Start.UnixNano()
	result.EndUnixNanos = outcome.End.UnixNano()
	result.Steps = outcome.Steps
	result.Error = outcome.Error
	r.finish(result, picked.UnixNano())
}

func (r *runner) release(op *plan.Op) {
	if op.Gated {
		close(r.gates[op.Index])
	}
}

func (r *runner) finish(result *pb.OperationResult, picked int64) {
	r.sink.Put(result)
	r.mu.Lock()
	r.agg.Add(summary.Record{Result: result, Picked: picked})
	r.mu.Unlock()
}

// meter measures loadgen's CPU over the window (or the part of it the phase ran).
type meter struct {
	mu                 sync.Mutex
	startCPU, endCPU   time.Duration
	startWall, endWall time.Time
	started, ended     bool
	timers             []*time.Timer
	quota              float64
}

func startMeter(window summary.Window) *meter {
	m := &meter{quota: cpu.Quota()}
	m.timers = append(m.timers,
		time.AfterFunc(time.Until(time.Unix(0, window.Start)), func() { m.sample(false) }),
		time.AfterFunc(time.Until(time.Unix(0, window.End)), func() { m.sample(true) }),
	)
	return m
}

func (m *meter) sample(end bool) {
	used, now := cpu.Used(), time.Now()
	m.mu.Lock()
	defer m.mu.Unlock()
	if end {
		if m.started && !m.ended {
			m.endCPU, m.endWall, m.ended = used, now, true
		}
		return
	}
	m.startCPU, m.startWall, m.started = used, now, true
}

func (m *meter) fraction() float64 {
	m.sample(true)
	m.mu.Lock()
	defer m.mu.Unlock()
	wall := m.endWall.Sub(m.startWall)
	if !m.started || wall <= 0 || m.quota <= 0 {
		return 0
	}
	return float64(m.endCPU-m.startCPU) / float64(wall) / m.quota
}

func (m *meter) stop() {
	for _, t := range m.timers {
		t.Stop()
	}
}
