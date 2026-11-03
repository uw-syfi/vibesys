package remote

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// CPU is the cpu.System of a Linux machine the shell reaches: its /proc, read with cat, and its
// name and architecture, read once.
type CPU struct {
	Shell   Shell
	Timers  clock.Timers
	Timeout time.Duration

	mu   sync.Mutex
	proc *cpu.Proc
}

var _ cpu.System = (*CPU)(nil)

// goArch names a kernel's machine type as Go does, as the host's facts name it.
var goArch = map[string]string{"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64", "ppc64le": "ppc64le", "s390x": "s390x", "riscv64": "riscv64"}

func (c *CPU) system(ctx context.Context) (*cpu.Proc, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.proc != nil {
		return c.proc, nil
	}
	bounded, done := Bounded(ctx, c.Timers, c.Timeout)
	defer done()
	out, err := Output(bounded, c.Shell, []string{"sh", "-c", "uname -s; uname -m; hostname; nproc"}, nil)
	if err != nil {
		return nil, err
	}
	f := strings.Fields(out)
	if len(f) != 4 || f[0] != "Linux" {
		return nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("%s is not a Linux machine this build can read (%q)", c.Shell.Where(), out)}
	}
	arch, ok := goArch[f[1]]
	if !ok {
		arch = f[1]
	}
	var n int
	_, _ = fmt.Sscan(f[3], &n)
	host := f[2]
	c.proc = &cpu.Proc{
		FS: ProcFS{Shell: c.Shell, Timers: c.Timers, Timeout: c.Timeout}, Hostname: func() (string, error) { return host, nil },
		OS: "linux", Arch: arch, Processors: n,
	}
	return c.proc, nil
}

// Machine implements cpu.System.
func (c *CPU) Machine(ctx context.Context) (core.MachineFacts, error) {
	p, err := c.system(ctx)
	if err != nil {
		return core.MachineFacts{}, err
	}
	return p.Machine(ctx)
}

// Read implements cpu.System.
func (c *CPU) Read(ctx context.Context) (cpu.Reading, error) {
	p, err := c.system(ctx)
	if err != nil {
		return cpu.Reading{}, err
	}
	return p.Read(ctx)
}
