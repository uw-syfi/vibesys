package docker

import (
	"archive/tar"
	"bytes"
	"fmt"
	"maps"
	"slices"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// dockerfileName is where the build finds the Dockerfile in its context.
const dockerfileName = "Dockerfile"

// Context returns the build context of a task's image: a tar of the task's files, with the
// Dockerfile at its root. The same files make the same bytes, whatever the order they were listed
// in, the time, or the user: every entry has the same owner and time, and only the executable bit
// of a mode is kept. It fails with core.FailureInvalidInput for a task with no Dockerfile or a
// path a tar cannot hold.
func Context(spec Spec) ([]byte, error) {
	files := maps.Clone(spec.Files)
	if files == nil {
		files = task.Tree{}
	}
	if spec.Dockerfile != nil {
		files[dockerfileName] = task.File{Data: spec.Dockerfile}
	}
	if len(files[dockerfileName].Data) == 0 {
		return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: "docker: the task has no Dockerfile, or an empty one"}
	}
	var buf bytes.Buffer
	w := tar.NewWriter(&buf)
	for _, name := range slices.Sorted(maps.Keys(files)) {
		f := files[name]
		mode := int64(0o644)
		if f.Executable {
			mode = 0o755
		}
		if err := w.WriteHeader(&tar.Header{
			Typeflag: tar.TypeReg, Name: name, Size: int64(len(f.Data)), Mode: mode,
			ModTime: time.Unix(0, 0), Format: tar.FormatPAX,
		}); err != nil {
			return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("docker: the task's file %q cannot be put in a build context: %v", name, err)}
		}
		if _, err := w.Write(f.Data); err != nil {
			return nil, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("docker: writing the build context: %v", err)}
		}
	}
	if err := w.Close(); err != nil {
		return nil, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("docker: writing the build context: %v", err)}
	}
	return buf.Bytes(), nil
}
