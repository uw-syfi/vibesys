package core

import (
	"fmt"
	"path"
	"slices"
	"strings"
	"unicode/utf8"
)

// The bounds of what a command job keeps as its outputs. They hold the record that goes into the
// job, and the bytes the daemon keeps and sends back, to what a result file, a table of timings or
// a profile needs: a job that leaves more is refused, by name and with the size it left, and keeps
// nothing. The contract's JobOutputs states the same numbers, and a test holds the two together.
const (
	// MaxOutputFiles is the most files a job keeps.
	MaxOutputFiles = 1000
	// MaxOutputBytes is the most bytes a job keeps, all its files together.
	MaxOutputBytes = 256 << 20
	// MaxOutputPathBytes is the longest path of a file a job keeps.
	MaxOutputPathBytes = 1024
)

// OutputFile is one file a command job kept: its path in the job's artifacts directory, its size
// and the SHA-256 of its content in lowercase hexadecimal.
type OutputFile struct {
	Path   string
	Size   uint64
	SHA256 string
}

// Outputs is what a command job that declared outputs left when its command ended. The set is
// closed: the files it kept, or why it kept none.
type Outputs interface{ outputs() }

// OutputsKept is the files a job kept, in path order.
type OutputsKept struct{ Files []OutputFile }

// OutputsRefused says why a job kept no files: what it left was past a bound, held an entry that
// is not a regular file, or could not be read. Reason names the bound and what was left.
type OutputsRefused struct{ Reason string }

func (OutputsKept) outputs()    {}
func (OutputsRefused) outputs() {}

// Bytes is the size of every file kept, together.
func (k OutputsKept) Bytes() uint64 {
	var n uint64
	for _, f := range k.Files {
		n += f.Size
	}
	return n
}

// Check says whether the record is one a job can hold: within the bounds, each path valid and
// once, in path order, each digest a SHA-256. The error is a *Failure of kind FailureInvalidInput.
func (k OutputsKept) Check() error {
	if len(k.Files) > MaxOutputFiles {
		return failf(FailureInvalidInput, "outputs: %d files, and a job keeps at most %d", len(k.Files), MaxOutputFiles)
	}
	if b := k.Bytes(); b > MaxOutputBytes {
		return failf(FailureInvalidInput, "outputs: %s, and a job keeps at most %s", ByteSize(b), ByteSize(MaxOutputBytes))
	}
	for i, f := range k.Files {
		if err := CheckOutputPath(f.Path); err != nil {
			return err
		}
		if i > 0 && k.Files[i-1].Path >= f.Path {
			return failf(FailureInvalidInput, "outputs: %q comes after %q, and the files are in path order, each once", f.Path, k.Files[i-1].Path)
		}
		if !isSHA256(f.SHA256) {
			return failf(FailureInvalidInput, "outputs: the digest of %q is not a SHA-256 in lowercase hexadecimal", f.Path)
		}
	}
	return nil
}

// Check says whether the refusal says why.
func (r OutputsRefused) Check() error {
	if r.Reason == "" {
		return failf(FailureInvalidInput, "outputs: a refusal says why")
	}
	return nil
}

// CheckOutputs checks either kind of record.
func CheckOutputs(o Outputs) error {
	switch o := o.(type) {
	case OutputsKept:
		return o.Check()
	case OutputsRefused:
		return o.Check()
	}
	return failf(FailureInvalidInput, "outputs: no record")
}

// checkOutputsEffect holds a transition's outputs to the one move that records them: a command job
// that declared outputs, leaving running for a final state.
func checkOutputsEffect(j Job, t Transition) error {
	command, ok := j.Spec.Settings.(CommandSettings)
	switch {
	case !ok || !command.Outputs:
		return failf(FailureInvalidInput, "transition: job %s did not declare outputs, and records none", j.ID)
	case t.From != JobRunning || !t.To.Final():
		return failf(FailureInvalidInput, "transition: outputs are recorded by the move from running to a final state, not from %s to %s", t.From, t.To)
	}
	return CheckOutputs(t.Outputs)
}

// cloneOutputs copies a record, so the job shares no slice with the transition.
func cloneOutputs(o Outputs) Outputs {
	if k, ok := o.(OutputsKept); ok {
		return OutputsKept{Files: slices.Clone(k.Files)}
	}
	return o
}

