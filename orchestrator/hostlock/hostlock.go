// Package hostlock is the record a host process keeps in a lock file it holds for its whole life:
// a HostLock message, as protobuf JSON. The writers are the host (for its gateway and for its state
// directory) and the reader is whoever must find the process a held lock belongs to.
package hostlock

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// Marshal is the record of the process pid running release, the host of root, on machine.
func Marshal(pid int, release, root, machine string) ([]byte, error) {
	if pid <= 0 {
		return nil, fmt.Errorf("a host lock names a process, and %d is not one", pid)
	}
	lock := &pb.HostLock{Pid: uint32(pid), Release: release, Root: root, Machine: machine}
	if err := protovalidate.Validate(lock); err != nil {
		return nil, fmt.Errorf("not a valid host lock: %w", err)
	}
	return protojson.Marshal(lock)
}

// Parse reads a record, refusing a field it does not know and any value its rules reject.
func Parse(data []byte) (*pb.HostLock, error) {
	var lock pb.HostLock
	if err := protojson.Unmarshal(data, &lock); err != nil {
		return nil, fmt.Errorf("not a host lock: %w", err)
	}
	if err := protovalidate.Validate(&lock); err != nil {
		return nil, fmt.Errorf("not a valid host lock: %w", err)
	}
	return &lock, nil
}

// ThisMachine is the name a record of this machine carries, or empty when the system gives none. An
// empty name is read as this machine, as a record older than the field is.
func ThisMachine() string {
	name, err := os.Hostname()
	if err != nil {
		return ""
	}
	return name
}

// Elsewhere says the record belongs to a process on another machine, which can be neither reached nor
// signalled from this one. Two machines of one name cannot be told apart, and a record without a name
// is taken to be from here.
func Elsewhere(lock *pb.HostLock, machine string) bool {
	return lock.GetMachine() != "" && machine != "" && lock.GetMachine() != machine
}

const (
	// StateLockFile is the file in a state directory whose lock its one holder keeps.
	StateLockFile = "LOCK"
	// StateRecordFile is the HostLock its holder writes beside it, while holding the lock.
	StateRecordFile = "holder"
)

// StateHolder says who holds the state directory dir now. held is false when nobody does, whatever
// the record says; lock is nil when somebody does and their record cannot be read. The probe takes and
// releases a shared lock when the lock is free.
func StateHolder(dir string) (lock *pb.HostLock, held bool, err error) {
	file, err := os.Open(filepath.Join(dir, StateLockFile))
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	defer file.Close()
	switch err := syscall.Flock(int(file.Fd()), syscall.LOCK_SH|syscall.LOCK_NB); {
	case err == nil:
		return nil, false, nil
	case !errors.Is(err, syscall.EWOULDBLOCK):
		return nil, false, err
	}
	data, err := os.ReadFile(filepath.Join(dir, StateRecordFile))
	if err != nil {
		return nil, true, nil
	}
	lock, err = Parse(data)
	if err != nil {
		return nil, true, nil
	}
	return lock, true, nil
}
