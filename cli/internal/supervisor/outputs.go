package supervisor

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/flowrun"
	"github.com/uw-syfi/vibesys/cli/internal/hostfs"
)

// commandArtifacts is the artifacts directory of a command job's one command, where its outputs
// are: the one place both the supervisor that collects them and the daemon that reads them back
// name it.
func commandArtifacts(h Host, repository, task string, job core.JobID) string {
	return flowrun.ArtifactsOf(h.Artifacts(repository, task, job), 1, "command")
}

// OpenOutput opens a file a command job of the task in the repository kept as an output, by its
// path in the job's artifacts directory. It refuses a path that leaves the directory and a file
// that is not a regular one; the caller holds what it reads to the digest the job recorded.
func (h Host) OpenOutput(repository, task string, job core.JobID, path string) (io.ReadCloser, error) {
	if h.Artifacts == nil || h.Files == nil {
		return nil, &core.Failure{Kind: core.FailureUnavailable, Message: "this daemon keeps no artifacts directory, so it has no outputs to read"}
	}
	if err := core.CheckOutputPath(path); err != nil {
		return nil, err
	}
	return h.Files.Open(commandArtifacts(h, repository, task, job), path)
}

// collectOutputs records what a command left in its artifacts directory: the files with their
// digests when core.RefuseOutputs passes what Tree lists, and otherwise why none is kept. A file
// that changes while it is read is refused, since its digest would not be the content kept.
func collectOutputs(ctx context.Context, files hostfs.FS, dir string) core.Outputs {
	listed, err := files.Tree(ctx, dir)
	if err != nil {
		return core.OutputsRefused{Reason: fmt.Sprintf("the artifacts directory could not be read, so none was kept: %v", err)}
	}
	entries := make([]core.OutputEntry, 0, len(listed))
	for _, e := range listed {
		entry := core.OutputEntry{Path: e.Path, Kind: core.OutputOther}
		switch e.Kind {
		case hostfs.EntryFile:
			entry.Kind, entry.Size = core.OutputRegular, uint64(max(e.Size, 0))
		case hostfs.EntrySymlink:
			entry.Kind = core.OutputSymlink
		case hostfs.EntryOther:
		}
		entries = append(entries, entry)
	}
	if refused := core.RefuseOutputs(entries); refused != nil {
		return *refused
	}
	kept := core.OutputsKept{}
	for _, e := range entries {
		digest, size, err := digestOf(files, dir, e.Path)
		switch {
		case err != nil:
			return core.OutputsRefused{Reason: fmt.Sprintf("%q in the artifacts directory could not be read, so none was kept: %v", e.Path, err)}
		case size != e.Size:
			return core.OutputsRefused{Reason: fmt.Sprintf("%q in the artifacts directory changed while it was read, so none was kept", e.Path)}
		}
		kept.Files = append(kept.Files, core.OutputFile{Path: e.Path, Size: size, SHA256: digest})
	}
	return kept
}

// digestOf reads a file whole, no further than one byte past the bound, and says its SHA-256 and
// size.
func digestOf(files hostfs.FS, dir, path string) (string, uint64, error) {
	r, err := files.Open(dir, path)
	if err != nil {
		return "", 0, err
	}
	defer r.Close()
	h := sha256.New()
	n, err := io.Copy(h, io.LimitReader(r, core.MaxOutputBytes+1))
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(h.Sum(nil)), uint64(n), nil
}

// outputsLine says in the job's output what was recorded.
func outputsLine(o core.Outputs) string {
	switch o := o.(type) {
	case core.OutputsKept:
		return fmt.Sprintf("[command] outputs: kept %s, %s; read them with vibesys-cli outputs <job>\n", core.FileCount(len(o.Files)), core.ByteSize(o.Bytes()))
	case core.OutputsRefused:
		return fmt.Sprintf("[command] outputs: refused: %s\n", o.Reason)
	}
	return ""
}
