package microservices

import (
	"fmt"
	"math"
	"time"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
)

// Arrivals is the arrival process of an open loop.
type Arrivals string

// The arrival processes.
const (
	// Poisson spaces arrivals by exponential gaps of mean 1/rate (the default).
	Poisson Arrivals = "poisson"
	// Uniform spaces arrivals 1/rate apart.
	Uniform Arrivals = "uniform"
)

// Load is how a phase offers load: Closed or Open.
type Load interface {
	// input is the load's part of the PhaseInput.
	input() (*pb.PhaseInput, error)
}

// Closed is capacity: Clients clients, each sending its next operation when its last one
// completed, for Secs after a Warmup. Latency is end minus start.
type Closed struct {
	// Clients is at least 1.
	Clients int
	// Secs is the measurement window (> 0); Warmup the load before it (>= 0).
	Secs, Warmup time.Duration
}

// Open is latency at a fixed offered rate: operations arrive on schedule, whatever the responses,
// for Secs after a Warmup. Latency is end minus the scheduled arrival.
type Open struct {
	// Rate is the operations per second offered (> 0).
	Rate float64
	// Secs is the measurement window (> 0); Warmup the load before it (>= 0).
	Secs, Warmup time.Duration
	// Arrivals is Poisson when empty.
	Arrivals Arrivals
}

func window(secs, warmup time.Duration) error {
	if secs <= 0 {
		return fmt.Errorf("secs must be > 0, got %v", secs)
	}
	if warmup < 0 {
		return fmt.Errorf("warmup must be >= 0, got %v", warmup)
	}
	return nil
}

func (c Closed) input() (*pb.PhaseInput, error) {
	if c.Clients < 1 {
		return nil, fmt.Errorf("closed loop: clients must be >= 1, got %d", c.Clients)
	}
	if err := window(c.Secs, c.Warmup); err != nil {
		return nil, fmt.Errorf("closed loop: %w", err)
	}
	return &pb.PhaseInput{
		Model: pb.LoadModel_LOAD_MODEL_CLOSED, Clients: uint32(c.Clients),
		Secs: c.Secs.Seconds(), WarmupSecs: c.Warmup.Seconds(),
	}, nil
}

func (o Open) input() (*pb.PhaseInput, error) {
	if !(o.Rate > 0) || math.IsInf(o.Rate, 0) {
		return nil, fmt.Errorf("open loop: rate must be a finite number > 0, got %v", o.Rate)
	}
	if err := window(o.Secs, o.Warmup); err != nil {
		return nil, fmt.Errorf("open loop: %w", err)
	}
	arrivals := pb.Arrivals_ARRIVALS_POISSON
	switch o.Arrivals {
	case Poisson, "":
	case Uniform:
		arrivals = pb.Arrivals_ARRIVALS_UNIFORM
	default:
		return nil, fmt.Errorf("open loop: arrivals is poisson or uniform, got %q", o.Arrivals)
	}
	return &pb.PhaseInput{
		Model: pb.LoadModel_LOAD_MODEL_OPEN, Rate: o.Rate, Arrivals: arrivals,
		Secs: o.Secs.Seconds(), WarmupSecs: o.Warmup.Seconds(),
	}, nil
}

// PhaseResult is what one phase measured in its window.
type PhaseResult struct {
	// OpsPerSec is the operations completed in the window divided by its length.
	OpsPerSec float64
	// P50Ms and P99Ms are the latency's median and 99th percentile, milliseconds.
	P50Ms, P99Ms float64
}
