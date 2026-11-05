package engine_test

// These tests run real phases against an in-process HTTP server with known latency. They assert
// what the design guarantees with margins far wider than scheduling noise (a stall of hundreds of
// milliseconds against a bound of a fraction of it), never exact times.

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/call"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/engine"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/messages"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/plan"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/summary"
)

// collect is a Sink that keeps every result.
type collect struct {
	mu      sync.Mutex
	results []*pb.OperationResult
}

func (c *collect) Put(result *pb.OperationResult) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.results = append(c.results, result)
}

func (c *collect) byIndex() map[uint64]*pb.OperationResult {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := map[uint64]*pb.OperationResult{}
	for _, r := range c.results {
		out[r.GetIndex()] = r
	}
	return out
}

func serve(t *testing.T, handler http.HandlerFunc) *call.Client {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	client, err := call.NewClient(server.URL, call.Options{Timeout: 5 * time.Second, Conns: 64})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(client.Close)
	return client
}

// indexedPlan is count GETs of /op/<index>, optionally keyed.
func indexedPlan(t *testing.T, count int, key func(int) string) *plan.Plan {
	t.Helper()
	ops := make([]*pb.PlannedOperation, count)
	for i := range ops {
		ops[i] = messages.Op(uint32(i%2), messages.Get("/op/"+strconv.Itoa(i)))
		if key != nil {
			ops[i].ExclusiveKey = key(i)
		}
	}
	p, err := plan.New([]string{"even", "odd"}, ops)
	if err != nil {
		t.Fatal(err)
	}
	return p
}

func run(t *testing.T, config engine.Config, p *plan.Plan, client *call.Client) (*pb.PhaseSummary, *collect) {
	t.Helper()
	sink := &collect{}
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute) // hang guard only
	defer cancel()
	outcome, err := engine.Run(ctx, config, p, client, sink)
	if err != nil {
		t.Fatal(err)
	}
	s, err := outcome.Aggregator.Summary(outcome.Phase)
	if err != nil {
		t.Fatal(err)
	}
	return s, sink
}

func opIndex(r *http.Request) int {
	index, _ := strconv.Atoi(r.URL.Path[len("/op/"):])
	return index
}

func percentileMicros(t *testing.T, h *pb.HdrHistogram, p float64) int64 {
	t.Helper()
	value, err := summary.Percentile(h, p)
	if err != nil {
		t.Fatal(err)
	}
	return value
}

// Coordinated omission: when the server stalls, the open loop keeps its schedule, and operations
// queued behind the stall are measured from when they were due, not from when they were sent.
func TestOpenLoopMeasuresFromScheduledTimeWhenTheServerStalls(t *testing.T) {
	const stalled, stall = 40, 600 * time.Millisecond
	client := serve(t, func(w http.ResponseWriter, r *http.Request) {
		if opIndex(r) == stalled {
			time.Sleep(stall)
		}
	})
	config := engine.Config{
		Load:   engine.Open{Rate: 100, Arrivals: pb.Arrivals_ARRIVALS_UNIFORM, MaxInFlight: 1, MinSustained: 0.5},
		Warmup: 0, Window: 1500 * time.Millisecond, Timeout: 5 * time.Second,
	}
	s, sink := run(t, config, indexedPlan(t, 400, nil), client)
	results := sink.byIndex()
	// One sender: operation 41 (due 10 ms after 40) could start only when 40 ended.
	next := results[stalled+1]
	waited := time.Duration(next.GetStartUnixNanos() - next.GetScheduledUnixNanos())
	latency := time.Duration(next.GetEndUnixNanos() - next.GetScheduledUnixNanos())
	served := time.Duration(next.GetEndUnixNanos() - next.GetStartUnixNanos())
	if waited < stall/2 || latency < waited || served > stall/2 {
		t.Fatalf("op %d: waited %v, latency %v, served in %v", stalled+1, waited, latency, served)
	}
	// The summary's tail shows the stall: the operations due during it waited for it.
	if p99 := time.Duration(percentileMicros(t, s.GetLatency(), 99)) * time.Microsecond; p99 < stall/2 {
		t.Fatalf("p99 %v hides a %v stall", p99, stall)
	}
	if lag := time.Duration(s.GetMaxSendLagMicros()) * time.Microsecond; lag < stall/2 {
		t.Fatalf("max send lag %v", lag)
	}
	// Arrivals stayed on schedule: uniform, 10 ms apart, whatever the responses.
	for i := uint64(1); i < 100; i++ {
		if gap := results[i].GetScheduledUnixNanos() - results[i-1].GetScheduledUnixNanos(); gap != int64(10*time.Millisecond) {
			t.Fatalf("arrival gap %d at %d", gap, i)
		}
	}
}

