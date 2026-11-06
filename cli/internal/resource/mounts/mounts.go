// Package mounts is the resource of read-only directories: by name, a directory of the engine's
// machine that every container sees at Dir/<name>, with the names in the job environment (JobEnvironment.mounts). It is
// what [placements.<name>.resources.mounts] configures, and what the [docker.mounts] section of the
// earlier operator config shape did.
package mounts

import (
	"context"
	"fmt"
	"maps"
	"regexp"
	"slices"
	"strings"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
)

const (
	// Dir holds the mounts in the container, one directory per name.
	Dir = "/vibesys/mounts"
	// Fact is the fingerprint fact: the names of the mounts.
	Fact = "mounts"
)

// name is the form of a mount's name: a path element and part of a variable name.
var name = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)

// Mounts is the resource. Create it with New.
type Mounts struct {
	dirs  map[string]string
	names []string
}

var _ resource.Resource = (*Mounts)(nil)

// New returns the resource of the directories, by name. It fails with core.FailureInvalidInput
// naming a name a mount cannot have, a directory that is not an absolute path, or no directory.
func New(dirs map[string]string) (*Mounts, error) {
	if len(dirs) == 0 {
		return nil, invalid("no directory to mount")
	}
	for n, dir := range dirs {
		if !name.MatchString(n) {
			return nil, invalid("%q is not a mount name (lower-case letters, digits, '_' and '-', starting with a letter or digit)", n)
		}
		if err := resource.CheckPath("the directory of "+n, dir); err != nil {
			return nil, invalid("%s: %v", n, core.AsFailure(err).Message)
		}
	}
	return &Mounts{dirs: maps.Clone(dirs), names: slices.Sorted(maps.Keys(dirs))}, nil
}

// Kind implements resource.Resource.
func (*Mounts) Kind() core.ResourceKind { return core.ResourceMounts }

// Claims implements resource.Resource. Read-only directories are shared by everyone, so a job
// holds no claim on them.
func (*Mounts) Claims() []core.Claim { return nil }

// Fingerprint implements resource.Resource: the names, which are what a task reads. The directories
// are not part of it: where a machine keeps a file is a fact of the machine, and a task checks
// the digest of what it reads from a mount.
func (m *Mounts) Fingerprint(ctx context.Context) (map[string]string, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return map[string]string{Fact: strings.Join(m.names, ",")}, nil
}

// Attach implements resource.Resource: each directory, read-only, at Dir/<name>.
func (m *Mounts) Attach(ctx context.Context) (resource.Attachment, error) {
	if err := ctx.Err(); err != nil {
		return resource.Attachment{}, err
	}
	// Only the mounts are this resource's to state; the rest of the job environment is the run's.
	job, err := envcodec.Encode(&cliv1.JobEnvironment{Mounts: m.names})
	if err != nil {
		return resource.Attachment{}, invalid("the names of the mounts are not a valid job environment: %v", err)
	}
	a := resource.Attachment{Env: job.Set}
	for _, n := range m.names {
		a.Mounts = append(a.Mounts, resource.Mount{Host: m.dirs[n], Container: Dir + "/" + n, ReadOnly: true})
	}
	return a, nil
}

func invalid(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: "mounts: " + fmt.Sprintf(format, args...)}
}
