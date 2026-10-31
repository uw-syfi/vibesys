// Package machinestats samples this machine's CPU, memory and GPUs for the
// machine_stats topic. A section it cannot read is a typed absence next to the
// sections it can, never a zero and never "no GPUs". These are OS facts, with no
// VibeSys meaning, and on a shared login node they describe that node.
package machinestats

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/clock"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"github.com/uw-syfi/vibesys/gateway/internal/subscription"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// Key is the topic's single key: one gateway serves one machine.
const Key = "machine"

// SampleDeadline bounds one sample. A section past it reports TIMED_OUT.
const SampleDeadline = 5 * time.Second

// maxValueBytes is the topic's bound on one value, which a sample must fit.
var maxValueBytes = hostoperations.Topics[hostoperations.TopicMachineStats].MaxValueBytes

var (
	errParse      = errors.New("unexpected tool output")
	errUnverified = errors.New("tool output format has no recording")
)

// Sampler reads one MachineStats at a time. It keeps CPU counters between
// samples, so a subscription owns its own Sampler, and calls never overlap.
type Sampler struct {
	ticks    clock.Clock
	host     Host
	platform platform
	calls    *calls
}

// calls holds, per section, whether a call into the Host is in flight. Neither
// Host.Find nor Host.Run can be relied on to return when its context ends: a
// PATH entry on a hung mount stalls LookPath, and a tool in uninterruptible sleep
// outlasts its kill. So a stuck call is left to finish alone, and until it does
// its section reports TIMED_OUT and starts no second call. At most one call per
// section is ever stuck, however many samples or subscriptions come and go.
type calls struct{ cpu, memory, gpu atomic.Bool }

// platform reads the sections an operating system exposes differently.
type platform interface {
	cpu(ctx context.Context, host Host) (*apiv1.CpuStats, error)
	memory(ctx context.Context, host Host) (*apiv1.MemoryStats, error)
	gpus(ctx context.Context, host Host) (*apiv1.GpuList, error)
}

// NewSampler returns a sampler for goos. An operating system with no sampler is
// UNSUPPORTED, which retrying will not change.
func NewSampler(goos string, ticks clock.Clock, host Host) (*Sampler, error) {
	return newSampler(goos, ticks, host, &calls{})
}

// newSampler shares calls with the samplers of earlier subscriptions, which may
// have left a call stuck.
func newSampler(goos string, ticks clock.Clock, host Host, shared *calls) (*Sampler, error) {
	switch goos {
	case "linux":
		return &Sampler{ticks, host, &linux{ticks: ticks}, shared}, nil
	case "darwin":
		return &Sampler{ticks, host, macos{}, shared}, nil
	}
	return nil, subscription.Unsupported(fmt.Sprintf("no machine stats on %s", goos))
}

// Sample reads every section concurrently, so one slow tool costs only its own
// section. It returns by the deadline of ctx whatever the Host is doing: a
// section still running then reports TIMED_OUT.
func (s *Sampler) Sample(ctx context.Context) (*apiv1.MachineStats, error) {
	stats := &apiv1.MachineStats{}
	var sections sync.WaitGroup
	sections.Go(func() {
		value, absence := read(ctx, &s.calls.cpu, func() (*apiv1.CpuStats, error) { return s.platform.cpu(ctx, s.host) })
		if absence != nil {
			stats.Cpu = &apiv1.MachineStats_CpuUnavailable{CpuUnavailable: absence}
			return
		}
		stats.Cpu = &apiv1.MachineStats_CpuStats{CpuStats: value}
	})
	sections.Go(func() {
		value, absence := read(ctx, &s.calls.memory, func() (*apiv1.MemoryStats, error) { return s.platform.memory(ctx, s.host) })
		if absence != nil {
			stats.Memory = &apiv1.MachineStats_MemoryUnavailable{MemoryUnavailable: absence}
			return
		}
		stats.Memory = &apiv1.MachineStats_MemoryStats{MemoryStats: value}
	})
	sections.Go(func() {
		value, absence := read(ctx, &s.calls.gpu, func() (*apiv1.GpuList, error) { return s.platform.gpus(ctx, s.host) })
		if absence != nil {
			stats.Gpu = &apiv1.MachineStats_GpuUnavailable{GpuUnavailable: absence}
			return
		}
		stats.Gpu = &apiv1.MachineStats_Gpus{Gpus: value}
	})
	sections.Wait()
	stats.SampledAt = timestamppb.New(s.ticks.Now())
	// Each row can be valid while the list is not: a value over the topic's bound
	// ends the topic for every subscriber. Only the GPU list varies in size.
	if proto.Size(stats) > maxValueBytes && stats.GetGpus() != nil {
		stats.Gpu = &apiv1.MachineStats_GpuUnavailable{GpuUnavailable: &apiv1.Absence{Reason: apiv1.AbsenceReason_ABSENCE_REASON_PARSE_FAILED}}
	}
	return stats, nil
}

