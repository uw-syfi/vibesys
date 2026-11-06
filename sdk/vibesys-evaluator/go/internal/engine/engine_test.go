package engine_test

// These tests run real phases against an in-process HTTP server with known latency. They assert
// what the design guarantees with margins far wider than scheduling noise (a stall of hundreds of
// milliseconds against a bound of a fraction of it), never exact times.

import (
	"cmp"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"slices"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/call"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/engine"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/files"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/messages"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/plan"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/summary"
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

// generator is a request stream of GETs of /op/<index>, optionally keyed, as a generator writes
// it: endless unless limit > 0, each operation after pause.
type generator struct {
	key   func(int) string
	limit int
	pause time.Duration
	keep  bool
	// buffer is loadgen's buffer (--buffer); 256 when 0.
	buffer int
}

// source starts the generator on a pipe and opens the real stream over it.
func (g generator) source(t *testing.T) *plan.Stream {
	t.Helper()
	read, write := io.Pipe()
	go func() {
		writer, err := files.NewWriter(write, files.Binpb, files.Header(pb.FileKind_FILE_KIND_REQUESTS, "test"))
		if err == nil {
			err = writer.Write(&pb.RequestsHeader{OperationNames: []string{"even", "odd"}})
		}
		for i := 0; err == nil && (g.limit == 0 || i < g.limit); i++ {
			op := messages.Op(uint32(i%2), messages.Get("/op/"+strconv.Itoa(i)))
			if g.key != nil {
				op.ExclusiveKey = g.key(i)
			}
			op.KeepBodies = g.keep
			if err = writer.Write(op); err == nil && g.pause > 0 {
				err = writer.Flush()
				time.Sleep(g.pause)
			}
		}
		if err == nil {
			err = writer.Flush()
		}
		_ = write.CloseWithError(err)
	}()
	buffer := g.buffer
	if buffer == 0 {
		buffer = 256
	}
	stream, err := plan.OpenStream(read, buffer, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		stream.Stop()
		_ = read.Close()
		stream.Wait()
	})
	return stream
}

func indexed(t *testing.T, key func(int) string) *plan.Stream {
	return generator{key: key}.source(t)
}

func run(t *testing.T, config engine.Config, source *plan.Stream, client *call.Client) (*pb.PhaseSummary, *collect) {
	t.Helper()
	s, sink, err := tryRun(config, source, client)
	if err != nil {
		t.Fatal(err)
	}
	return s, sink
}

func tryRun(config engine.Config, source *plan.Stream, client *call.Client) (*pb.PhaseSummary, *collect, error) {
	sink := &collect{}
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute) // hang guard only
	defer cancel()
	if err := source.WaitReady(ctx); err != nil {
		return nil, nil, err
	}
	outcome, err := engine.Run(ctx, config, source, client, sink)
	if err != nil {
		return nil, nil, err
	}
	source.Stop()
	outcome.Phase.Generator = source.Stats()
	s, err := outcome.Aggregator.Summary(outcome.Phase)
	return s, sink, err
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
	s, sink := run(t, config, indexed(t, nil), client)
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

// Closed loop: N clients each send the next operation of the stream when their last one
// completed, so the stream's prefix is sent exactly once, at most N at a time, at about
// N / latency, and the stream never runs out.
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
	s, sink := run(t, config, indexed(t, nil), client)
	if !s.GetSustained() || s.GetErrors() != 0 || s.GetGenerator().GetWaited() != 0 {
		t.Fatalf("summary %v", s)
	}
	if peak.Load() > clients {
		t.Fatalf("%d operations in flight with %d clients", peak.Load(), clients)
	}
	n := len(sink.results)
	for i := range n {
		if seen[i] != 1 {
			t.Fatalf("operation %d sent %d times; the stream's prefix of %d must be sent once each", i, seen[i], n)
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
	s, _ := run(t, config, indexed(t, nil), client)
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
	s, _ := run(t, config, indexed(t, nil), client)
	if !s.GetSustained() || s.GetErrors() != 0 {
		t.Fatalf("summary %v", s)
	}
}

// Exclusive keys: operations with one key never overlap at the server and arrive in stream
// order, in both loops, while operations on other keys proceed.
func TestExclusiveKeysAreGrantedInStreamOrder(t *testing.T) {
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
		s, _ := run(t, config, indexed(t, key), client)
		mu.Lock()
		if overlap {
			t.Fatalf("%T: two operations with one key overlapped", load)
		}
		for k, indexes := range order {
			for j := 1; j < len(indexes); j++ {
				if indexes[j] != indexes[j-1]+keys {
					t.Fatalf("%T: key %s served %v: not stream order", load, k, indexes[:j+1])
				}
			}
		}
		mu.Unlock()
		if s.GetErrors() != 0 {
			t.Fatalf("%T: summary %v", load, s)
		}
	}
}

// A stream that ends while the phase runs fails it: a generator writes until loadgen stops.
func TestAStreamThatEndsFailsThePhase(t *testing.T) {
	client := serve(t, func(w http.ResponseWriter, r *http.Request) {})
	for _, load := range []engine.Load{
		engine.Closed{Clients: 2},
		engine.Open{Rate: 100, Arrivals: pb.Arrivals_ARRIVALS_UNIFORM, MaxInFlight: 8},
	} {
		config := engine.Config{Load: load, Window: 5 * time.Second, Timeout: 5 * time.Second}
		source := generator{limit: 300}.source(t)
		if _, _, err := tryRun(config, source, client); !errors.Is(err, plan.ErrStreamEnded) {
			t.Fatalf("%T: %v, want %v", load, err, plan.ErrStreamEnded)
		}
	}
}

