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
	"time"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/clock"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"github.com/uw-syfi/vibesys/gateway/internal/subscription"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// Key is the topic's single key: one gateway serves one machine.
const Key = "machine"

// SampleDeadline bounds one sample. A section past it reports TIMED_OUT.
const SampleDeadline = 5 * time.Second

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
}

// platform reads the sections an operating system exposes differently.
type platform interface {
	cpu(ctx context.Context, host Host) (*apiv1.CpuStats, error)
	memory(ctx context.Context, host Host) (*apiv1.MemoryStats, error)
	gpus(ctx context.Context, host Host) (*apiv1.GpuList, error)
}

// NewSampler returns a sampler for goos. An operating system with no sampler is
// UNSUPPORTED, which retrying will not change.
func NewSampler(goos string, ticks clock.Clock, host Host) (*Sampler, error) {
	switch goos {
	case "linux":
		return &Sampler{ticks, host, &linux{ticks: ticks}}, nil
	case "darwin":
		return &Sampler{ticks, host, macos{}}, nil
	}
	return nil, subscription.Unsupported(fmt.Sprintf("no machine stats on %s", goos))
}

// Sample reads every section concurrently, so one slow tool costs only its own
// section. It returns once ctx ends and every Host call has returned.
func (s *Sampler) Sample(ctx context.Context) (*apiv1.MachineStats, error) {
	stats := &apiv1.MachineStats{}
	var sections sync.WaitGroup
	sections.Go(func() {
		value, absence := read(ctx, func() (*apiv1.CpuStats, error) { return s.platform.cpu(ctx, s.host) })
		if absence != nil {
			stats.Cpu = &apiv1.MachineStats_CpuUnavailable{CpuUnavailable: absence}
			return
		}
		stats.Cpu = &apiv1.MachineStats_CpuStats{CpuStats: value}
	})
	sections.Go(func() {
		value, absence := read(ctx, func() (*apiv1.MemoryStats, error) { return s.platform.memory(ctx, s.host) })
		if absence != nil {
			stats.Memory = &apiv1.MachineStats_MemoryUnavailable{MemoryUnavailable: absence}
			return
		}
		stats.Memory = &apiv1.MachineStats_MemoryStats{MemoryStats: value}
	})
	sections.Go(func() {
		value, absence := read(ctx, func() (*apiv1.GpuList, error) { return s.platform.gpus(ctx, s.host) })
		if absence != nil {
			stats.Gpu = &apiv1.MachineStats_GpuUnavailable{GpuUnavailable: absence}
			return
		}
		stats.Gpu = &apiv1.MachineStats_Gpus{Gpus: value}
	})
	sections.Wait()
	stats.SampledAt = timestamppb.New(s.ticks.Now())
	return stats, nil
}

// read turns every way a section can fail into its typed absence.
func read[T any](ctx context.Context, section func() (T, error)) (value T, absence *apiv1.Absence) {
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
		Source: source{goos, ticks, host, policy},
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
}

func (s source) Run(ctx context.Context, sink subscription.Sink[*apiv1.MachineStats]) error {
	sampler, err := NewSampler(s.goos, s.ticks, s.host)
	if err != nil {
		return err
	}
	return subscription.Sampled(Key, s.policy.Cadence, SampleDeadline, s.ticks, sampler.Sample).Run(ctx, sink)
}

func parseFailure(format string, args ...any) error {
	return fmt.Errorf("%w: %s", errParse, fmt.Sprintf(format, args...))
}
