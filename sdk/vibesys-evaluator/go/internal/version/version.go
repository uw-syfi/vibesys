// Package version is loadgen's version, the one version of vibesys-evaluator, and what this build
// implements.
package version

import (
	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/files"
)

// Version is set at release (-ldflags "-X .../internal/version.Version=X.Y.Z").
var Version = "0.1.0-dev"

// Writer names loadgen in the FileHeader of the files it writes.
func Writer() string { return "loadgen " + Version }

// Capabilities is what this build implements.
func Capabilities() *pb.Capabilities {
	return &pb.Capabilities{
		LoadgenVersion:   Version,
		MinSchemaVersion: files.SchemaVersion,
		MaxSchemaVersion: files.SchemaVersion,
		Calls:            []pb.CallKind{pb.CallKind_CALL_KIND_HTTP},
		Models:           []pb.LoadModel{pb.LoadModel_LOAD_MODEL_CLOSED, pb.LoadModel_LOAD_MODEL_OPEN},
		Arrivals:         []pb.Arrivals{pb.Arrivals_ARRIVALS_UNIFORM, pb.Arrivals_ARRIVALS_POISSON},
		ExclusiveKeys:    true,
		RequestStream:    true,
		RefEncodings: []pb.RefEncoding{
			pb.RefEncoding_REF_ENCODING_RAW, pb.RefEncoding_REF_ENCODING_JSON_STRING, pb.RefEncoding_REF_ENCODING_JSON_VALUE,
		},
	}
}
