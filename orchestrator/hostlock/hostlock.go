// Package hostlock is the record a host process keeps in a lock file it holds for its whole life:
// a HostLock message, as protobuf JSON. The writers are the host (for its gateway and for its state
// directory) and the reader is whoever must find the process a held lock belongs to.
package hostlock

import (
	"fmt"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// Marshal is the record of the process pid running release.
func Marshal(pid int, release string) ([]byte, error) {
	if pid <= 0 {
		return nil, fmt.Errorf("a host lock names a process, and %d is not one", pid)
	}
	lock := &pb.HostLock{Pid: uint32(pid), Release: release}
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
