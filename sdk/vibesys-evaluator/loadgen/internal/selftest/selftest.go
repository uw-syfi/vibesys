// Package selftest measures loadgen's own limits where it runs: the highest open-loop rate it
// sustains against an HTTP server that answers at once, and the latency it adds at half that rate.
// The server is in loadgen's process, so it shares loadgen's CPUs and the limits are conservative.
package selftest

import (
	"context"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"time"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/call"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/cpu"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/engine"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/files"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/messages"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/plan"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/results"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/summary"
)

// The guard a rate must pass to count as sustained.
const (
	minOfferedRatio = 0.99
	maxLagP99Micros = 1000
	startRate       = 1000
	bisections      = 4
	maxInFlight     = 512
	warmup          = 500 * time.Millisecond
)

// Options bounds the measurement.
type Options struct {
	// Step is the window of each trial rate.
	Step time.Duration
	// MaxRate caps the search, operations/s.
	MaxRate float64
}

// Run measures and returns the result.
func Run(ctx context.Context, options Options) (*pb.SelftestResult, error) {
	if options.Step <= 0 || !(options.MaxRate >= startRate) {
		return nil, fmt.Errorf("selftest needs a positive --step-secs and --max-rate of at least %d", startRate)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, err
	}
	server := &http.Server{
		Handler:           http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("ok")) }),
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() { _ = server.Serve(listener) }()
	defer func() { _ = server.Close() }()
	client, err := call.NewClient("http://"+listener.Addr().String(), call.Options{Timeout: 5 * time.Second, Conns: maxInFlight})
	if err != nil {
		return nil, err
	}
	defer client.Close()
	m := measurer{ctx: ctx, client: client, step: options.Step}

	pass, fail := 0.0, 0.0
	for rate := float64(startRate); ; rate = math.Min(rate*2, options.MaxRate) {
		ok, _, err := m.trial(rate)
		if err != nil {
			return nil, err
		}
		if !ok {
			fail = rate
			break
		}
		pass = rate
		if rate >= options.MaxRate {
			break
		}
	}
	for i := 0; fail > 0 && i < bisections; i++ {
		rate := (pass + fail) / 2
		ok, _, err := m.trial(rate)
		if err != nil {
			return nil, err
		}
		if ok {
			pass = rate
		} else {
			fail = rate
		}
	}
	if pass == 0 {
		return nil, fmt.Errorf("loadgen did not sustain any rate down to %.0f operations/s here", fail)
	}
	half := pass / 2
	_, measured, err := m.trial(half)
	if err != nil {
		return nil, err
	}
	return &pb.SelftestResult{
		MaxRate:            pass,
		AddedLatency:       measured.GetLatency(),
		LoadgenCpuFraction: measured.GetLoadgenCpuFraction(),
		Cpus:               cpu.Quota(),
		AddedLatencyRate:   half,
	}, nil
}

type measurer struct {
	ctx    context.Context
	client *call.Client
	step   time.Duration
}

// trial runs one open loop at rate and reports whether it passed the guard.
func (m measurer) trial(rate float64) (bool, *pb.PhaseSummary, error) {
	count := int(math.Ceil(rate*(warmup+m.step).Seconds()*1.02)) + 100
	p, err := nullPlan(count)
	if err != nil {
		return false, nil, err
	}
	sink, err := results.New(io.Discard, files.Binpb, results.Options{Writer: "selftest"})
	if err != nil {
		return false, nil, err
	}
	outcome, err := engine.Run(m.ctx, engine.Config{
		Load:    engine.Open{Rate: rate, Arrivals: pb.Arrivals_ARRIVALS_UNIFORM, MaxInFlight: maxInFlight, MinSustained: minOfferedRatio},
		Warmup:  warmup,
		Window:  m.step,
		Timeout: 5 * time.Second,
	}, p, m.client, sink)
	spilled, closeErr := sink.Close()
	if err != nil {
		return false, nil, err
	}
	if closeErr != nil {
		return false, nil, closeErr
	}
	outcome.Phase.ResultsSpilled = spilled
	phase, err := outcome.Aggregator.Summary(outcome.Phase)
	if err != nil {
		return false, nil, err
	}
	lag, err := summary.Percentile(phase.GetSendLag(), 99)
	if err != nil {
		return false, nil, err
	}
	ok := phase.GetSustained() && lag <= maxLagP99Micros && phase.GetErrors() == 0 && !phase.GetExhausted()
	return ok, phase, nil
}

func nullPlan(count int) (*plan.Plan, error) {
	op := messages.Op(0, messages.Get("/"))
	ops := make([]*pb.PlannedOperation, count)
	for i := range ops {
		ops[i] = op
	}
	return plan.New([]string{"null"}, ops)
}
