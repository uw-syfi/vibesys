package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// TestWriteAtomicReplacesReportWithoutLeavingTemporaries verifies that the
// normalizer's report write is atomic: the destination contains exactly the
// written bytes with 0644 permissions and no leftover temporary files remain in
// the output directory.
func TestWriteAtomicReplacesReportWithoutLeavingTemporaries(t *testing.T) {
	directory := t.TempDir()
	output := filepath.Join(directory, "report.json")
	if err := os.WriteFile(output, []byte("stale"), 0o600); err != nil {
		t.Fatal(err)
	}

	payload := []byte(`{"schema_version":1}` + "\n")
	if err := writeAtomic(output, payload); err != nil {
		t.Fatal(err)
	}

	data, err := os.ReadFile(output)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != string(payload) {
		t.Fatalf("report contents = %q", string(data))
	}
	info, err := os.Stat(output)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o644 {
		t.Fatalf("report mode = %v", info.Mode().Perm())
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

// TestWriteAtomicRejectsUnwritableDirectory confirms the writer surfaces an
// error instead of leaving a partial report when the temporary file cannot be
// created.
func TestWriteAtomicRejectsUnwritableDirectory(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "does-not-exist", "report.json")
	if err := writeAtomic(missing, []byte("{}")); err == nil {
		t.Fatal("wrote a report into a missing directory")
	}
}

// TestWriteAtomicProducesDecodableReport is a light guard that the bytes handed
// to writeAtomic survive the round trip as valid JSON.
func TestWriteAtomicProducesDecodableReport(t *testing.T) {
	output := filepath.Join(t.TempDir(), "report.json")
	if err := writeAtomic(output, []byte(`{"schema_version":1,"span_count":3}`)); err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	data, err := os.ReadFile(output)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("report is not valid JSON: %v", err)
	}
}