// Closed loop: N clients each send the next operation of the plan when their last one completed,
// so the plan's prefix is sent exactly once, at most N at a time, at about N / latency.
func TestClosedLoopSendsAPrefixWithNClients(t *testing.T) {
	const clients, serverTime = 4, 20 * time.Millisecond
	var inFlight, peak atomic.Int64
	var seenMu sync.Mutex
	seen := map[int]int{}
	client := serve(t, func(w http.ResponseWriter, r *http.Request) {
		now := inFlight.Add(1)
		for old := peak.Load(); now > old && !peak.CompareAndSwap(old, now); old = peak.Load() {
		}
		seenMu.Lock()
		seen[opIndex(r)]++
		seenMu.Unlock()
		time.Sleep(serverTime)
		inFlight.Add(-1)
	})
	config := engine.Config{Load: engine.Closed{Clients: clients}, Warmup: 200 * time.Millisecond, Window: time.Second, Timeout: 5 * time.Second}
	s, sink := run(t, config, indexedPlan(t, 5000, nil), client)
	if s.GetExhausted() || !s.GetSustained() || s.GetErrors() != 0 {
		t.Fatalf("summary %v", s)
	}
	if peak.Load() > clients {
		t.Fatalf("%d operations in flight with %d clients", peak.Load(), clients)
	}
	n := len(sink.results)
	for i := range n {
		if seen[i] != 1 {
			t.Fatalf("operation %d sent %d times; the plan's prefix of %d must be sent once each", i, seen[i], n)
		}
	}
	// At most clients / serverTime = 200 ops/s; well above half of it unless the host is starved.
	if rate := s.GetAchievedRate(); rate > 200*1.05 || rate < 50 {
		t.Fatalf("achieved %v ops/s with %d clients of %v", rate, clients, serverTime)
	}
	if p50 := time.Duration(percentileMicros(t, s.GetLatency(), 50)) * time.Microsecond; p50 < serverTime {
		t.Fatalf("p50 %v below the server's %v", p50, serverTime)
	}
}

// A target slower than the offered rate makes the open loop fall behind: not sustained, with the
// send lag to show why.
func TestOpenLoopIsNotSustainedWhenTheTargetCannotKeepUp(t *testing.T) {
	client := serve(t, func(w http.ResponseWriter, r *http.Request) { time.Sleep(50 * time.Millisecond) })
	config := engine.Config{
		// Two senders of 50 ms each carry at most 40 ops/s against 200 offered.
		Load:   engine.Open{Rate: 200, Arrivals: pb.Arrivals_ARRIVALS_POISSON, Seed: 7, MaxInFlight: 2, MinSustained: 0.95},
		Warmup: 0, Window: time.Second, Timeout: 5 * time.Second,
	}
	s, _ := run(t, config, indexedPlan(t, 400, nil), client)
	if s.GetSustained() || s.GetOfferedRate() > 0.95*200 {
		t.Fatalf("sustained %v at offered %v of 200", s.GetSustained(), s.GetOfferedRate())
	}
	if s.GetMaxSendLagMicros() < 100_000 {
		t.Fatalf("max send lag %d us", s.GetMaxSendLagMicros())
	}
}

// The same target at a rate it can carry is sustained.
func TestOpenLoopIsSustainedWhenTheTargetKeepsUp(t *testing.T) {
	client := serve(t, func(w http.ResponseWriter, r *http.Request) {})
	config := engine.Config{
		Load:   engine.Open{Rate: 200, Arrivals: pb.Arrivals_ARRIVALS_POISSON, Seed: 7, MaxInFlight: 64, MinSustained: 0.8},
		Warmup: 100 * time.Millisecond, Window: time.Second, Timeout: 5 * time.Second,
	}
	s, _ := run(t, config, indexedPlan(t, 1000, nil), client)
	if !s.GetSustained() || s.GetExhausted() || s.GetErrors() != 0 {
		t.Fatalf("summary %v", s)
	}
}

