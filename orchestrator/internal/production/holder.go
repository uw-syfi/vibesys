package production

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hostlock"
)

// ErrHeld says another process holds the orchestrator's state directory. The error wrapping it is
// a *HeldError.
var ErrHeld = errors.New("another process holds the orchestrator")

// HeldError is the refusal of a state directory another process holds.
type HeldError struct {
	Dir string
	// Holder is the record the holding process wrote, or nil when there is none to read.
	Holder *pb.HostLock
	// Unreadable says why Holder is nil.
	Unreadable error
}

func (e *HeldError) Error() string {
	if e.Holder == nil {
		return fmt.Sprintf("%v: the holder of %s is unknown: %v", ErrHeld, e.Dir, e.Unreadable)
	}
	who := fmt.Sprintf("pid %d, release %s", e.Holder.GetPid(), e.Holder.GetRelease())
	if e.Holder.GetRoot() != "" {
		who += ", the host of " + e.Holder.GetRoot()
	}
	if e.Holder.GetMachine() != "" {
		who += " on " + e.Holder.GetMachine()
	}
	return fmt.Sprintf("%v: held by %s, in %s", ErrHeld, who, e.Dir)
}

// Is makes a *HeldError an ErrHeld.
func (e *HeldError) Is(target error) bool { return target == ErrHeld }

// writeHolder records this process as the holder of the state directory, as a HostLock. It is
// written only while the store's lock is held, so the file names the holder whenever the lock is
// taken.
func writeHolder(dir, release, root string) error {
	data, err := hostlock.Marshal(os.Getpid(), release, root, hostlock.ThisMachine())
	if err != nil {
		return err
	}
	tmp := filepath.Join(dir, hostlock.StateRecordFile+".tmp")
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return fmt.Errorf("write the holder file: %w", err)
	}
	return os.Rename(tmp, filepath.Join(dir, hostlock.StateRecordFile))
}

// ReadHolder says who holds a state directory, from the record its holder wrote.
func ReadHolder(dir string) *HeldError {
	held := &HeldError{Dir: dir}
	data, err := os.ReadFile(filepath.Join(dir, hostlock.StateRecordFile))
	if err != nil {
		held.Unreadable = err
		return held
	}
	if held.Holder, err = hostlock.Parse(data); err != nil {
		held.Unreadable = err
	}
	return held
}
