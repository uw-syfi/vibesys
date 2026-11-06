package main

import (
	"google.golang.org/protobuf/encoding/protojson"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/version"
)

func init() {
	register("core", "version", coreVersion)
}

// coreVersion prints what this build implements (the loadgen Capabilities message, which
// carries the one version of vibesys-evaluator).
func coreVersion(io IO) error {
	out, err := protojson.MarshalOptions{UseProtoNames: true}.Marshal(version.Capabilities())
	if err != nil {
		return err
	}
	_, err = io.Stdout.Write(append(out, '\n'))
	return err
}
