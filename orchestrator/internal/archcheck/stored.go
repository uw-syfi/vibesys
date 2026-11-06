package archcheck

import (
	"fmt"
	"slices"
	"strings"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// StoredFile is the file of what the host keeps and no client receives.
const StoredFile = "vibesys/orchestrator/v1/stored.proto"

// StoredLeaks walks the messages every service of files sends or receives, transitively, and names
// each path that reaches a message of StoredFile or a field marked stored_only. A stored fact cannot
// reach a client over a message that cannot hold it, so a service whose messages pass this check has
// nothing to strip. It reads descriptors and not names, so a new field, oneof case or nested message
// is held as soon as it is declared.
func StoredLeaks(files []protoreflect.FileDescriptor) []string {
	var found []string
	for _, file := range files {
		services := file.Services()
		for i := range services.Len() {
			methods := services.Get(i).Methods()
			for j := range methods.Len() {
				method := methods.Get(j)
				for _, m := range []protoreflect.MessageDescriptor{method.Input(), method.Output()} {
					walkStored(m, string(method.FullName()), map[protoreflect.FullName]bool{}, &found)
				}
			}
		}
	}
	slices.Sort(found)
	return slices.Compact(found)
}

func walkStored(m protoreflect.MessageDescriptor, path string, seen map[protoreflect.FullName]bool, found *[]string) {
	if seen[m.FullName()] {
		return
	}
	seen[m.FullName()] = true
	if m.ParentFile().Path() == StoredFile {
		*found = append(*found, fmt.Sprintf("%s reaches %s, which is stored and no client may receive: send its public message (docs/contributing/process-boundaries.md, What clients see)", path, m.FullName()))
		return
	}
	fields := m.Fields()
	for i := range fields.Len() {
		fd := fields.Get(i)
		here := strings.Join([]string{path, string(fd.Name())}, ".")
		if proto.GetExtension(fd.Options(), pb.E_StoredOnly).(bool) {
			*found = append(*found, fmt.Sprintf("%s is marked stored_only and a service's message holds it: move it to the record in %s (docs/contributing/process-boundaries.md, What clients see)", here, StoredFile))
			continue
		}
		if next := fd.Message(); next != nil {
			walkStored(next, here, seen, found)
		}
	}
}