// Exclusive keys: operations with one key never overlap at the server and arrive in plan order,
// in both loops, while operations on other keys proceed.
func TestExclusiveKeysAreGrantedInPlanOrder(t *testing.T) {
	const keys = 3
	key := func(i int) string { return "k" + strconv.Itoa(i%keys) }
	for _, load := range []engine.Load{
		engine.Closed{Clients: 8},
		engine.Open{Rate: 400, Arrivals: pb.Arrivals_ARRIVALS_UNIFORM, MaxInFlight: 64, MinSustained: 0},
	} {
		var mu sync.Mutex
		active := map[string]int{}
		order := map[string][]int{}
		overlap := false
		client := serve(t, func(w http.ResponseWriter, r *http.Request) {
			i := opIndex(r)
			k := key(i)
			mu.Lock()
			active[k]++
			overlap = overlap || active[k] > 1
			order[k] = append(order[k], i)
			mu.Unlock()
			time.Sleep(time.Duration(1+i%4) * time.Millisecond)
			mu.Lock()
			active[k]--
			mu.Unlock()
		})
		config := engine.Config{Load: load, Window: 500 * time.Millisecond, Timeout: 5 * time.Second}
		s, _ := run(t, config, indexedPlan(t, 2000, key), client)
		mu.Lock()
		if overlap {
			t.Fatalf("%T: two operations with one key overlapped", load)
		}
		for k, indexes := range order {
			for j := 1; j < len(indexes); j++ {
				if indexes[j] != indexes[j-1]+keys {
					t.Fatalf("%T: key %s served %v: not plan order", load, k, indexes[:j+1])
				}
			}
		}
		mu.Unlock()
		if s.GetErrors() != 0 {
			t.Fatalf("%T: summary %v", load, s)
		}
	}
}

// Running out of plan before the window ends is reported, not waited out.
func TestExhaustedPlanIsReported(t *testing.T) {
	client := serve(t, func(w http.ResponseWriter, r *http.Request) {})
	for _, load := range []engine.Load{
		engine.Closed{Clients: 2},
		engine.Open{Rate: 100, Arrivals: pb.Arrivals_ARRIVALS_UNIFORM, MaxInFlight: 8},
	} {
		config := engine.Config{Load: load, Warmup: 0, Window: time.Second, Timeout: 5 * time.Second}
		s, sink := run(t, config, indexedPlan(t, 20, nil), client)
		if !s.GetExhausted() || len(sink.results) != 20 {
			t.Fatalf("%T: exhausted %v after %d results", load, s.GetExhausted(), len(sink.results))
		}
	}
	// A plan longer than the window needs is not exhausted, and only its due prefix is sent.
	config := engine.Config{Load: engine.Open{Rate: 100, Arrivals: pb.Arrivals_ARRIVALS_UNIFORM, MaxInFlight: 8}, Window: 500 * time.Millisecond, Timeout: 5 * time.Second}
	s, sink := run(t, config, indexedPlan(t, 1000, nil), client)
	if s.GetExhausted() || len(sink.results) != 50 {
		t.Fatalf("exhausted %v with %d results, want 50", s.GetExhausted(), len(sink.results))
	}
}

// Operations still waiting for a sender when the phase's deadline passes are recorded as never
// sent (CANCELLED, start 0) and counted as errors of the window.
func TestOperationsNeverSentBeforeTheDeadlineAreCancelled(t *testing.T) {
	release := make(chan struct{})
	t.Cleanup(func() { close(release) })
	client := serve(t, func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-release:
		case <-r.Context().Done():
		}
	})
	config := engine.Config{
		Load:   engine.Open{Rate: 100, Arrivals: pb.Arrivals_ARRIVALS_UNIFORM, MaxInFlight: 1},
		Window: 300 * time.Millisecond, Timeout: 200 * time.Millisecond,
	}
	s, sink := run(t, config, indexedPlan(t, 100, nil), client)
	results := sink.byIndex()
	if len(results) != 30 || s.GetCompleted() != 30 || s.GetErrors() != 30 {
		t.Fatalf("%d results, summary %v", len(results), s)
	}
	unsent := 0
	for _, r := range results {
		if r.GetStartUnixNanos() == 0 {
			unsent++
			if r.GetError() != pb.ErrorKind_ERROR_KIND_CANCELLED || len(r.GetSteps()) != 0 {
				t.Fatalf("unsent result %v", r)
			}
		}
	}
	if unsent == 0 || s.GetSent() != uint64(30-unsent) {
		t.Fatalf("unsent %d, sent %d", unsent, s.GetSent())
	}
}

// Poisson arrivals are a function of the seed alone, with mean gap 1/rate.
func TestPoissonArrivalsAreSeeded(t *testing.T) {
	a := engine.NewArrivals(pb.Arrivals_ARRIVALS_POISSON, 1000, 42)
	b := engine.NewArrivals(pb.Arrivals_ARRIVALS_POISSON, 1000, 42)
	c := engine.NewArrivals(pb.Arrivals_ARRIVALS_POISSON, 1000, 43)
	var last time.Duration
	differs := false
	for i := range 100_000 {
		x, y, z := a(), b(), c()
		if x != y || x < last || (i == 0 && x != 0) {
			t.Fatalf("arrival %d: %v %v after %v", i, x, y, last)
		}
		differs = differs || x != z
		last = x
	}
	if mean := last.Seconds() / 100_000; mean < 0.00098 || mean > 0.00102 || !differs {
		t.Fatalf("mean gap %v s, seeds differ %v", mean, differs)
	}
}
