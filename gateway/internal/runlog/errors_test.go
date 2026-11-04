package runlog

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestCompleteRecordsPrecedeCorruption(t *testing.T) {
	path := filepath.Join(t.TempDir(), "run.log")
	valid := []byte("valid")
	damaged := framed([]byte("bad"))
	damaged[len(damaged)-1] ^= 1
	data := append(append([]byte(Magic), framed(valid)...), damaged...)
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	f, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	batch, err := f.Read(ctx, 1<<20)
	var corruption *Corruption
	if !errors.As(err, &corruption) || len(batch) != 1 || !bytes.Equal(batch[0], valid) {
		t.Fatalf("Read = %v, %v", batch, err)
	}
}

func TestRejectAppendOnlyViolations(t *testing.T) {
	for _, replace := range []bool{false, true} {
		t.Run(map[bool]string{false: "truncate", true: "replace"}[replace], func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "run.log")
			data := append([]byte(Magic), framed([]byte("first"))...)
			if err := os.WriteFile(path, data, 0600); err != nil {
				t.Fatal(err)
			}
			f, err := Open(path)
			if err != nil {
				t.Fatal(err)
			}
			defer f.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			if _, err := f.Read(ctx, 1); err != nil {
				t.Fatal(err)
			}
			if replace {
				if err := os.Remove(path); err != nil {
					t.Fatal(err)
				}
			}
			if err := os.WriteFile(path, []byte(Magic), 0600); err != nil {
				t.Fatal(err)
			}
			_, err = f.Read(ctx, 1)
			var corruption *Corruption
			if !errors.As(err, &corruption) {
				t.Fatalf("Read = %v, want corruption", err)
			}
		})
	}
}
