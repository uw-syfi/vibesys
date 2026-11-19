package client

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"hash"
	"os"
	"path"
	"path/filepath"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// Outputs returns a command job with the record of the output files it kept, which the daemon
// refuses for a job that kept none, saying why.
func (c *Client) Outputs(ctx context.Context, req *cliv1.ReadOutputsRequest) (*cliv1.Job, error) {
	req.Content = false
	var job *cliv1.Job
	err := c.readOutputs(ctx, req, func(r *cliv1.ReadOutputsResponse) error {
		if job != nil || r.GetJob() == nil {
			return fmt.Errorf("the daemon sent more than the job's outputs")
		}
		job = r.GetJob()
		return nil
	})
	if err != nil {
		return nil, err
	}
	if job == nil {
		return nil, fmt.Errorf("the daemon sent no job")
	}
	if _, err := wire.ToJobOutputs("job.outputs", job.GetOutputs()); err != nil {
		return nil, err
	}
	return job, nil
}

// CopyOutputs writes the output files a command job kept into the directory to, made when it is
// not there, at their paths in the job's record, and returns the job. Each file is written beside
// its place and renamed into it only once its size and SHA-256 are the ones the job recorded, so a
// file that is there is whole; one already at the path is replaced. Nothing is written outside to:
// every path is held to the record's rules and opened through an os.Root of to.
func (c *Client) CopyOutputs(ctx context.Context, req *cliv1.ReadOutputsRequest, to string) (*cliv1.Job, error) {
	req.Content = true
	if err := os.MkdirAll(to, 0o755); err != nil {
		return nil, fmt.Errorf("making %s: %w", to, err)
	}
	root, err := os.OpenRoot(to)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	w := &outputWriter{root: root}
	defer w.abandon()
	err = c.readOutputs(ctx, req, func(r *cliv1.ReadOutputsResponse) error {
		switch item := r.GetItem().(type) {
		case *cliv1.ReadOutputsResponse_Job:
			return w.start(item.Job)
		case *cliv1.ReadOutputsResponse_Chunk:
			return w.write(item.Chunk)
		}
		return fmt.Errorf("the daemon sent an item this build cannot read")
	})
	if err != nil {
		return nil, err
	}
	if err := w.finish(); err != nil {
		return nil, err
	}
	return w.job, nil
}

func (c *Client) readOutputs(ctx context.Context, req *cliv1.ReadOutputsRequest, emit func(*cliv1.ReadOutputsResponse) error) error {
	stream, err := c.rpc.ReadOutputs(ctx, req)
	if err != nil {
		return c.fail(err)
	}
	for {
		resp, err := stream.Recv()
		if err != nil {
			return streamEnd(c, err)
		}
		if err := emit(resp); err != nil {
			return err
		}
	}
}

// outputWriter writes the files of one job's outputs, in the order its record lists them.
type outputWriter struct {
	root  *os.Root
	job   *cliv1.Job
	files []*cliv1.KeptOutput
	next  int // the index of the file being written, or of the next one

	open    *os.File
	part    string
	written uint64
	digest  hash.Hash
}

func (w *outputWriter) start(job *cliv1.Job) error {
	if w.job != nil {
		return fmt.Errorf("the daemon sent the job twice")
	}
	if _, err := wire.ToJobOutputs("job.outputs", job.GetOutputs()); err != nil {
		return err
	}
	w.job, w.files = job, job.GetOutputs().GetKept().GetOutputs()
	return nil
}

// write adds a piece to its file, after finishing every file before it, which an empty file is.
func (w *outputWriter) write(chunk *cliv1.KeptOutputChunk) error {
	if w.job == nil {
		return fmt.Errorf("the daemon sent a file before its job")
	}
	for w.open == nil || w.files[w.next].GetName() != chunk.GetName() {
		if w.open != nil {
			if err := w.close(); err != nil {
				return err
			}
			w.next++
		}
		if w.next >= len(w.files) {
			return fmt.Errorf("the daemon sent %q, which is not the next file the job kept", chunk.GetName())
		}
		if err := w.create(); err != nil {
			return err
		}
	}
	if chunk.GetOffset() != w.written {
		return fmt.Errorf("the daemon sent %q from byte %d, and %d were written", chunk.GetName(), chunk.GetOffset(), w.written)
	}
	if w.written+uint64(len(chunk.GetData())) > w.files[w.next].GetSize() {
		return fmt.Errorf("the daemon sent more of %q than the %d bytes the job kept", chunk.GetName(), w.files[w.next].GetSize())
	}
	if _, err := w.open.Write(chunk.GetData()); err != nil {
		return err
	}
	w.digest.Write(chunk.GetData())
	w.written += uint64(len(chunk.GetData()))
	return nil
}

// finish writes the files that are left, which have no bytes to come.
func (w *outputWriter) finish() error {
	if w.job == nil {
		return fmt.Errorf("the daemon sent no job")
	}
	for ; w.next < len(w.files); w.next++ {
		if w.open == nil {
			if err := w.create(); err != nil {
				return err
			}
		}
		if err := w.close(); err != nil {
			return err
		}
	}
	return nil
}

// create opens the file at w.next beside its place.
func (w *outputWriter) create() error {
	rel := w.files[w.next].GetName()
	if dir := path.Dir(rel); dir != "." {
		if err := w.root.MkdirAll(filepath.FromSlash(dir), 0o755); err != nil {
			return fmt.Errorf("making the directory of %q: %w", rel, err)
		}
	}
	w.part = filepath.FromSlash(rel) + ".vibesys-part"
	f, err := w.root.OpenFile(w.part, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o644)
	if err != nil {
		return fmt.Errorf("writing %q: %w", rel, err)
	}
	w.open, w.written, w.digest = f, 0, sha256.New()
	return nil
}

// close holds the open file to the record and renames it into its place.
func (w *outputWriter) close() error {
	want := w.files[w.next]
	f := w.open
	w.open = nil
	if err := f.Close(); err != nil {
		_ = w.root.Remove(w.part)
		return err
	}
	if got := hex.EncodeToString(w.digest.Sum(nil)); w.written != want.GetSize() || got != want.GetSha256() {
		_ = w.root.Remove(w.part)
		return fmt.Errorf("%q came with %d bytes of SHA-256 %s, and the job kept %d bytes of SHA-256 %s: it changed since the job ended, and was not written",
			want.GetName(), w.written, got, want.GetSize(), want.GetSha256())
	}
	if err := w.root.Rename(w.part, filepath.FromSlash(want.GetName())); err != nil {
		_ = w.root.Remove(w.part)
		return fmt.Errorf("writing %q: %w", want.GetName(), err)
	}
	return nil
}

// abandon removes a file left half written.
func (w *outputWriter) abandon() {
	if w.open != nil {
		_ = w.open.Close()
		_ = w.root.Remove(w.part)
		w.open = nil
	}
}
