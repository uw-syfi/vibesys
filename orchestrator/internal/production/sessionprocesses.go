package production

import (
	"errors"
	"fmt"
	"os"
	"syscall"
	"time"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// A session's process is alive exactly while it holds the exclusive lock on its place's lock file:
// the kernel frees the lock when the process ends, however it ends, and a pid or a file cannot say
// that. So the rules here are the whole of how the host treats a process it did not start itself.
//
//   - A free lock means no process, whatever any record says. The groups the record names are ended
//     and the record is removed, while the host holds the lock, so no new process starts under it.
//   - A held lock means a process, and the record's pid is its own: the process wrote the record
//     while it held the lock, and the host removes a stale record before it starts a process. So a pid
//     the system has since given to another program is never signalled, because that only happens
//     once the lock is free.
//   - A record that cannot be read names nothing: it is reported with its path, and nothing is signalled.

// ProcessSignals is how the host ends processes. OSSignals is the implementation; a test passes one
// that records what it was asked to end.
type ProcessSignals interface {
	// Kill ends one process. One that is already gone is not an error.
	Kill(pid int) error
	// KillGroup ends every process of a group. A group already gone is not an error.
	KillGroup(pgid int) error
}

// OSSignals is ProcessSignals over the operating system.
type OSSignals struct{}

// Kill implements ProcessSignals.
func (OSSignals) Kill(pid int) error { return ignoreGone(syscall.Kill(pid, syscall.SIGKILL)) }

// KillGroup implements ProcessSignals.
func (OSSignals) KillGroup(pgid int) error { return ignoreGone(syscall.Kill(-pgid, syscall.SIGKILL)) }

func ignoreGone(err error) error {
	if errors.Is(err, syscall.ESRCH) {
		return nil
	}
	return err
}

// ProcessRecordError says the record of a place's processes cannot be used. The record is removed
// when this is reported, so the next look finds none, but nothing was signalled for it.
type ProcessRecordError struct {
	Path string
	Err  error
}

func (e *ProcessRecordError) Error() string {
	return fmt.Sprintf("the record of the session's processes at %s cannot be used, so none was ended: %v", e.Path, e.Err)
}

func (e *ProcessRecordError) Unwrap() error { return e.Err }

// ErrHolderLives says a process holds a place, so no other can be started there.
var ErrHolderLives = errors.New("a process already holds this session")

// holderLock is what the host's try at a place's lock found.
type holderLock struct {
	// held says another process holds the lock: a holder lives.
	held bool
	// file is the lock, taken by this try, when it was free. The caller closes it, which frees the
	// lock again. It is nil when the lock is held, and when there is no lock file: nothing ever held
	// the place.
	file *os.File
}

func (l holderLock) release() {
	if l.file != nil {
		_ = l.file.Close()
	}
}

// tryLock takes the lock at path without waiting, and says so, or says that another process holds it.
// It never creates the file: only a holder does.
func tryLock(path string) (holderLock, error) {
	file, err := os.OpenFile(path, os.O_RDWR, 0)
	if errors.Is(err, os.ErrNotExist) {
		return holderLock{}, nil
	}
	if err != nil {
		return holderLock{}, fmt.Errorf("open the session's lock: %w", err)
	}
	switch err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); {
	case err == nil:
		return holderLock{file: file}, nil
	case errors.Is(err, syscall.EWOULDBLOCK):
		_ = file.Close()
		return holderLock{held: true}, nil
	default:
		_ = file.Close()
		return holderLock{}, fmt.Errorf("try the session's lock %s: %w", path, err)
	}
}

// readProcesses reads the record at path. A record that is not there is nil with no error.
func readProcesses(path string) (*pb.SessionProcesses, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, &ProcessRecordError{Path: path, Err: err}
	}
	var record pb.SessionProcesses
	if err := proto.Unmarshal(data, &record); err != nil {
		return nil, &ProcessRecordError{Path: path, Err: err}
	}
	if err := protovalidate.Validate(&record); err != nil {
		return nil, &ProcessRecordError{Path: path, Err: err}
	}
	return &record, nil
}

// alive says a holder lives at a place. A lock the host cannot try is taken to be held: nothing is
// declared gone, or signalled, on a guess.
func alive(place HolderPlace) bool {
	lock, err := tryLock(place.LockFile())
	lock.release()
	return err != nil || lock.held
}

// reapGone ends the agent processes a gone holder recorded, and removes the record and the socket
// name it left. The caller holds the place's lock, so the holder is gone and no new one starts.
func reapGone(place HolderPlace, signals ProcessSignals) error {
	record, err := readProcesses(place.ProcessesFile())
	// Removed whatever it held, so that a record nothing can use is reported once and not forever.
	for _, stale := range []string{place.ProcessesFile(), place.Socket} {
		_ = os.Remove(stale)
	}
	if err != nil || record == nil {
		return err
	}
	var errs []error
	for _, pgid := range record.GetAgentProcessGroups() {
		errs = append(errs, signals.KillGroup(int(pgid)))
	}
	return errors.Join(errs...)
}

// reapUnheld does reapGone for a place nothing holds. A place a holder lives at is left alone: its
// agents are its own.
func reapUnheld(place HolderPlace, signals ProcessSignals) error {
	lock, err := tryLock(place.LockFile())
	if err != nil || lock.held {
		return err
	}
	defer lock.release()
	return reapGone(place, signals)
}

// killHolder ends the holder at a place and its agents. The pid it signals is read while the lock is
// held, so it is the holder's own; the agents are reaped once the lock is free, from the record as
// the holder last wrote it.
func killHolder(place HolderPlace, signals ProcessSignals) error {
	lock, err := tryLock(place.LockFile())
	if err != nil {
		return err
	}
	if lock.held {
		record, err := readProcesses(place.ProcessesFile())
		if err != nil {
			return fmt.Errorf("end the session's process: %w", err)
		}
		if record == nil {
			return fmt.Errorf("the session's process holds %s and has not recorded its pid yet", place.LockFile())
		}
		if err := signals.Kill(int(record.GetHolderPid())); err != nil {
			return fmt.Errorf("end the session's process %d: %w", record.GetHolderPid(), err)
		}
		if lock, err = awaitFree(place.LockFile()); err != nil {
			return err
		}
	}
	defer lock.release()
	return reapGone(place, signals)
}

// freeBound is how long a process that was sent SIGKILL has to give up its lock. The kernel frees it
// as the process ends, so this only guards against a lock that is never free.
const (
	freeBound = 10 * time.Second
	freeTick  = 5 * time.Millisecond
)

func awaitFree(path string) (holderLock, error) {
	for waited := time.Duration(0); ; waited += freeTick {
		lock, err := tryLock(path)
		if err != nil || !lock.held {
			return lock, err
		}
		if waited >= freeBound {
			return holderLock{}, fmt.Errorf("the session's process still holds %s %s after it was killed", path, freeBound)
		}
		time.Sleep(freeTick)
	}
}

// prepareStart checks no holder lives at a place, and ends what a gone one left, so that a process
// started now starts clean.
func prepareStart(place HolderPlace, signals ProcessSignals) error {
	lock, err := tryLock(place.LockFile())
	if err != nil {
		return err
	}
	if lock.held {
		return fmt.Errorf("%w: %s is locked", ErrHolderLives, place.LockFile())
	}
	defer lock.release()
	return reapGone(place, signals)
}
