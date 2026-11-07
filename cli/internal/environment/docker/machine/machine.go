// Package machine holds the docker.Machine implementations of the shipped binary: Local, whose
// engine runs on this machine and mounts the caller's own directories, and Remote, whose engine
// runs on another machine reached over ssh, where a run's directories are copies made with rsync.
package machine

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
)

// Local is a machine whose engine shares this machine's files. Its caches are directories of
// CacheRoot.
type Local struct {
	CacheRoot string
	As        docker.User
}

var _ docker.Machine = Local{}

// Stage implements docker.Machine: the directories are mounted where they are.
func (Local) Stage(_ context.Context, d docker.Dirs) (docker.Dirs, error) { return d, nil }

// Collect implements docker.Machine: the commands wrote where the caller reads.
func (Local) Collect(context.Context, docker.Dirs, docker.Dirs) error { return nil }

// Cache implements docker.Machine.
func (l Local) Cache(ctx context.Context, key string) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	dir := path.Join(l.CacheRoot, key)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("making the cache directory: %v", err)}
	}
	return dir, nil
}

// User implements docker.Machine.
func (l Local) User(context.Context) (docker.User, error) { return l.As, nil }

// Program implements docker.Machine: the engine runs on this machine.
func (Local) Program(_ context.Context, local string) (string, error) { return local, nil }

// CallTimeout bounds each call to the remote machine that should answer at once.
const CallTimeout = 2 * time.Minute

// Remote is a machine reached over ssh. Under Root it keeps one directory per staged checkout of
// the scope (staged/<scope>/<key>, holding checkout/, scratch/ and artifacts/), and one per cache
// key (cache/<key>). Copies can take as long as the data needs; every other call is bounded by
// CallTimeout.
//
// A run's staging removes the staged directories that ended jobs left. Without Runs the scope runs
// one job at a time, so every staged checkout but the run's own is one. With Runs the scope is a
// placement's whose jobs run at once, each in a run of its own (Run), and the checkouts are kept by
// run (staged/<scope>/<run>/<key>): a run removes its own run's other checkouts and the directories
// of the scope's dead runs, each while it holds it, and never a live run's.
type Remote struct {
	Shell remote.SSH
	Rsync remote.Rsync
	Root  string
	Scope string
	// Run is the scope of this process's run, under Scope; it is used with Runs.
	Run string
	// Runs, when set, says which runs of the scope live.
	Runs   docker.Runs
	Timers clock.Timers
	// Arch is the Go architecture of the programs Program places; empty is this build's.
	Arch string

	mu   sync.Mutex
	user *docker.User
}

var _ docker.Machine = (*Remote)(nil)

// run runs a short shell script on the machine, bounded.
func (r *Remote) run(ctx context.Context, script string, args ...string) (string, error) {
	bounded, done := remote.Bounded(ctx, r.Timers, CallTimeout)
	defer done()
	return remote.Output(bounded, r.Timers, r.Shell, append([]string{"sh", "-c", script, "vs-machine"}, args...), "")
}

// base is the directory of a staged checkout: one per checkout, so a job's commands share it and
// another job's do not.
func (r *Remote) base(checkout string) string {
	sum := sha256.Sum256([]byte(checkout))
	return path.Join(r.parent(), hex.EncodeToString(sum[:8]))
}

// parent is the directory the run's staged checkouts are in.
func (r *Remote) parent() string {
	if r.Runs == nil {
		return r.scopeDir()
	}
	return path.Join(r.scopeDir(), r.Run)
}

// scopeDir is the directory of the scope's staged checkouts, or of its runs' with Runs.
func (r *Remote) scopeDir() string { return path.Join(r.Root, "staged", r.Scope) }

