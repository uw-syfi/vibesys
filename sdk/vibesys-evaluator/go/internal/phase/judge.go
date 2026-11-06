package phase

import (
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/evaluator"
	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
)

// The saturation guard (docs/spec.md, "Saturation guard and validity").
const (
	// MaxGeneratorWaited is the share of the window's operations that may have waited for the
	// generator.
	MaxGeneratorWaited = 0.01
	// MaxLoadgenCPU is loadgen's CPU over the window, as a share of its quota, above which it may
	// be the bottleneck.
	MaxLoadgenCPU = 0.8
	// MaxSendLagShare is the share of the latency p99 that the open loop's send lag p99 may reach
	// ...
	MaxSendLagShare = 0.1
	// SendLagFloorMs ... or this floor, whichever is larger. `loadgen selftest` will replace the
	// floor with twice the latency it measures loadgen adding.
	SendLagFloorMs = 1.0
	// MinSaturatedShare: when at least this share of the window's operations were due while every
	// sender was busy (loadgen's saturated), the send lag's tail is the application holding the
	// senders, which its latency (end - scheduled) already includes: not a reason to fail.
	MinSaturatedShare = 0.01
)

// Measurement is what a phase measured in its window.
type Measurement struct {
	OpsPerSec, P50Ms, P99Ms float64
}

// Verdict is a phase's measurement, or why it is not one.
type Verdict struct {
	// Measurement is set when Failure is empty.
	Measurement Measurement
	// Failure is why the phase is not a measurement.
	Failure string
}

// ReadOutput reads the PhaseOutput the pod left in dir.
func ReadOutput(dir string) (*pb.PhaseOutput, error) {
	raw, err := os.ReadFile(filepath.Join(dir, OutputFile))
	if err != nil {
		return nil, err
	}
	output := &pb.PhaseOutput{}
	if err := proto.Unmarshal(raw, output); err != nil {
		return nil, fmt.Errorf("%s: not a PhaseOutput: %w", filepath.Join(dir, OutputFile), err)
	}
	return output, nil
}

// Judge says whether the output is a measurement under validity. An error is a summary loadgen
// cannot have written.
func Judge(output *pb.PhaseOutput, validity evaluator.Validity) (Verdict, error) {
	s := output.GetSummary()
	if s == nil {
		return Verdict{Failure: "the phase output has no loadgen summary"}, nil
	}
	p99, err := ms(s.GetLatency(), 99)
	if err != nil {
		return Verdict{}, err
	}
	reason, err := guard(s, p99, validity)
	if err != nil {
		return Verdict{}, err
	}
	if reason == "" {
		reason = rules(s, validity)
	}
	if reason != "" {
		return Verdict{Failure: reason}, nil
	}
	p50, err := ms(s.GetLatency(), 50)
	if err != nil {
		return Verdict{}, err
	}
	return Verdict{Measurement: Measurement{OpsPerSec: s.GetAchievedRate(), P50Ms: p50, P99Ms: p99}}, nil
}

func ms(h *pb.HdrHistogram, percentile float64) (float64, error) {
	if h == nil {
		return 0, nil
	}
	merged, err := evaluator.MergeHistograms(h.GetEncoded())
	if err != nil {
		return 0, fmt.Errorf("loadgen's summary holds a histogram it cannot have written: %w", err)
	}
	return float64(merged.Percentile(percentile)) / 1000, nil
}

// errorKinds is " (transport 2, status 1)" for the summary's errors by kind, or "".
func errorKinds(s *pb.PhaseSummary) string {
	kinds := make([]uint32, 0, len(s.GetErrorsByKind()))
	for kind, count := range s.GetErrorsByKind() {
		if count > 0 {
			kinds = append(kinds, kind)
		}
	}
	if len(kinds) == 0 {
		return ""
	}
	slices.Sort(kinds)
	parts := make([]string, len(kinds))
	for i, kind := range kinds {
		name := strings.ToLower(strings.TrimPrefix(pb.ErrorKind(kind).String(), "ERROR_KIND_"))
		parts[i] = fmt.Sprintf("%s %d", name, s.GetErrorsByKind()[kind])
	}
	return " (" + strings.Join(parts, ", ") + ")"
}

func percent(x float64) string { return fmt.Sprintf("%.0f%%", 100*x) }

