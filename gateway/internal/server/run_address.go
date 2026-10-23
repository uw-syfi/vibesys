package server

import (
	"fmt"
	"path/filepath"

	"buf.build/go/protovalidate"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/protobuf/proto"
)

// readRunProject consumes Project's address publication, independently of head readability.
// A missing/damaged address stays absent on the wire, never an empty project directory.
func readRunProject(root, id, dir string) (string, error) {
	data, err := readFile(root, filepath.Join("run-addresses", id+".pb"), 1<<18)
	if err != nil {
		return "", err
	}
	address := &storev1.RunAddress{}
	if err := proto.Unmarshal(data, address); err != nil {
		return "", err
	}
	if err := protovalidate.Validate(address); err != nil {
		return "", err
	}
	if address.LogDirectory != dir {
		return "", fmt.Errorf("run address target differs from index: %s", id)
	}
	if err := protovalidate.Validate(&apiv1.WatchRunsResponse{RunId: id, ProjectRoot: proto.String(address.ProjectRoot)}); err != nil {
		return "", err
	}
	return address.ProjectRoot, nil
}
