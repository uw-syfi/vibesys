package host

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// maxSocketPath is the longest Unix socket path every supported system accepts, with room for
// the terminating byte. macOS allows 104 bytes and Linux 108.
const maxSocketPath = 100

// The files of a run directory.
const (
	launchFile = "launch.pb"
	socketFile = "s"
	spoolFile  = "out"
	stderrFile = "err"
	pidFile    = "pid"
	// aliveFile is what a supervisor holds a lock on for as long as it lives. The system drops the
	// lock when the process ends, however it ends, so a lock that can be taken is the proof that
	// the supervisor is dead, which a socket that does not answer is not.
	aliveFile = "alive"
)

// runDir is one job's directory.
type runDir string

func (d runDir) launch() string { return filepath.Join(string(d), launchFile) }
func (d runDir) socket() string { return filepath.Join(string(d), socketFile) }
func (d runDir) spool() string  { return filepath.Join(string(d), spoolFile) }
func (d runDir) stderr() string { return filepath.Join(string(d), stderrFile) }
func (d runDir) pid() string    { return filepath.Join(string(d), pidFile) }
func (d runDir) alive() string  { return filepath.Join(string(d), aliveFile) }

// dirName is the name of a job's run directory.
func dirName(ref lifecycle.Ref) string { return ref.String() }

// refOf is the job a run directory name stands for, and false for a name no launch makes.
func refOf(name string) (lifecycle.Ref, bool) {
	store, job, ok := strings.Cut(name, "-")
	if !ok || store == "" || job == "" {
		return lifecycle.Ref{}, false
	}
	return lifecycle.Ref{Store: lifecycle.StoreID(store), Job: core.JobID(job)}, true
}

// proveDead takes the lock of a run's supervisor, which proves it is dead and keeps a process that
// was started and has not come up yet from going on while the caller holds the lock. It fails with
// runtimedir.ErrHeld for a supervisor that may live.
func proveDead(dir runDir) (*runtimedir.Lock, error) { return runtimedir.TryLock(dir.alive()) }

// checkSocketPath refuses a run directory whose socket path a system would cut short.
func checkSocketPath(dir runDir) error {
	if n := len(dir.socket()); n > maxSocketPath {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
			"the supervisor socket path %q is %d bytes, over the %d a Unix socket allows; use a shorter runtime directory",
			dir.socket(), n, maxSocketPath)}
	}
	return nil
}

// writeLaunch writes the launch into the directory, whole or not at all.
func writeLaunch(dir runDir, l lifecycle.Launch) error {
	data, err := proto.Marshal(wire.Launch(l))
	if err != nil {
		return err
	}
	tmp := dir.launch() + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, dir.launch())
}

// readLaunch reads the launch back, validated as a supervisor trusts nothing it is handed.
func readLaunch(dir runDir) (lifecycle.Launch, error) {
	data, err := os.ReadFile(dir.launch())
	if err != nil {
		return lifecycle.Launch{}, err
	}
	var msg cliv1.Launch
	if err := proto.Unmarshal(data, &msg); err != nil {
		return lifecycle.Launch{}, fmt.Errorf("launch file: %w", err)
	}
	return wire.ToLaunch(&msg)
}

// RecordsDir is where the run records of one daemon are, under a state directory that outlives
// the runtime directory. A home directory can be shared between machines and hold several runtime
// directories, each with its own daemon and its own runs.
func RecordsDir(state, machine, runtimeDir string) string {
	sum := sha256.Sum256([]byte(machine + "\x00" + runtimeDir))
	return filepath.Join(state, "run-records", hex.EncodeToString(sum[:6]))
}

// record is what is known of a run outside its directory.
type record struct {
	BootID string
	Launch lifecycle.Launch
}

func (h *Host) recordPath(ref lifecycle.Ref) string { return filepath.Join(h.records, dirName(ref)) }

// writeRecord writes the record of a run, whole or not at all, before anything of the run exists:
// a run with no record is a run that never was.
func (h *Host) writeRecord(ref lifecycle.Ref, l lifecycle.Launch) error {
	boot, err := h.bootID()
	if err != nil {
		return fmt.Errorf("reading the boot id: %w", err)
	}
	data, err := proto.Marshal(&cliv1.RunRecord{BootId: boot, Launch: wire.Launch(l)})
	if err != nil {
		return err
	}
	if err := os.MkdirAll(h.records, 0o700); err != nil {
		return err
	}
	path := h.recordPath(ref)
	if err := os.WriteFile(path+".tmp", data, 0o600); err != nil {
		return err
	}
	return os.Rename(path+".tmp", path)
}

// readRecord returns the record of a run, and false when there is none.
func (h *Host) readRecord(ref lifecycle.Ref) (record, bool, error) {
	data, err := os.ReadFile(h.recordPath(ref))
	if errors.Is(err, fs.ErrNotExist) {
		return record{}, false, nil
	}
	if err != nil {
		return record{}, false, err
	}
	var msg cliv1.RunRecord
	if err := proto.Unmarshal(data, &msg); err != nil {
		return record{}, false, fmt.Errorf("run record: %w", err)
	}
	launch, err := wire.ToLaunch(msg.GetLaunch())
	if err != nil {
		return record{}, false, err
	}
	if launch.Ref() != ref {
		return record{}, false, fmt.Errorf("run record of job %s is for job %s", ref, launch.Ref())
	}
	return record{BootID: msg.GetBootId(), Launch: launch}, true, nil
}

// removeRecord removes the record of a run. It is idempotent.
func (h *Host) removeRecord(ref lifecycle.Ref) error {
	if err := os.Remove(h.recordPath(ref)); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	return nil
}

// recordedRefs lists the runs with a record.
func (h *Host) recordedRefs() ([]lifecycle.Ref, error) {
	entries, err := os.ReadDir(h.records)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var out []lifecycle.Ref
	for _, e := range entries {
		if ref, ok := refOf(e.Name()); ok && !strings.HasSuffix(e.Name(), ".tmp") {
			out = append(out, ref)
		}
	}
	return out, nil
}