// guard is the saturation guard and the sustained rate: did loadgen offer the load asked for?
func guard(s *pb.PhaseSummary, p99Ms float64, validity evaluator.Validity) (string, error) {
	completed := max(s.GetCompleted(), 1)
	if gen := s.GetGenerator(); gen != nil && float64(gen.GetWaited()) > MaxGeneratorWaited*float64(completed) {
		waited, err := ms(gen.GetWait(), 99)
		if err != nil {
			return "", err
		}
		return fmt.Sprintf("the request generator fell behind: %d of the window's %d operations waited for it (more than %s; "+
			"p99 wait %.2f ms); it wrote %.0f ops/s, so the phase measures the generator, not the application: "+
			"make build cheaper or give the phase pod more CPU",
			gen.GetWaited(), s.GetCompleted(), percent(MaxGeneratorWaited), waited, gen.GetRate()), nil
	}
	if s.GetLoadgenCpuFraction() > MaxLoadgenCPU {
		return fmt.Sprintf("loadgen used %s of its CPU over the window (more than %s): it may be the bottleneck, "+
			"so the phase measures it, not the application", percent(s.GetLoadgenCpuFraction()), percent(MaxLoadgenCPU)), nil
	}
	if s.GetModel() != pb.LoadModel_LOAD_MODEL_OPEN {
		return "", nil
	}
	lag, err := ms(s.GetSendLag(), 99)
	if err != nil {
		return "", err
	}
	allowed := max(MaxSendLagShare*p99Ms, SendLagFloorMs)
	held := float64(s.GetSaturated()) >= MinSaturatedShare*float64(completed)
	if lag > allowed && !held {
		return fmt.Sprintf("loadgen sent late: send lag p99 %.2f ms is above %.2f ms (the larger of %s of latency p99 "+
			"%.2f ms and %.1f ms); the offered load was not what was asked for",
			lag, allowed, percent(MaxSendLagShare), p99Ms, SendLagFloorMs), nil
	}
	ratio := 0.0
	if s.GetTargetRate() > 0 {
		ratio = s.GetOfferedRate() / s.GetTargetRate()
	}
	if ratio < validity.MinSustainedRatio {
		return fmt.Sprintf("offered %.1f ops/s of the %.1f asked for (%.1f%%, below min_sustained_ratio %s): "+
			"the load was not sustained", s.GetOfferedRate(), s.GetTargetRate(), 100*ratio, percent(validity.MinSustainedRatio)), nil
	}
	return "", nil
}

// rules are the task's validity rules over the window's operations.
func rules(s *pb.PhaseSummary, validity evaluator.Validity) string {
	if s.GetCompleted() == 0 {
		return "no operation completed in the measurement window"
	}
	if rate := float64(s.GetErrors()) / float64(s.GetCompleted()); rate > validity.MaxErrorRate {
		return fmt.Sprintf("error rate %.4f%% is above max_error_rate %.4f%%: %d loadgen errors%s over %d operations",
			100*rate, 100*validity.MaxErrorRate, s.GetErrors(), errorKinds(s), s.GetCompleted())
	}
	completed := map[uint32]uint64{}
	for _, op := range s.GetOperations() {
		completed[op.GetOperation()] = op.GetCompleted()
	}
	var short []string
	for n, name := range s.GetOperationNames() {
		if completed[uint32(n)] < uint64(validity.MinOpsPerOperation) {
			short = append(short, fmt.Sprintf("%s (%d)", name, completed[uint32(n)]))
		}
	}
	if len(short) > 0 {
		return fmt.Sprintf("operations completed fewer than min_ops_per_operation=%d times in the window: %s; "+
			"lengthen the window or raise their weight", validity.MinOpsPerOperation, strings.Join(short, ", "))
	}
	return ""
}

// Log is the progress lines of a judged phase: how the generator kept up and what it measured.
func Log(in *pb.PhaseInput, output *pb.PhaseOutput, verdict Verdict) []string {
	lines := generatorLog(in, output)
	if verdict.Failure == "" {
		m := verdict.Measurement
		lines = append(lines, fmt.Sprintf("phase %d (%s): %.1f ops/s, p50 %.2f ms, p99 %.2f ms",
			in.GetPhaseIndex(), Describe(in), m.OpsPerSec, m.P50Ms, m.P99Ms))
	}
	return lines
}

// Counts is the log of a phase that is not judged (an accuracy run's): how the generator kept up,
// what the phase sent and how many of its operations failed, by kind.
func Counts(in *pb.PhaseInput, output *pb.PhaseOutput) []string {
	s := output.GetSummary()
	return append(generatorLog(in, output), fmt.Sprintf("phase %d (%s): %d operations sent, %d failed%s",
		in.GetPhaseIndex(), Describe(in), s.GetSent(), s.GetErrors(), errorKinds(s)))
}

func generatorLog(in *pb.PhaseInput, output *pb.PhaseOutput) []string {
	gen := output.GetSummary().GetGenerator()
	if gen == nil {
		return nil
	}
	return []string{fmt.Sprintf("phase %d: the generator wrote %.0f ops/s, %d operations waited for it",
		in.GetPhaseIndex(), gen.GetRate(), gen.GetWaited())}
}

// Failure is the sample's error message for a phase that is not a measurement.
func Failure(in *pb.PhaseInput, reason string) string {
	return fmt.Sprintf("phase %d (%s): %s", in.GetPhaseIndex(), Describe(in), reason)
}
