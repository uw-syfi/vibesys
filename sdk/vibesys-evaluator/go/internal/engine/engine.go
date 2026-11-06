// Package engine runs one phase: it takes operations from a Source in stream order and sends
// them, closed loop (a fixed number of clients) or open loop (operations at arrival times,
// whatever the responses), grants exclusive keys in stream order, and hands every result to a
// sink and to the summary. Nothing is read, parsed or validated here; operations arrive
// compiled, and the time a sender waits for one is recorded as the generator's.
package engine

import (
	"context"
	"errors"
	"sync"
	"time"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/call"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/cpu"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/plan"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/summary"
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
	// RecordClients sets OperationResult.client: the closed loop's client, or the open loop's
	// in-flight slot, that ran the operation.
	RecordClients bool
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

// Source hands out operations in stream order; plan.Stream is the implementation (plan.Repeat
// the selftest's). Take returns the next one and how long the caller waited for it; an error
// ends the phase.
type Source interface {
	Names() []string
	Take(ctx context.Context) (*plan.Op, time.Duration, error)
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
// timeout after the window ends; ctx cancels it early, and a Source error ends it with that error.
func Run(ctx context.Context, config Config, source Source, sender Sender, sink Sink) (Outcome, error) {
	if err := config.Validate(); err != nil {
		return Outcome{}, err
	}
	t0 := time.Now().Add(lead)
	window := summary.Window{Start: t0.Add(config.Warmup).UnixNano(), End: t0.Add(config.Warmup + config.Window).UnixNano()}
	deadline := time.Unix(0, window.End).Add(config.Timeout)
	phaseCtx, cancel := context.WithDeadline(ctx, deadline)
	defer cancel()
	failCtx, fail := context.WithCancelCause(phaseCtx)
	defer fail(nil)
	r := &runner{
		ctx:     failCtx,
		fail:    fail,
		source:  source,
		sender:  sender,
		sink:    sink,
		window:  window,
		clients: config.RecordClients,
		agg:     summary.New(window, source.Names()),
	}
	meter := startMeter(window)
	defer meter.stop()
	phase := summary.Phase{}
	switch load := config.Load.(type) {
	case Closed:
		phase.Model = pb.LoadModel_LOAD_MODEL_CLOSED
		phase.Clients = uint32(load.Clients)
		r.closed(t0, load.Clients)
	case Open:
		phase.Model = pb.LoadModel_LOAD_MODEL_OPEN
		phase.TargetRate = load.Rate
		phase.MinSustained = load.MinSustained
		phase.Saturated = r.open(t0, load)
	}
	if err := ctx.Err(); err != nil {
		return Outcome{}, err
	}
	if err := context.Cause(failCtx); err != nil && !errors.Is(err, context.DeadlineExceeded) && !errors.Is(err, context.Canceled) {
		return Outcome{}, err
	}
	phase.CPUFraction = meter.fraction()
	return Outcome{Aggregator: r.agg, Phase: phase}, nil
}

type runner struct {
	ctx     context.Context
	fail    context.CancelCauseFunc
	source  Source
	sender  Sender
	sink    Sink
	window  summary.Window
	clients bool

	mu  sync.Mutex
	agg *summary.Aggregator
}

// take takes the next operation within ctx; a Source error ends the phase (r.ctx's cause).
func (r *runner) take(ctx context.Context) (*plan.Op, time.Duration, bool) {
	op, waited, err := r.source.Take(ctx)
	if err != nil {
		if ctx.Err() == nil {
			r.fail(err)
		}
		return nil, waited, false
	}
	return op, waited, true
}

// closed runs a closed loop: each client takes the next operation as soon as its previous one
// finished, until the window ends.
func (r *runner) closed(t0 time.Time, clients int) {
	end := time.Unix(0, r.window.End)
	// No client takes an operation after the window: a wait for the generator ends with it.
	takeCtx, cancel := context.WithDeadline(r.ctx, end)
	defer cancel()
	var wg sync.WaitGroup
	start := make(chan struct{})
	for client := range clients {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			for r.ctx.Err() == nil {
				if !time.Now().Before(end) {
					return
				}
				op, waited, ok := r.take(takeCtx)
				if !ok {
					return
				}
				// Due when the client has it: the wait for the generator is its own, not latency.
				now := time.Now()
				if !now.Before(end) {
					r.release(op) // taken too late: never sent, not a result
					return
				}
				r.run(op, now, now, waited, uint32(client))
			}
		}()
	}
	sleepUntil(r.ctx, t0)
	close(start)
	wg.Wait()
}

type job struct {
	op        *plan.Op
	scheduled time.Time
	waited    time.Duration
}

// open runs an open loop: operations at their arrival times until the window ends. It returns how
// many of the window's operations were due while every sender was busy (saturated): their wait
// is the application holding the senders, unlike a wait for the generator, which is the take's.
func (r *runner) open(t0 time.Time, load Open) uint64 {
	var saturated uint64
	arrivals := NewArrivals(load.Arrivals, load.Rate, load.Seed)
	work := make(chan job)
	var wg sync.WaitGroup
	for slot := range load.MaxInFlight {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for item := range work {
				waitUntil(r.ctx, item.scheduled)
				r.run(item.op, item.scheduled, time.Now(), item.waited, uint32(slot))
			}
		}()
	}
	defer wg.Wait()
	defer close(work)
	end := time.Unix(0, r.window.End)
	for {
		scheduled := t0.Add(arrivals())
		if !scheduled.Before(end) {
			return saturated
		}
		// Hand the operation to a sender a little early; the sender waits for the exact time.
		sleepUntil(r.ctx, scheduled.Add(-ahead))
		op, waited, ok := r.take(r.ctx)
		if !ok {
			return saturated
		}
		item := job{op: op, scheduled: scheduled, waited: waited}
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
			r.abandon(op, scheduled, arrivals, t0, end)
			return saturated
		}
	}
}