// read runs one section's Host calls and turns every way they can fail into the
// section's typed absence. It returns when they do or when ctx ends, whichever is
// first; in flight is held until they do, so a call that never returns is
// abandoned once and never repeated.
func read[T any](ctx context.Context, inflight *atomic.Bool, section func() (T, error)) (T, *apiv1.Absence) {
	var none T
	if !inflight.CompareAndSwap(false, true) {
		return none, &apiv1.Absence{Reason: apiv1.AbsenceReason_ABSENCE_REASON_TIMED_OUT}
	}
	type outcome struct {
		value   T
		absence *apiv1.Absence
	}
	done := make(chan outcome, 1)
	go func() {
		defer inflight.Store(false)
		value, absence := attempt(ctx, section)
		done <- outcome{value, absence}
	}()
	select {
	case result := <-done:
		return result.value, result.absence
	case <-ctx.Done():
		return none, &apiv1.Absence{Reason: apiv1.AbsenceReason_ABSENCE_REASON_TIMED_OUT}
	}
}

func attempt[T any](ctx context.Context, section func() (T, error)) (value T, absence *apiv1.Absence) {
	defer func() {
		if recover() != nil {
			absence = &apiv1.Absence{Reason: apiv1.AbsenceReason_ABSENCE_REASON_PARSE_FAILED}
		}
	}()
	value, err := section()
	if err != nil {
		return value, &apiv1.Absence{Reason: reason(ctx, err)}
	}
	return value, nil
}

func reason(ctx context.Context, err error) apiv1.AbsenceReason {
	switch {
	case ctx.Err() != nil:
		return apiv1.AbsenceReason_ABSENCE_REASON_TIMED_OUT
	case errors.Is(err, ErrToolNotFound):
		return apiv1.AbsenceReason_ABSENCE_REASON_TOOL_NOT_FOUND
	case errors.Is(err, errUnverified):
		return apiv1.AbsenceReason_ABSENCE_REASON_TOOL_UNVERIFIED
	case errors.Is(err, errParse):
		return apiv1.AbsenceReason_ABSENCE_REASON_PARSE_FAILED
	}
	return apiv1.AbsenceReason_ABSENCE_REASON_READ_FAILED
}

// Topic is the machine_stats topic. Each producer run gets a fresh Sampler.
func Topic(goos string, ticks clock.Clock, host Host) subscription.Topic[*apiv1.MachineStats] {
	policy := hostoperations.Topics[hostoperations.TopicMachineStats]
	return subscription.Topic[*apiv1.MachineStats]{
		Name:   hostoperations.TopicMachineStats,
		Policy: policy,
		Source: source{goos: goos, ticks: ticks, host: host, policy: policy, calls: &calls{}},
		Entry: func(key string, value *apiv1.MachineStats) *apiv1.TopicEntry {
			return &apiv1.TopicEntry{Key: key, Value: &apiv1.TopicEntry_MachineStats{MachineStats: value}}
		},
	}
}

type source struct {
	goos   string
	ticks  clock.Clock
	host   Host
	policy hostoperations.TopicPolicy
	// calls outlives each producer run, because a call left stuck by one still
	// holds its section when the next subscription starts.
	calls *calls
}

func (s source) Run(ctx context.Context, sink subscription.Sink[*apiv1.MachineStats]) error {
	sampler, err := newSampler(s.goos, s.ticks, s.host, s.calls)
	if err != nil {
		return err
	}
	return subscription.Sampled(Key, s.policy.Cadence, SampleDeadline, s.ticks, sampler.Sample).Run(ctx, sink)
}

func parseFailure(format string, args ...any) error {
	return fmt.Errorf("%w: %s", errParse, fmt.Sprintf(format, args...))
}
