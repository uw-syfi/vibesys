// Package protocol holds the revision of OrchestratorService that this build speaks. The proto
// file carries it as file options, so the daemon, this module's client and the desktop app read the
// same number from generated code instead of each writing it down.
package protocol

import (
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
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
