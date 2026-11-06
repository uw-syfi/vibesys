// Package summary folds operation results into a PhaseSummary. It is pure: the same results, in
// any order, give the same summary. Counts and histograms cover the operations scheduled in the
// measurement window; see PhaseSummary in loadgen.proto for each field.
package summary

import (
	"encoding/base64"
	"fmt"
	"time"

	"google.golang.org/protobuf/proto"

	hdr "github.com/HdrHistogram/hdrhistogram-go"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
)

// Histogram bounds: 1 us to one hour, three significant figures.
const (
	lowestMicros  = 1
	highestMicros = 3_600_000_000
	sigFigs       = 3
)

// Window is the measurement window, [Start, End) in Unix nanoseconds.
type Window struct {
	Start, End int64
}

// Secs is the window's length in seconds.
func (w Window) Secs() float64 { return float64(w.End-w.Start) / 1e9 }

// Contains reports whether t is in the window.
func (w Window) Contains(t int64) bool { return t >= w.Start && t < w.End }

// Record is one result plus when a sender took it up (after any send lag, before any wait for its
// exclusive key; Picked is 0 for an operation that was never sent) and how long the sender waited
// for the generator to produce it.
type Record struct {
	Result        *pb.OperationResult
	Picked        int64
	GeneratorWait time.Duration
}

// Aggregator accumulates records. It is not safe for concurrent use.
type Aggregator struct {
	window        Window
	names         []string
	latency       *hdr.Histogram
	perOp         []*hdr.Histogram
	perOpDone     []uint64
	perOpErrors   []uint64
	sendLag       *hdr.Histogram
	exclusiveWait *hdr.Histogram
	generatorWait *hdr.Histogram
	maxLagNanos   int64
	maxGenNanos   int64
	genWaited     uint64
	sent          uint64
	started       uint64
	completed     uint64
	ended         uint64
	errors        uint64
	spilled       uint64
	byKind        map[uint32]uint64
}

// New returns an empty aggregator for a window and the plan's operation names.
func New(window Window, names []string) *Aggregator {
	a := &Aggregator{
		window:        window,
		names:         names,
		latency:       newHistogram(),
		sendLag:       newHistogram(),
		exclusiveWait: newHistogram(),
		generatorWait: newHistogram(),
		perOp:         make([]*hdr.Histogram, len(names)),
		perOpDone:     make([]uint64, len(names)),
		perOpErrors:   make([]uint64, len(names)),
		byKind:        map[uint32]uint64{},
	}
	for i := range a.perOp {
		a.perOp[i] = newHistogram()
	}
	return a
}

func newHistogram() *hdr.Histogram { return hdr.New(lowestMicros, highestMicros, sigFigs) }

func record(h *hdr.Histogram, nanos int64) {
	micros := max(nanos/1000, 0)
	_ = h.RecordValue(min(micros, highestMicros))
}

// Add folds one record in.
func (a *Aggregator) Add(r Record) {
	result := r.Result
	sent := result.GetStartUnixNanos() != 0
	if sent && a.window.Contains(result.GetStartUnixNanos()) {
		a.started++
	}
	scheduled := result.GetScheduledUnixNanos()
	if !a.window.Contains(scheduled) {
		return
	}
	op := result.GetOperation()
	a.completed++
	wait := r.GeneratorWait.Nanoseconds()
	record(a.generatorWait, wait)
	a.maxGenNanos = max(a.maxGenNanos, wait)
	if wait > 0 {
		a.genWaited++
	}
	a.perOpDone[op]++
	failed := result.GetError() != pb.ErrorKind_ERROR_KIND_UNSPECIFIED
	if failed {
		a.errors++
		a.perOpErrors[op]++
		a.byKind[uint32(result.GetError())]++
	}
	if !sent {
		return
	}
	a.sent++
	if result.GetEndUnixNanos() >= a.window.End {
		a.spilled++
	} else {
		a.ended++
	}
	lag := r.Picked - scheduled
	a.maxLagNanos = max(a.maxLagNanos, lag)
	record(a.sendLag, lag)
	record(a.exclusiveWait, result.GetStartUnixNanos()-r.Picked)
	if !failed {
		latency := result.GetEndUnixNanos() - scheduled
		record(a.latency, latency)
		record(a.perOp[op], latency)
	}
}

