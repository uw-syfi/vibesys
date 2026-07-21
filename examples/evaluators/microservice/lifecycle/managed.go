package lifecycle

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
)

const stopTimeout = 5 * time.Second

// candidateSupervisor keeps the PID namespace alive when a launcher exits
// after daemonizing services. Detached processes cannot escape their parent
// PID namespace; terminating bubblewrap's namespace init atomically terminates
// every process, including double-forked and setsid descendants.
const candidateSupervisor = `
"$@" &
main=$!
wait "$main"
status=$?
while :; do
    found=0
    for process in /proc/[0-9]*; do
        pid=${process##*/}
        if [ "$pid" != "1" ] && [ "$pid" != "$$" ]; then
            found=1
            break
        fi
    done
    if [ "$found" -eq 0 ]; then
        exit "$status"
    fi
    sleep 0.02
done
`

// ManagedCandidate runs a candidate inside a dedicated PID namespace. The
// constructor fails closed when bubblewrap is unavailable: process-tree
// sampling cannot prove that a fast daemon did not escape before a crash.
type ManagedCandidate struct {
	command        []string
	stopCommand    []string
	cleanupCommand []string
	cwd            string
	env            map[string]string
	bwrap          string

	mu        sync.Mutex
	cmd       *exec.Cmd
	waitDone  chan error
	log       *os.File
	logPath   string
	closeOnce sync.Once
	closeErr  error
}

func NewManagedCandidate(
	command []string,
	stopCommand []string,
	cleanupCommand []string,
	cwd string,
	environment map[string]string,
) (*ManagedCandidate, error) {
	if len(command) == 0 {
		return nil, fmt.Errorf("managed candidate command must not be empty")
	}
	for index, argument := range command {
		if argument == "" {
			return nil, fmt.Errorf("managed candidate command argument %d is empty", index)
		}
	}
	for index, argument := range stopCommand {
		if argument == "" {
			return nil, fmt.Errorf("managed candidate stop command argument %d is empty", index)
		}
	}
	for index, argument := range cleanupCommand {
		if argument == "" {
			return nil, fmt.Errorf("managed candidate cleanup command argument %d is empty", index)
		}
	}
	absCWD, err := filepath.Abs(cwd)
	if err != nil {
		return nil, fmt.Errorf("resolve candidate directory: %w", err)
	}
	info, err := os.Stat(absCWD)
	if err != nil || !info.IsDir() {
		return nil, fmt.Errorf("candidate directory %q is not a directory", absCWD)
	}
	for name := range environment {
		if name == "" || strings.Contains(name, "=") {
			return nil, fmt.Errorf("invalid managed candidate environment name %q", name)
		}
	}
	bwrap, err := exec.LookPath("bwrap")
	if err != nil {
		return nil, fmt.Errorf(
			"managed candidate requires bubblewrap PID-namespace containment: %w",
			err,
		)
	}
	if err := probePIDNamespace(bwrap); err != nil {
		return nil, fmt.Errorf("managed candidate PID-namespace containment is unavailable: %w", err)
	}
	log, err := os.CreateTemp("", "microservice-candidate-*.log")
	if err != nil {
		return nil, fmt.Errorf("create candidate log: %w", err)
	}
	return &ManagedCandidate{
		command:        append([]string(nil), command...),
		stopCommand:    append([]string(nil), stopCommand...),
		cleanupCommand: append([]string(nil), cleanupCommand...),
		cwd:            absCWD,
		env:            cloneEnvironment(environment),
		bwrap:          bwrap,
		log:            log,
		logPath:        log.Name(),
	}, nil
}

func probePIDNamespace(bwrap string) error {
	command := exec.Command(
		bwrap,
		"--unshare-pid",
		"--die-with-parent",
		"--ro-bind", "/", "/",
		"--proc", "/proc",
		"/bin/true",
	)
	if output, err := command.CombinedOutput(); err != nil {
		return fmt.Errorf("bubblewrap probe: %w: %s", err, strings.TrimSpace(string(output)))
	}
	return nil
}

func (m *ManagedCandidate) Prepare(ctx context.Context) error {
	return m.Start(ctx)
}