// A generator slower than the load shows as the senders' wait for it, with the rate it managed:
// what the validity guard names. 5 ms an operation is at most 200 operations/s against a server
// that answers at once; a buffer of 8 operations hides almost none of it.
func TestASlowGeneratorIsMeasured(t *testing.T) {
	client := serve(t, func(w http.ResponseWriter, r *http.Request) {})
	config := engine.Config{Load: engine.Closed{Clients: 4}, Window: time.Second, Timeout: 5 * time.Second}
	s, _ := run(t, config, generator{pause: 5 * time.Millisecond, buffer: 8}.source(t), client)
	stats := s.GetGenerator()
	if stats.GetWaited() < s.GetCompleted()/2 || stats.GetMaxWaitMicros() < 1000 {
		t.Fatalf("waited %d of %d, max %d us", stats.GetWaited(), s.GetCompleted(), stats.GetMaxWaitMicros())
	}
	if stats.GetRate() <= 0 || stats.GetRate() > 400 || stats.GetRead() < s.GetCompleted() {
		t.Fatalf("generator %v", stats)
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
	s, sink := run(t, config, indexed(t, nil), client)
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

// An application that holds every sender (4 senders, 50 ms responses, 200 operations/s offered)
// saturates the open loop: its late operations are counted as saturated, so the send lag is
// attributed to the application. A fast application leaves the senders free and counts none, and
// neither waits for a generator that keeps up: saturation and starvation are counted apart.
func TestOpenLoopCountsOperationsDueWhileEverySenderIsBusy(t *testing.T) {
	slow := serve(t, func(w http.ResponseWriter, r *http.Request) { time.Sleep(50 * time.Millisecond) })
	config := engine.Config{
		Load:   engine.Open{Rate: 200, Arrivals: pb.Arrivals_ARRIVALS_UNIFORM, MaxInFlight: 4},
		Window: time.Second, Timeout: 5 * time.Second,
	}
	s, _ := run(t, config, generator{}.source(t), slow)
	if s.GetSaturated() < s.GetCompleted()/2 || s.GetMaxSendLagMicros() < 100_000 {
		t.Fatalf("slow application: saturated %d of %d, max send lag %d us", s.GetSaturated(), s.GetCompleted(), s.GetMaxSendLagMicros())
	}
	if s.GetGenerator().GetWaited() > s.GetCompleted()/100 {
		t.Fatalf("slow application: %d operations waited for the generator", s.GetGenerator().GetWaited())
	}
	fast := serve(t, func(w http.ResponseWriter, r *http.Request) {})
	config.Load = engine.Open{Rate: 200, Arrivals: pb.Arrivals_ARRIVALS_UNIFORM, MaxInFlight: 64}
	s, _ = run(t, config, generator{}.source(t), fast)
	if s.GetSaturated() > s.GetCompleted()/100 {
		t.Fatalf("fast application: saturated %d of %d", s.GetSaturated(), s.GetCompleted())
	}
}

// With RecordClients each sent result names its client (the closed loop's client, the open
// loop's in-flight slot), and two operations of one client never overlap; without it, none does.
func TestClientsAreRecordedWhenAsked(t *testing.T) {
	client := serve(t, func(w http.ResponseWriter, r *http.Request) { time.Sleep(5 * time.Millisecond) })
	for name, load := range map[string]struct {
		load    engine.Load
		clients uint32
	}{
		"closed": {engine.Closed{Clients: 4}, 4},
		"open":   {engine.Open{Rate: 300, Arrivals: pb.Arrivals_ARRIVALS_UNIFORM, MaxInFlight: 8, MinSustained: 0.5}, 8},
	} {
		for _, record := range []bool{true, false} {
			config := engine.Config{Load: load.load, Window: 500 * time.Millisecond, Timeout: 5 * time.Second, RecordClients: record}
			_, sink := run(t, config, indexed(t, nil), client)
			last := map[uint32]int64{}
			sink.mu.Lock()
			results := append([]*pb.OperationResult(nil), sink.results...)
			sink.mu.Unlock()
			slices.SortFunc(results, func(a, b *pb.OperationResult) int { return cmp.Compare(a.GetStartUnixNanos(), b.GetStartUnixNanos()) })
			for _, r := range results {
				if !record {
					if r.Client != nil {
						t.Fatalf("%s: client %d recorded without RecordClients", name, r.GetClient())
					}
					continue
				}
				if r.GetStartUnixNanos() == 0 {
					continue
				}
				if r.Client == nil || r.GetClient() >= load.clients {
					t.Fatalf("%s: operation %d has client %v of %d", name, r.GetIndex(), r.Client, load.clients)
				}
				if r.GetStartUnixNanos() < last[r.GetClient()] {
					t.Fatalf("%s: client %d ran operation %d before its previous one ended", name, r.GetClient(), r.GetIndex())
				}
				last[r.GetClient()] = r.GetEndUnixNanos()
			}
			if record && len(last) < 2 {
				t.Fatalf("%s: %d clients ran operations", name, len(last))
			}
		}
	}
}
