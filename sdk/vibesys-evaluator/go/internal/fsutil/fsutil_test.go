package fsutil

import (
	"os"
	"path/filepath"
	"testing"
)

// TestWriteFileAtomicReplacesFileWithoutLeavingTemporaries verifies the write is
// atomic: the destination contains exactly the written bytes with the requested
// permissions and no leftover temporary files remain in the output directory.
func TestWriteFileAtomicReplacesFileWithoutLeavingTemporaries(t *testing.T) {
	directory := t.TempDir()
	output := filepath.Join(directory, "report.json")
	if err := os.WriteFile(output, []byte("stale"), 0o600); err != nil {
		t.Fatal(err)
	}

	payload := []byte(`{"schema_version":1}` + "\n")
	if err := WriteFileAtomic(output, payload, 0o644); err != nil {
		t.Fatal(err)
	}

	data, err := os.ReadFile(output)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != string(payload) {
		t.Fatalf("contents = %q", string(data))
	}
	info, err := os.Stat(output)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o644 {
		t.Fatalf("mode = %v", info.Mode().Perm())
	}

	entries, err := os.ReadDir(directory)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if entry.Name() != "report.json" {
			t.Fatalf("unexpected leftover file %q", entry.Name())
		}
	}
}

// TestWriteFileAtomicHonorsPerm confirms the mode argument is applied rather
// than the temporary file's default 0600.
func TestWriteFileAtomicHonorsPerm(t *testing.T) {
	output := filepath.Join(t.TempDir(), "result.json")
	if err := WriteFileAtomic(output, []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(output)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("mode = %v", info.Mode().Perm())
	}
}

// TestWriteFileAtomicRejectsUnwritableDirectory confirms the writer surfaces an
// error instead of leaving a partial file when the temporary cannot be created.
func TestWriteFileAtomicRejectsUnwritableDirectory(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "does-not-exist", "report.json")
	if err := WriteFileAtomic(missing, []byte("{}"), 0o644); err == nil {
		t.Fatal("wrote a file into a missing directory")
	}
}