func (m *ManagedCandidate) Start(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.cmd != nil {
		return fmt.Errorf("candidate is already running")
	}
	arguments := []string{
		"--unshare-pid",
		"--die-with-parent",
		"--bind", "/", "/",
		"--proc", "/proc",
		"--dev-bind", "/dev", "/dev",
		"--chdir", m.cwd,
		"/bin/sh", "-c", candidateSupervisor, "vibesys-candidate-supervisor",
	}
	arguments = append(arguments, m.command...)
	command := exec.Command(m.bwrap, arguments...)
	command.Dir = m.cwd
	command.Env = environmentWithOverrides(os.Environ(), m.env)
	command.Stdout = m.log
	command.Stderr = m.log
	if err := command.Start(); err != nil {
		return fmt.Errorf("start contained candidate: %w", err)
	}
	m.cmd = command
	m.waitDone = make(chan error, 1)
	go func() { m.waitDone <- command.Wait() }()
	return nil
}

func cloneEnvironment(environment map[string]string) map[string]string {
	cloned := make(map[string]string, len(environment))
	for name, value := range environment {
		cloned[name] = value
	}
	return cloned
}

func environmentWithOverrides(base []string, overrides map[string]string) []string {
	result := make([]string, 0, len(base)+len(overrides))
	for _, entry := range base {
		name, _, _ := strings.Cut(entry, "=")
		if _, overridden := overrides[name]; overridden {
			continue
		}
		result = append(result, entry)
	}
	for name, value := range overrides {
		result = append(result, name+"="+value)
	}
	return result
}

func (m *ManagedCandidate) Stop(ctx context.Context, hard bool) error {
	m.mu.Lock()
	command := m.cmd
	waitDone := m.waitDone
	if command == nil {
		m.mu.Unlock()
		return nil
	}
	m.mu.Unlock()

	// Check for an already-reaped process before signaling. os.Process.Signal
	// uses a pidfd on supported Linux kernels, avoiding bare-PID reuse races.
	select {
	case <-waitDone:
		m.clear(command)
		return m.runStopCommand(ctx)
	default:
	}

	signal := os.Signal(syscall.SIGTERM)
	if hard {
		signal = syscall.SIGKILL
	}
	_ = command.Process.Signal(signal)
	exited := waitForExit(waitDone, stopTimeout)
	if !exited && !hard {
		_ = command.Process.Signal(syscall.SIGKILL)
		exited = waitForExit(waitDone, stopTimeout)
	}
	if !exited {
		return fmt.Errorf("contained candidate did not terminate after %s", stopTimeout)
	}
	m.clear(command)
	if err := m.runStopCommand(ctx); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	return nil
}

func (m *ManagedCandidate) runStopCommand(ctx context.Context) error {
	return m.runCommand(ctx, m.stopCommand, "stop")
}

func (m *ManagedCandidate) runCommand(ctx context.Context, arguments []string, name string) error {
	if len(arguments) == 0 {
		return nil
	}
	command := exec.CommandContext(ctx, arguments[0], arguments[1:]...)
	command.Dir = m.cwd
	command.Env = environmentWithOverrides(os.Environ(), m.env)
	output, err := command.CombinedOutput()
	if err != nil {
		return fmt.Errorf(
			"managed candidate %s command failed: %w: %s",
			name,
			err,
			strings.TrimSpace(string(output)),
		)
	}
	return nil
}

func waitForExit(waitDone <-chan error, timeout time.Duration) bool {
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case <-waitDone:
		return true
	case <-timer.C:
		return false
	}
}

func (m *ManagedCandidate) clear(command *exec.Cmd) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.cmd == command {
		m.cmd = nil
		m.waitDone = nil
	}
}

func (m *ManagedCandidate) Close(ctx context.Context) error {
	m.closeOnce.Do(func() {
		stopErr := m.Stop(ctx, false)
		cleanupErr := m.runCommand(ctx, m.cleanupCommand, "cleanup")
		logErr := m.log.Close()
		removeErr := os.Remove(m.logPath)
		m.closeErr = errors.Join(stopErr, cleanupErr, logErr, removeErr)
	})
	return m.closeErr
}

func (m *ManagedCandidate) LogTail(limit int) string {
	if limit <= 0 {
		return ""
	}
	if err := m.log.Sync(); err != nil {
		return fmt.Sprintf("read candidate log: %v", err)
	}
	raw, err := os.ReadFile(m.logPath)
	if err != nil {
		return fmt.Sprintf("read candidate log: %v", err)
	}
	if len(raw) > limit {
		raw = raw[len(raw)-limit:]
	}
	return string(raw)
}
