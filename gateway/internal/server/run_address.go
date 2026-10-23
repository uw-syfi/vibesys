package server

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"buf.build/go/protovalidate"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/protobuf/proto"
)

// readRunProject consumes Project's address publication, independently of head readability.
// A damaged publication is rejected; the observation boundary recovers a missing one.
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

// project reads routing facts once per immutable committed prefix when an older
// publisher left no address sidecar. No run data or index entry is rewritten.
func (o *headObserver) project(root, id, dir string, head *storev1.RunHead) (string, error) {
	project, err := readRunProject(root, id, dir)
	if err == nil || !errors.Is(err, os.ErrNotExist) {
		return project, err
	}
	f, err := openRegular(dir, "run.log")
	if err != nil {
		return "", err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return "", err
	}
	if head == nil {
		head = &storev1.RunHead{}
	}
	facts, err := o.recovered(f, info, head)
	if err != nil {
		return "", err
	}
	if facts.project == "" {
		return "", os.ErrNotExist
	}
	if err := protovalidate.Validate(&apiv1.WatchRunsResponse{RunId: id, ProjectRoot: proto.String(facts.project)}); err != nil {
		return "", err
	}
	return facts.project, nil
}
