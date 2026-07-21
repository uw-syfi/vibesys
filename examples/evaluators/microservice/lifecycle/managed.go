package lifecycle

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	processPollInterval = 20 * time.Millisecond
	stopTimeout         = 5 * time.Second
)

// ManagedCandidate starts a candidate in its own process group and retains a
// continuously sampled set of descendants. Retaining descendants matters when
// a launcher daemonizes a service into a different session before a crash.
type ManagedCandidate struct {
	command []string
	cwd     string
	env     map[string]string

	mu        sync.Mutex
	cmd       *exec.Cmd
	waitDone  chan error
	monitor   chan struct{}
	tracked   map[int]struct{}
	log       *os.File
	logPath   string
	closeOnce sync.Once
	closeErr  error
}

func NewManagedCandidate(
	command []string,
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
	log, err := os.CreateTemp("", "microservice-candidate-*.log")
	if err != nil {
		return nil, fmt.Errorf("create candidate log: %w", err)
	}
	return &ManagedCandidate{
		command: append([]string(nil), command...),
		cwd:     absCWD,
		env:     cloneEnvironment(environment),
		tracked: make(map[int]struct{}),
		log:     log,
		logPath: log.Name(),
	}, nil
}

func (m *ManagedCandidate) Prepare(ctx context.Context) error {
	return m.Start(ctx)
}

func (m *ManagedCandidate) Start(_ context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.cmd != nil {
		return fmt.Errorf("candidate is already running")
	}
	command := exec.Command(m.command[0], m.command[1:]...)
	command.Dir = m.cwd
	command.Env = os.Environ()
	for name, value := range m.env {
		command.Env = append(command.Env, name+"="+value)
	}
	command.Stdout = m.log
	command.Stderr = m.log
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := command.Start(); err != nil {
		return fmt.Errorf("start candidate: %w", err)
	}
	m.cmd = command
	m.waitDone = make(chan error, 1)
	m.monitor = make(chan struct{})
	m.tracked = map[int]struct{}{command.Process.Pid: {}}
	go func() { m.waitDone <- command.Wait() }()
	go m.monitorDescendants(command.Process.Pid, m.monitor)
	return nil
}

func cloneEnvironment(environment map[string]string) map[string]string {
	cloned := make(map[string]string, len(environment))
	for name, value := range environment {
		cloned[name] = value
	}
	return cloned
}

func (m *ManagedCandidate) Stop(ctx context.Context, hard bool) error {
	m.mu.Lock()
	command := m.cmd
	if command == nil {
		m.mu.Unlock()
		return nil
	}
	monitor := m.monitor
	if monitor != nil {
		close(monitor)
		m.monitor = nil
	}
	rootPID := command.Process.Pid
	waitDone := m.waitDone
	m.mu.Unlock()

	m.captureDescendants(rootPID)
	signal := syscall.SIGTERM
	if hard {
		signal = syscall.SIGKILL
	}
	m.signalTracked(rootPID, signal)
	if !hard {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-waitDone:
		case <-time.After(stopTimeout):
			m.signalTracked(rootPID, syscall.SIGKILL)
		}
	} else {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-waitDone:
		case <-time.After(stopTimeout):
		}
	}
	if err := m.waitTerminated(ctx, rootPID, stopTimeout); err != nil {
		m.signalTracked(rootPID, syscall.SIGKILL)
		if secondErr := m.waitTerminated(ctx, rootPID, stopTimeout); secondErr != nil {
			return fmt.Errorf("%w; after SIGKILL: %v", err, secondErr)
		}
	}
	m.mu.Lock()
	if m.cmd == command {
		m.cmd = nil
		m.waitDone = nil
	}
	m.mu.Unlock()
	return nil
}

func (m *ManagedCandidate) Close(ctx context.Context) error {
	m.closeOnce.Do(func() {
		stopErr := m.Stop(ctx, false)
		logErr := m.log.Close()
		removeErr := os.Remove(m.logPath)
		m.closeErr = errors.Join(stopErr, logErr, removeErr)
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

func (m *ManagedCandidate) monitorDescendants(rootPID int, stop <-chan struct{}) {
	ticker := time.NewTicker(processPollInterval)
	defer ticker.Stop()
	for {
		m.captureDescendants(rootPID)
		select {
		case <-stop:
			m.captureDescendants(rootPID)
			return
		case <-ticker.C:
		}
	}
}

func (m *ManagedCandidate) captureDescendants(rootPID int) {
	parents, err := processParents()
	if err != nil {
		return
	}
	descendants := descendantsOf(rootPID, parents)
	m.mu.Lock()
	for _, pid := range descendants {
		m.tracked[pid] = struct{}{}
	}
	m.mu.Unlock()
}

func (m *ManagedCandidate) signalTracked(rootPID int, signal syscall.Signal) {
	_ = syscall.Kill(-rootPID, signal)
	m.mu.Lock()
	tracked := make([]int, 0, len(m.tracked))
	for pid := range m.tracked {
		tracked = append(tracked, pid)
	}
	m.mu.Unlock()
	for _, pid := range tracked {
		_ = syscall.Kill(pid, signal)
	}
}

func (m *ManagedCandidate) waitTerminated(
	ctx context.Context,
	rootPID int,
	timeout time.Duration,
) error {
	deadline := time.Now().Add(timeout)
	for {
		alive := make([]int, 0)
		if processExists(-rootPID) {
			alive = append(alive, -rootPID)
		}
		m.mu.Lock()
		for pid := range m.tracked {
			if processExists(pid) {
				alive = append(alive, pid)
			}
		}
		m.mu.Unlock()
		if len(alive) == 0 {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("candidate processes did not terminate: %v", alive)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(25 * time.Millisecond):
		}
	}
}

func processExists(pid int) bool {
	err := syscall.Kill(pid, 0)
	return err == nil || !errors.Is(err, syscall.ESRCH)
}

func processParents() (map[int]int, error) {
	command := exec.Command("ps", "-e", "-o", "pid=,ppid=")
	output, err := command.Output()
	if err != nil {
		return nil, fmt.Errorf("enumerate processes: %w", err)
	}
	parents := make(map[int]int)
	scanner := bufio.NewScanner(strings.NewReader(string(output)))
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) != 2 {
			continue
		}
		pid, pidErr := strconv.Atoi(fields[0])
		parent, parentErr := strconv.Atoi(fields[1])
		if pidErr == nil && parentErr == nil {
			parents[pid] = parent
		}
	}
	return parents, scanner.Err()
}

func descendantsOf(root int, parents map[int]int) []int {
	descendants := make([]int, 0)
	seen := map[int]struct{}{root: {}}
	changed := true
	for changed {
		changed = false
		for pid, parent := range parents {
			if _, already := seen[pid]; already {
				continue
			}
			if _, connected := seen[parent]; connected {
				seen[pid] = struct{}{}
				descendants = append(descendants, pid)
				changed = true
			}
		}
	}
	return descendants
}
