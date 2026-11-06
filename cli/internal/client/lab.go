package client

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// PushChunk is the most bytes one push of a file to a lab carries.
const PushChunk = 1 << 20

// Copied says what a copy into or out of a lab moved.
type Copied struct {
	Files int
	Bytes uint64
}

// Pull copies what is at the path of the lab's artifacts directory to dest on this machine: a file
// to dest, and a directory's files under dest, keeping their paths from it. Directories are made
// as they are needed, and a file there is replaced.
func (c *Client) Pull(ctx context.Context, callCtx *cliv1.CallContext, job, from, dest string) (Copied, error) {
	var out Copied
	var f *os.File
	var at string
	closeFile := func() error {
		if f == nil {
			return nil
		}
		err := f.Close()
		f = nil
		return err
	}
	defer func() { _ = closeFile() }()
	pulled := false
	err := c.Lab(ctx, &cliv1.LabRequest{Context: callCtx, JobId: job, Call: &cliv1.LabCall{Op: &cliv1.LabCall_Pull{Pull: &cliv1.LabPull{Path: from}}}}, func(r *cliv1.LabReply) error {
		switch item := r.GetItem().(type) {
		case *cliv1.LabReply_File:
			file := item.File
			to, err := local(dest, file.GetPath())
			if err != nil {
				return err
			}
			if file.GetOffset() == 0 {
				if err := closeFile(); err != nil {
					return err
				}
				if err := os.MkdirAll(filepath.Dir(to), 0o755); err != nil {
					return err
				}
				mode := os.FileMode(0o644)
				if file.GetExecutable() {
					mode = 0o755
				}
				if f, err = os.OpenFile(to, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, mode); err != nil {
					return err
				}
				at = to
				out.Files++
			}
			if f == nil || at != to {
				return wire.Unavailable(fmt.Sprintf("the lab sent a piece of %s before its start", file.GetPath()))
			}
			if _, err := f.WriteAt(file.GetData(), int64(file.GetOffset())); err != nil {
				return err
			}
			out.Bytes += uint64(len(file.GetData()))
		case *cliv1.LabReply_Pulled:
			pulled = true
		default:
			return wire.Unavailable(fmt.Sprintf("the lab answered a pull with %T", item))
		}
		return nil
	})
	if err == nil {
		err = closeFile()
	}
	if err == nil && !pulled {
		err = wire.Unavailable("the lab's answer to the pull ended before it said it was done")
	}
	return out, err
}

// local is where a file of a pull goes: dest for the file pulled itself, and its path from the
// directory pulled under dest otherwise. A path that climbs out of dest is refused.
func local(dest, rel string) (string, error) {
	if rel == "" {
		return dest, nil
	}
	clean := path.Clean(rel)
	if path.IsAbs(clean) || clean == ".." || len(clean) > 2 && clean[:3] == "../" {
		return "", wire.Unavailable(fmt.Sprintf("the lab sent a file at %q, outside the directory pulled", rel))
	}
	return filepath.Join(dest, filepath.FromSlash(clean)), nil
}

// Push copies the file or directory src of this machine to the path of the lab's artifacts
// directory: a file to the path, and a directory's files under it, keeping their paths from it.
func (c *Client) Push(ctx context.Context, callCtx *cliv1.CallContext, job, src, to string) (Copied, error) {
	var out Copied
	info, err := os.Stat(src)
	if err != nil {
		return out, err
	}
	if !info.IsDir() {
		return out, c.pushFile(ctx, callCtx, job, src, to, info, &out)
	}
	err = filepath.WalkDir(src, func(p string, d os.DirEntry, err error) error {
		if err != nil || !d.Type().IsRegular() {
			return err
		}
		rel, err := filepath.Rel(src, p)
		if err != nil {
			return err
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		return c.pushFile(ctx, callCtx, job, p, path.Join(to, filepath.ToSlash(rel)), info, &out)
	})
	return out, err
}

// pushFile sends one file in pieces, the last of which makes it visible to the lab.
func (c *Client) pushFile(ctx context.Context, callCtx *cliv1.CallContext, job, src, to string, info os.FileInfo, out *Copied) error {
	f, err := os.Open(src)
	if err != nil {
		return err
	}
	defer f.Close()
	buf := make([]byte, PushChunk)
	var offset uint64
	for {
		n, err := io.ReadFull(f, buf)
		last := errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF)
		if err != nil && !last {
			return err
		}
		push := &cliv1.LabPush{Path: to, Offset: offset, Data: buf[:n], Executable: info.Mode()&0o100 != 0, Last: last}
		answered := false
		err = c.Lab(ctx, &cliv1.LabRequest{Context: callCtx, JobId: job, Call: &cliv1.LabCall{Op: &cliv1.LabCall_Push{Push: push}}}, func(r *cliv1.LabReply) error {
			answered = r.GetPushed() != nil
			return nil
		})
		if err != nil {
			return err
		}
		if !answered {
			return wire.Unavailable("the lab did not answer a push")
		}
		offset += uint64(n)
		out.Bytes += uint64(n)
		if last {
			out.Files++
			return nil
		}
	}
}