// stageScript makes a run's directories, removes the other staged directories of its parent $2
// (its earlier checkouts: the parent's checkouts are one job's at a time), and empties the run's
// artifacts directory. With $4 it then prints the names of the directories of $4 other than $2's,
// one per line: the scope's other runs.
const stageScript = `set -eu
base="$1"; parent="$2"; artifacts="$3"; scope="${4-}"
mkdir -p "$base/checkout" "$base/scratch" "$base/artifacts"
for d in "$parent"/*; do
  if [ -d "$d" ] && [ "$d" != "$base" ]; then rm -rf "$d"; fi
done
if [ -n "$artifacts" ]; then rm -rf "$base/artifacts/$artifacts"; mkdir -p "$base/artifacts/$artifacts"; fi
if [ -n "$scope" ]; then
  for d in "$scope"/*; do
    if [ -d "$d" ] && [ "$d" != "$parent" ]; then printf '%s\n' "${d##*/}"; fi
  done
fi`

// removeScript removes a directory and everything in it.
const removeScript = `rm -rf "$1"`

// Stage implements docker.Machine. The checkout is copied without deleting what the commands left
// in the staged copy, such as a build, so a job's later commands find it; the scratch directory is
// mirrored.
func (r *Remote) Stage(ctx context.Context, d docker.Dirs) (docker.Dirs, error) {
	base := r.base(d.Checkout)
	name := ""
	if d.Artifacts != "" {
		name = path.Base(d.Artifacts)
	}
	args := []string{base, r.parent(), name}
	if r.Runs != nil {
		if err := r.Runs.Hold(ctx, r.Run); err != nil {
			return docker.Dirs{}, fmt.Errorf("marking the run %s live: %w", r.Run, err)
		}
		args = append(args, r.scopeDir())
	}
	others, err := r.run(ctx, stageScript, args...)
	if err != nil {
		return docker.Dirs{}, fmt.Errorf("staging the run on %s: %w", r.Shell.Where(), err)
	}
	if r.Runs != nil {
		if err := r.clearDead(ctx, strings.Fields(others)); err != nil {
			return docker.Dirs{}, err
		}
	}
	out := docker.Dirs{Checkout: path.Join(base, "checkout")}
	if err := r.Rsync.Push(ctx, d.Checkout, out.Checkout, false); err != nil {
		return docker.Dirs{}, fmt.Errorf("copying the checkout to %s: %w", r.Shell.Where(), err)
	}
	if d.Scratch != "" {
		out.Scratch = path.Join(base, "scratch")
		if err := r.Rsync.Push(ctx, d.Scratch, out.Scratch, true); err != nil {
			return docker.Dirs{}, fmt.Errorf("copying the scratch directory to %s: %w", r.Shell.Where(), err)
		}
	}
	if name != "" {
		out.Artifacts = path.Join(base, "artifacts", name)
	}
	return out, nil
}

// clearDead removes the staged directories of the scope's runs that are dead, each while it holds
// the run, so that no run of it starts meanwhile. A live run's are left, and so is a directory
// whose name is no run's.
func (r *Remote) clearDead(ctx context.Context, runs []string) error {
	for _, run := range runs {
		release, dead, err := r.Runs.Claim(run)
		if err != nil || !dead {
			continue
		}
		_, err = r.run(ctx, removeScript, path.Join(r.scopeDir(), run))
		release()
		if err != nil {
			return fmt.Errorf("removing what the dead run %s left on %s: %w", run, r.Shell.Where(), err)
		}
	}
	return nil
}

// Collect implements docker.Machine.
func (r *Remote) Collect(ctx context.Context, local, staged docker.Dirs) error {
	if local.Scratch != "" && staged.Scratch != "" {
		if err := r.Rsync.Pull(ctx, staged.Scratch, local.Scratch, true); err != nil {
			return fmt.Errorf("copying the scratch directory back from %s: %w", r.Shell.Where(), err)
		}
	}
	if local.Artifacts != "" && staged.Artifacts != "" {
		if err := r.Rsync.Pull(ctx, staged.Artifacts, local.Artifacts, false); err != nil {
			return fmt.Errorf("copying the artifacts back from %s: %w", r.Shell.Where(), err)
		}
	}
	return nil
}

