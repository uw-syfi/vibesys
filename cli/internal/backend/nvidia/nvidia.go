// Package nvidia reads what identifies a machine's NVIDIA GPUs, for an environment's fingerprint:
// their model and count, the driver, and the CUDA version the driver supports. It reads them with
// nvidia-smi, on whatever machine a remote.Shell reaches, once: they do not change while the
// program runs.
package nvidia

import (
	"context"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
)

// The fingerprint facts this package reports.
const (
	FactModel      = "gpu_model"
	FactCount      = "gpu_count"
	FactDriver     = "gpu_driver"
	FactCUDADriver = "cuda_driver"
)

// Timeout bounds the reading of the facts.
const Timeout = 2 * time.Minute

// GPUs reads the GPU facts of the machine the shell reaches.
type GPUs struct {
	Shell  remote.Shell
	Timers clock.Timers

	mu    sync.Mutex
	facts map[string]string
}

// GPUFacts returns the facts, reading them the first time.
func (g *GPUs) GPUFacts(ctx context.Context) (map[string]string, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.facts != nil {
		return clone(g.facts), nil
	}
	list, err := remote.Within(ctx, g.Timers, g.Shell, Timeout, []string{"nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"}, "")
	if err != nil {
		return nil, fmt.Errorf("reading the GPUs: %w", err)
	}
	header, err := remote.Within(ctx, g.Timers, g.Shell, Timeout, []string{"nvidia-smi"}, "")
	if err != nil {
		return nil, fmt.Errorf("reading the GPUs: %w", err)
	}
	facts, err := Parse(list, header)
	if err != nil {
		return nil, err
	}
	g.facts = facts
	return clone(facts), nil
}

func clone(m map[string]string) map[string]string {
	out := make(map[string]string, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

var cudaVersion = regexp.MustCompile(`CUDA Version:\s*([0-9]+(?:\.[0-9]+)*)`)

// Parse reads the facts from nvidia-smi's GPU list (--query-gpu=name,driver_version
// --format=csv,noheader) and its summary, whose header names the CUDA version. GPUs of more than
// one model or driver are refused: a fingerprint names one of each.
func Parse(list, summary string) (map[string]string, error) {
	unavailable := func(format string, args ...any) error {
		return &core.Failure{Kind: core.FailureUnavailable, Message: "nvidia: " + fmt.Sprintf(format, args...)}
	}
	var model, driver string
	count := 0
	for _, line := range strings.Split(strings.TrimSpace(list), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		name, version, ok := strings.Cut(line, ",")
		name, version = strings.TrimSpace(name), strings.TrimSpace(version)
		if !ok || name == "" || version == "" {
			return nil, unavailable("nvidia-smi listed a GPU as %q, not a name and a driver version", line)
		}
		if count > 0 && (name != model || version != driver) {
			return nil, unavailable("the machine has GPUs of more than one model or driver (%s %s and %s %s)", model, driver, name, version)
		}
		model, driver = name, version
		count++
	}
	if count == 0 {
		return nil, unavailable("nvidia-smi lists no GPU")
	}
	m := cudaVersion.FindStringSubmatch(summary)
	if m == nil {
		return nil, unavailable("nvidia-smi does not say the CUDA version of the driver")
	}
	return map[string]string{
		FactModel: model, FactCount: strconv.Itoa(count), FactDriver: driver, FactCUDADriver: m[1],
	}, nil
}
