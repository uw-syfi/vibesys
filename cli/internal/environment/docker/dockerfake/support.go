package dockerfake

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"path"
	"strings"
	"sync/atomic"

	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
)

// Source is a docker.Source made of a function.
type Source func(ctx context.Context, target environment.Target) (docker.Spec, error)

var _ docker.Source = Source(nil)

// Spec implements docker.Source.
func (s Source) Spec(ctx context.Context, target environment.Target) (docker.Spec, error) {
	return s(ctx, target)
}

// Fixed is a Source that gives every target the same spec.
func Fixed(spec docker.Spec) Source {
	return func(context.Context, environment.Target) (docker.Spec, error) { return spec, nil }
}

// IDs is a docker.IDs that counts. Two IDs with different prefixes never return the same id, as
// two processes' ids never do.
type IDs struct {
	prefix uint32
	n      atomic.Uint64
}

var _ docker.IDs = (*IDs)(nil)

// NewIDs returns ids made of the process number and a count.
func NewIDs(process uint32) *IDs { return &IDs{prefix: process} }

// New implements docker.IDs.
func (i *IDs) New() string { return fmt.Sprintf("%08x%08x", i.prefix, i.n.Add(1)) }

// fromRefs returns the reference of each plain "FROM <ref>" line of the Dockerfile at the root of
// a build context. The fake reads only that form, apart from the environment's own reading of a
// Dockerfile, so a test of what the environment reads does not test it against itself.
func fromRefs(buildContext []byte) []string {
	r := tar.NewReader(bytes.NewReader(buildContext))
	for {
		h, err := r.Next()
		if err != nil {
			return nil
		}
		if h.Name != "Dockerfile" {
			continue
		}
		text, err := io.ReadAll(r)
		if err != nil {
			return nil
		}
		var refs []string
		for _, line := range strings.Split(string(text), "\n") {
			if f := strings.Fields(line); len(f) >= 2 && strings.EqualFold(f[0], "FROM") {
				refs = append(refs, f[1])
			}
		}
		return refs
	}
}

// copiedOf returns the Dockerfile at the root of a build context and each file of the context that
// one of its plain COPY or ADD instructions copies, by name and content, in the context's order.
// A source is a file, a directory, everything ("."), or a pattern of path.Match; a COPY --from
// copies from another stage, not the context.
func copiedOf(buildContext []byte) []byte {
	type file struct {
		name string
		data []byte
	}
	var files []file
	var dockerfile []byte
	r := tar.NewReader(bytes.NewReader(buildContext))
	for {
		h, err := r.Next()
		if err != nil {
			break
		}
		data, err := io.ReadAll(r)
		if err != nil {
			break
		}
		if h.Name == "Dockerfile" {
			dockerfile = data
		}
		files = append(files, file{h.Name, data})
	}
	var sources []string
	for _, line := range strings.Split(string(dockerfile), "\n") {
		f := strings.Fields(line)
		if len(f) < 3 || !(strings.EqualFold(f[0], "COPY") || strings.EqualFold(f[0], "ADD")) {
			continue
		}
		args, fromStage := f[1:], false
		for len(args) > 0 && strings.HasPrefix(args[0], "--") {
			fromStage = fromStage || strings.HasPrefix(args[0], "--from")
			args = args[1:]
		}
		if rest := strings.Join(args, " "); strings.HasPrefix(rest, "[") {
			args = nil
			_ = json.Unmarshal([]byte(rest), &args)
		}
		if fromStage || len(args) < 2 {
			continue
		}
		sources = append(sources, args[:len(args)-1]...)
	}
	out := append([]byte("Dockerfile\n"), dockerfile...)
	for _, f := range files {
		for _, src := range sources {
			src = path.Clean(strings.TrimPrefix(src, "/"))
			matched, _ := path.Match(src, f.name)
			if src == "." || f.name == src || strings.HasPrefix(f.name, src+"/") || matched {
				out = fmt.Appendf(out, "\n%s %d\n", f.name, len(f.data))
				out = append(out, f.data...)
				break
			}
		}
	}
	return out
}