// Cache implements docker.Machine.
func (r *Remote) Cache(ctx context.Context, key string) (string, error) {
	dir := path.Join(r.Root, "cache", key)
	if _, err := r.run(ctx, `mkdir -p "$1"`, dir); err != nil {
		return "", fmt.Errorf("making the cache directory on %s: %w", r.Shell.Where(), err)
	}
	return dir, nil
}

// programScript says whether the machine runs programs of the platform $3 (uname -m) and, when it
// does, whether the program is already at $1; when it is not, it makes its directory and prints
// "copy". It prints "platform <the machine's>" for another platform.
const programScript = `set -eu
m=$(uname -m)
[ "$m" = "$3" ] || { echo "platform $m"; exit 0; }
[ -x "$1" ] && { echo present; exit 0; }
mkdir -p "$2"
echo copy`

// unameOf names Go's architectures as uname -m names them on Linux.
var unameOf = map[string]string{"amd64": "x86_64", "arm64": "aarch64", "ppc64le": "ppc64le", "s390x": "s390x", "riscv64": "riscv64"}

// Program implements docker.Machine. The program is copied under Root by the digest of its
// content, once, and published by a rename, so a run of another process that copies the same
// program at the same moment finds it whole. Arch is the platform the program is built for.
func (r *Remote) Program(ctx context.Context, local string) (string, error) {
	data, err := os.ReadFile(local)
	if err != nil {
		return "", &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("reading the program %s: %v", local, err)}
	}
	sum := sha256.Sum256(data)
	dir := path.Join(r.Root, "bin", hex.EncodeToString(sum[:12]))
	dest := path.Join(dir, path.Base(local))
	arch := r.Arch
	if arch == "" {
		arch = runtime.GOARCH
	}
	uname, ok := unameOf[arch]
	if !ok {
		uname = arch
	}
	said, err := r.run(ctx, programScript, dest, dir, uname)
	if err != nil {
		return "", fmt.Errorf("placing the program %s on %s: %w", path.Base(local), r.Shell.Where(), err)
	}
	switch said = strings.TrimSpace(said); {
	case said == "present":
		return dest, nil
	case strings.HasPrefix(said, "platform "):
		return "", &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
			"%s is a %s machine, and the program %s is built for %s", r.Shell.Where(), strings.TrimPrefix(said, "platform "), local, uname)}
	}
	tmp := fmt.Sprintf("%s.tmp-%d", dest, os.Getpid())
	if err := r.Rsync.PushFile(ctx, local, tmp); err != nil {
		return "", fmt.Errorf("copying the program %s to %s: %w", path.Base(local), r.Shell.Where(), err)
	}
	if _, err := r.run(ctx, `chmod 0755 "$1" && mv -f "$1" "$2"`, tmp, dest); err != nil {
		return "", fmt.Errorf("placing the program %s on %s: %w", path.Base(local), r.Shell.Where(), err)
	}
	return dest, nil
}

// User implements docker.Machine: the ssh user's ids there, read once.
func (r *Remote) User(ctx context.Context) (docker.User, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.user != nil {
		return *r.user, nil
	}
	out, err := r.run(ctx, `id -u; id -g`)
	if err != nil {
		return docker.User{}, fmt.Errorf("reading the user on %s: %w", r.Shell.Where(), err)
	}
	fields := strings.Fields(out)
	if len(fields) != 2 {
		return docker.User{}, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("the user's ids on %s are not two numbers: %q", r.Shell.Where(), out)}
	}
	uid, err1 := strconv.Atoi(fields[0])
	gid, err2 := strconv.Atoi(fields[1])
	if err1 != nil || err2 != nil {
		return docker.User{}, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("the user's ids on %s are not two numbers: %q", r.Shell.Where(), out)}
	}
	r.user = &docker.User{UID: uid, GID: gid}
	return *r.user, nil
}
