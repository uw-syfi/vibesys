// Package protocol holds the revision of OrchestratorService that this build speaks. The proto
// file carries it as file options, so the daemon, this module's client and the desktop app read the
// same number from generated code instead of each writing it down.
package protocol

import (
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// The metadata an operator's client puts on its calls to say which install of the client is calling:
// an id that stays the same for the install, and a name to show. Together they are the core's Viewer, a
// label and nothing more: the daemon authenticates neither and grants nothing for them. Both or neither
// are sent, and a call that asks for a catch-up needs both.
const (
	MetaViewerID    = "vibesys-viewer-id"
	MetaViewerLabel = "vibesys-viewer-label"
)

var (
	// Version rises only when a change breaks a peer.
	Version = option(pb.E_ProtocolVersion)
	// MinVersion is the oldest revision this build still serves.
	MinVersion = option(pb.E_MinProtocolVersion)
)

func option(ext protoreflect.ExtensionType) uint32 {
	return proto.GetExtension(pb.File_vibesys_orchestrator_v1_service_proto.Options(), ext).(uint32)
}
