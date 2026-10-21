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

func TestFollowCreationAndAppend(t *testing.T) {
	path := filepath.Join(t.TempDir(), "run.log")
	follower, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer follower.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	type result struct {
		batch [][]byte
		err   error
	}
	read := func() <-chan result {
		done := make(chan result, 1)
		go func() { batch, err := follower.Read(ctx, 1<<20); done <- result{batch, err} }()
		return done
	}
	receive := func(done <-chan result, want []byte) {
		t.Helper()
		select {
		case got := <-done:
			if got.err != nil || len(got.batch) != 1 || !bytes.Equal(got.batch[0], want) {
				t.Fatalf("Read = %v, %v", got.batch, got.err)
			}
		case <-ctx.Done():
			t.Fatal(ctx.Err())
		}
	}
	first := []byte("first")
	pending := read()
	file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	if _, err := file.Write(append([]byte(Magic), framed(first)...)); err != nil {
		t.Fatal(err)
	}
	receive(pending, first)
	second := []byte("second")
	pending = read()
	frame := framed(second)
	if _, err := file.Write(frame[:len(frame)-1]); err != nil {
		t.Fatal(err)
	}
	if _, err := file.Write(frame[len(frame)-1:]); err != nil {
		t.Fatal(err)
	}
	receive(pending, second)
	pending = read()
	cancel()
	select {
	case got := <-pending:
		if !errors.Is(got.err, context.Canceled) {
			t.Fatalf("cancel = %v", got.err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("cancellation hung")
	}
}

func TestFollowBatchBound(t *testing.T) {
	path := filepath.Join(t.TempDir(), "run.log")
	records := [][]byte{[]byte("aa"), []byte("bbb"), []byte("c")}
	data := []byte(Magic)
	for _, record := range records {
		data = append(data, framed(record)...)
	}
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	follower, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer follower.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	for _, want := range records {
		got, err := follower.Read(ctx, 3)
		if err != nil || len(got) != 1 || !bytes.Equal(got[0], want) {
			t.Fatalf("Read = %v, %v", got, err)
		}
	}
}