// abandonWait bounds how long the abandoned operations wait for the generator.
const abandonWait = time.Second

// abandon records op and the operations due after it in the window as never sent, as far as the
// generator has them ready.
func (r *runner) abandon(op *plan.Op, scheduled time.Time, arrivals func() time.Duration, t0, end time.Time) {
	now := time.Now().UnixNano()
	ctx, cancel := context.WithTimeout(context.Background(), abandonWait)
	defer cancel()
	for scheduled.Before(end) {
		r.release(op)
		r.finish(&pb.OperationResult{
			Index:              op.Index,
			Operation:          op.Operation,
			ScheduledUnixNanos: scheduled.UnixNano(),
			EndUnixNanos:       now,
			Error:              pb.ErrorKind_ERROR_KIND_CANCELLED,
		}, 0, 0)
		scheduled = t0.Add(arrivals())
		var err error
		if op, _, err = r.source.Take(ctx); err != nil {
			return
		}
	}
}

// run sends one operation taken at picked by client (scheduled is when it was due, waited how
// long the sender waited for the generator).
func (r *runner) run(op *plan.Op, scheduled, picked time.Time, waited time.Duration, client uint32) {
	result := &pb.OperationResult{Index: op.Index, Operation: op.Operation, ScheduledUnixNanos: scheduled.UnixNano()}
	if r.clients {
		result.Client = &client
	}
	if op.Prev != nil {
		select {
		case <-op.Prev:
		case <-r.ctx.Done():
			result.EndUnixNanos = time.Now().UnixNano()
			result.Error = pb.ErrorKind_ERROR_KIND_CANCELLED
			r.release(op)
			r.finish(result, 0, waited)
			return
		}
	}
	outcome := r.sender.Do(r.ctx, op)
	r.release(op)
	result.StartUnixNanos = outcome.Start.UnixNano()
	result.EndUnixNanos = outcome.End.UnixNano()
	result.Steps = outcome.Steps
	result.Error = outcome.Error
	r.finish(result, picked.UnixNano(), waited)
}

func (r *runner) release(op *plan.Op) {
	if op.Done != nil {
		close(op.Done)
	}
}

func (r *runner) finish(result *pb.OperationResult, picked int64, waited time.Duration) {
	r.sink.Put(result)
	r.mu.Lock()
	r.agg.Add(summary.Record{Result: result, Picked: picked, GeneratorWait: waited})
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