func isSHA256(s string) bool {
	if len(s) != 64 {
		return false
	}
	for _, c := range s {
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return false
		}
	}
	return true
}

// CheckOutputPath says whether p can name a file a job keeps: UTF-8, slash-separated, relative,
// with no empty, "." or ".." element, and at most MaxOutputPathBytes long. Such a path stays inside
// whatever directory it is joined to.
func CheckOutputPath(p string) error {
	switch {
	case p == "":
		return failf(FailureInvalidInput, "outputs: an empty path")
	case len(p) > MaxOutputPathBytes:
		return failf(FailureInvalidInput, "outputs: the path %q is %d bytes long, and a kept file's path is at most %d", p[:64]+"...", len(p), MaxOutputPathBytes)
	case !utf8.ValidString(p):
		return failf(FailureInvalidInput, "outputs: the path %q is not UTF-8", p)
	case strings.ContainsRune(p, 0) || strings.ContainsRune(p, '\\'):
		return failf(FailureInvalidInput, "outputs: the path %q has a NUL or a backslash", p)
	case path.IsAbs(p) || path.Clean(p) != p || p == "." || p == ".." || strings.HasPrefix(p, "../"):
		return failf(FailureInvalidInput, "outputs: the path %q is not relative and clean", p)
	}
	return nil
}

// OutputEntryKind is what an entry of a job's artifacts directory is. The set is closed.
type OutputEntryKind string

// The kinds of entry. Only a regular file is kept; a directory holds entries and is not one.
const (
	OutputRegular OutputEntryKind = "a regular file"
	OutputSymlink OutputEntryKind = "a symbolic link"
	OutputOther   OutputEntryKind = "neither a regular file nor a directory"
)

// OutputEntry is one entry a job left under its artifacts directory, as a listing gives it: its
// path there, what it is, and for a regular file its size.
type OutputEntry struct {
	Path string
	Kind OutputEntryKind
	Size uint64
}

// RefuseOutputs says why the entries a job left cannot be kept, or nil when they can: too many
// files, too many bytes (each naming the bound and what was left, both counted in full), an entry
// that is not a regular file, or a path a kept file cannot have. It is the one rule of what a job
// keeps; the caller reads the files' content only when it passes.
func RefuseOutputs(entries []OutputEntry) *OutputsRefused {
	var bytes uint64
	for _, e := range entries {
		bytes += e.Size
	}
	var over []string
	if len(entries) > MaxOutputFiles {
		over = append(over, fmt.Sprintf("%d files, more than the %d a job keeps", len(entries), MaxOutputFiles))
	}
	if bytes > MaxOutputBytes {
		over = append(over, fmt.Sprintf("%s, more than the %s a job keeps", ByteSize(bytes), ByteSize(MaxOutputBytes)))
	}
	if len(over) > 0 {
		return &OutputsRefused{Reason: fmt.Sprintf("the job left %s in its artifacts directory, so none was kept", strings.Join(over, ", and "))}
	}
	for _, e := range entries {
		if e.Kind != OutputRegular {
			return &OutputsRefused{Reason: fmt.Sprintf("%q in the artifacts directory is %s, and a job keeps only regular files, so none was kept", e.Path, e.Kind)}
		}
		if err := CheckOutputPath(e.Path); err != nil {
			return &OutputsRefused{Reason: fmt.Sprintf("%s, so none was kept", AsFailure(err).Message)}
		}
	}
	return nil
}

// FileCount words a number of files: "1 file", "3 files".
func FileCount(n int) string {
	if n == 1 {
		return "1 file"
	}
	return fmt.Sprintf("%d files", n)
}

// ByteSize words a number of bytes for a person, exactly: "256 MiB (268435456 bytes)" or
// "12 bytes".
func ByteSize(n uint64) string {
	units := []struct {
		name string
		size uint64
	}{{"GiB", 1 << 30}, {"MiB", 1 << 20}, {"KiB", 1 << 10}}
	for _, u := range units {
		if n >= u.size {
			if n%u.size == 0 {
				return fmt.Sprintf("%d %s (%d bytes)", n/u.size, u.name, n)
			}
			return fmt.Sprintf("%.1f %s (%d bytes)", float64(n)/float64(u.size), u.name, n)
		}
	}
	if n == 1 {
		return "1 byte"
	}
	return fmt.Sprintf("%d bytes", n)
}