// Phase is what the summary says about the phase beyond its results.
type Phase struct {
	Model        pb.LoadModel
	TargetRate   float64
	Clients      uint32
	CPUFraction  float64
	MinSustained float64
	// Saturated counts the open loop's window operations that waited for a busy sender.
	Saturated      uint64
	ResultsSpilled uint64
	// Generator is the stream's own GeneratorStats (read, rate, ready_secs, buffer); the
	// summary adds the senders' waits.
	Generator *pb.GeneratorStats
}

func rate(count uint64, secs float64) float64 {
	if secs <= 0 {
		return 0
	}
	return float64(count) / secs
}

// Summary returns the PhaseSummary of the records added so far.
func (a *Aggregator) Summary(phase Phase) (*pb.PhaseSummary, error) {
	secs := a.window.Secs()
	summary := &pb.PhaseSummary{
		Model:                phase.Model,
		TargetRate:           phase.TargetRate,
		Clients:              phase.Clients,
		WindowStartUnixNanos: a.window.Start,
		WindowEndUnixNanos:   a.window.End,
		Sent:                 a.sent,
		Completed:            a.completed,
		Errors:               a.errors,
		ErrorsByKind:         a.byKind,
		Spilled:              a.spilled,
		OfferedRate:          rate(a.started, secs),
		AchievedRate:         rate(a.ended, secs),
		MaxSendLagMicros:     max(a.maxLagNanos/1000, 0),
		LoadgenCpuFraction:   max(phase.CPUFraction, 0),
		Saturated:            phase.Saturated,
		ResultsSpilled:       phase.ResultsSpilled,
		OperationNames:       a.names,
	}
	generator := &pb.GeneratorStats{}
	if phase.Generator != nil {
		generator = proto.Clone(phase.Generator).(*pb.GeneratorStats)
	}
	generator.Waited = a.genWaited
	generator.MaxWaitMicros = max(a.maxGenNanos/1000, 0)
	summary.Sustained = phase.Model != pb.LoadModel_LOAD_MODEL_OPEN || summary.GetOfferedRate() >= phase.MinSustained*phase.TargetRate
	var err error
	if summary.SendLag, err = Encode(a.sendLag); err != nil {
		return nil, err
	}
	if summary.ExclusiveWait, err = Encode(a.exclusiveWait); err != nil {
		return nil, err
	}
	if summary.Latency, err = Encode(a.latency); err != nil {
		return nil, err
	}
	if generator.Wait, err = Encode(a.generatorWait); err != nil {
		return nil, err
	}
	summary.Generator = generator
	for i := range a.names {
		latency, err := Encode(a.perOp[i])
		if err != nil {
			return nil, err
		}
		summary.Operations = append(summary.Operations, &pb.OperationSummary{
			Operation: uint32(i),
			Completed: a.perOpDone[i],
			Errors:    a.perOpErrors[i],
			Latency:   latency,
		})
	}
	return summary, nil
}

// Encode returns a histogram in the schema's encoding: the compressed V2 bytes, not base64.
func Encode(h *hdr.Histogram) (*pb.HdrHistogram, error) {
	text, err := h.Encode(hdr.V2CompressedEncodingCookieBase)
	if err != nil {
		return nil, fmt.Errorf("encode histogram: %w", err)
	}
	raw, err := base64.StdEncoding.DecodeString(string(text))
	if err != nil {
		return nil, fmt.Errorf("encode histogram: %w", err)
	}
	return &pb.HdrHistogram{
		LowestTrackableMicros:  h.LowestTrackableValue(),
		HighestTrackableMicros: h.HighestTrackableValue(),
		SignificantFigures:     uint32(h.SignificantFigures()),
		Encoded:                raw,
	}, nil
}

// Decode reads a histogram written by Encode (or by the Python side).
func Decode(h *pb.HdrHistogram) (*hdr.Histogram, error) {
	return hdr.Decode([]byte(base64.StdEncoding.EncodeToString(h.GetEncoded())))
}

// Percentile returns a histogram's value at a percentile, in microseconds.
func Percentile(h *pb.HdrHistogram, percentile float64) (int64, error) {
	decoded, err := Decode(h)
	if err != nil {
		return 0, err
	}
	return decoded.ValueAtQuantile(percentile), nil
}
