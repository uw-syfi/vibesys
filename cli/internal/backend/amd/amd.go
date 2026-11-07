// Package amd reads what identifies a machine's AMD GPUs, for an environment's fingerprint: their
// model and count, with rocminfo (or amd-smi when rocminfo is not there), and the version of the
// node's amdgpu kernel driver, from /sys/module/amdgpu/version (or amd-smi). Script prints what
// Parse reads, so the facts can be read through a shell on the engine's machine (GPUs) or on a
// compute node inside a job, where a placement whose machine is allocated per job reads them.
package amd

import (
	"context"
	"fmt"
	"maps"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
)

// The fingerprint facts this package reports. They are not the names the NVIDIA GPUs' facts have,
// so a placement may give both kinds without a fact of one taking the name of the other's.
const (
	FactModel  = "amd_gpu_model"
	FactCount  = "amd_gpu_count"
	FactDriver = "amdgpu_driver"
)

// Timeout bounds the reading of the facts.
const Timeout = 2 * time.Minute

// Script is the POSIX shell script whose output Parse reads: the driver's version, then the GPU
// agents rocminfo lists, then amd-smi's static facts when rocminfo lists none. A machine with
// neither program, or no driver, prints empty sections, which Parse refuses.
const Script = `echo '==driver=='
cat /sys/module/amdgpu/version 2>/dev/null
echo '==rocminfo=='
r=$(command -v rocminfo 2>/dev/null || ls /opt/rocm/bin/rocminfo 2>/dev/null)
[ -n "$r" ] && "$r" 2>/dev/null | grep -E '^[[:space:]]*(Name|Marketing Name|Device Type):'
echo '==amd-smi=='
a=$(command -v amd-smi 2>/dev/null || ls /opt/rocm/bin/amd-smi 2>/dev/null)
[ -n "$a" ] && "$a" static --asic --driver 2>/dev/null | grep -E '^[[:space:]]*(GPU|MARKET_NAME|VERSION):'
exit 0`

var (
	keyValue = regexp.MustCompile(`^\s*([A-Za-z_ ]+?):\s*(.*?)\s*$`)
	smiGPU   = regexp.MustCompile(`^\s*GPU:\s*[0-9]+\s*$`)
)

// sections splits Script's output by its ==name== lines.
func sections(out string) map[string][]string {
	got := map[string][]string{}
	name := ""
	for _, line := range strings.Split(out, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "==") && strings.HasSuffix(trimmed, "==") && len(trimmed) > 4 {
			name = strings.Trim(trimmed, "=")
			got[name] = got[name]
			continue
		}
		if name != "" && trimmed != "" {
			got[name] = append(got[name], line)
		}
	}
	return got
}

// Parse reads the facts from Script's output. GPUs of more than one model are refused, as a
// fingerprint names one, and so is a machine whose output names no GPU or no driver.
func Parse(out string) (map[string]string, error) {
	unavailable := func(format string, args ...any) error {
		return &core.Failure{Kind: core.FailureUnavailable, Message: "amd: " + fmt.Sprintf(format, args...)}
	}
	s := sections(out)
	driver := ""
	if d := s["driver"]; len(d) > 0 {
		driver = strings.TrimSpace(d[0])
	}
	models, smiDriver := fromSMI(s["amd-smi"])
	if rocm := fromRocminfo(s["rocminfo"]); len(rocm) > 0 {
		models = rocm
	}
	if driver == "" {
		driver = smiDriver
	}
	switch {
	case len(models) == 0:
		return nil, unavailable("neither rocminfo nor amd-smi lists a GPU")
	case driver == "":
		return nil, unavailable("the amdgpu driver's version is not in /sys/module/amdgpu/version, and amd-smi does not say it")
	}
	for _, m := range models[1:] {
		if m != models[0] {
			return nil, unavailable("the machine has GPUs of more than one model (%s and %s)", models[0], m)
		}
	}
	return map[string]string{FactModel: models[0], FactCount: strconv.Itoa(len(models)), FactDriver: driver}, nil
}

// fromRocminfo is the model of each GPU agent of rocminfo's Name, Marketing Name and Device Type
// lines: every agent starts with Name, and the CPU agents are left out.
func fromRocminfo(lines []string) []string {
	var models []string
	model, gpu := "", false
	flush := func() {
		if gpu && model != "" {
			models = append(models, model)
		}
		model, gpu = "", false
	}
	for _, line := range lines {
		m := keyValue.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		switch m[1] {
		case "Name":
			flush()
		case "Marketing Name":
			model = m[2]
		case "Device Type":
			gpu = m[2] == "GPU"
		}
	}
	flush()
	return models
}

// fromSMI is the model of each GPU of amd-smi's static listing, and the driver's version it names.
func fromSMI(lines []string) (models []string, driver string) {
	for _, line := range lines {
		if smiGPU.MatchString(line) {
			continue
		}
		m := keyValue.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		switch m[1] {
		case "MARKET_NAME":
			models = append(models, m[2])
		case "VERSION":
			if driver == "" {
				driver = m[2]
			}
		}
	}
	return models, driver
}

// GPUs reads the AMD GPU facts of the machine the shell reaches, once: they do not change while
// the program runs.
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
		return maps.Clone(g.facts), nil
	}
	bounded, done := remote.Bounded(ctx, g.Timers, Timeout)
	defer done()
	out, err := remote.Output(bounded, g.Timers, g.Shell, []string{"sh", "-c", Script}, "")
	if err != nil {
		return nil, fmt.Errorf("reading the GPUs: %w", err)
	}
	facts, err := Parse(out)
	if err != nil {
		return nil, err
	}
	g.facts = facts
	return maps.Clone(facts), nil
}
