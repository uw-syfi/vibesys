package apptainer

import (
	"archive/tar"
	"errors"
	"fmt"
	"io"
	"path"
	"strings"
)

// Runtime is what runs a container on a node.
type Runtime int

const (
	// RuntimeApptainer converts each image to a SIF on this machine, which the nodes' apptainer
	// runs.
	RuntimeApptainer Runtime = iota
	// RuntimePyxis streams each image's root file system to the login node, which makes an Enroot
	// squashfs of it, and runs each process as a step of srun --container-image (Slurm's Pyxis
	// plugin, over Enroot).
	RuntimePyxis
)

// ImageMarker is the file at the root of a squashfs that names the content of the image it holds,
// in one line: the last entry of the stream it is made from, so a squashfs that has it was made
// from the whole stream. sqfstar accepts a stream cut short between two entries.
const ImageMarker = ".vibesys-image"

// replaced are the files of an image's root file system that Enroot reads to set up a container,
// which its import writes from the image's config, as RootFS does: the environment, and the mounts
// of the image's volumes, of which a container of this package gets none.
var replaced = map[string]bool{"etc/environment": true, "etc/fstab": true}

// RootFS writes to w the root file system of an image, as Enroot runs it, from export, its files as
// `docker export` writes them of a container of it, and env, its environment (KEY=VALUE, one each):
// every entry of export but what Enroot's own import leaves out (the devices under dev/, which the
// node gives, and .dockerenv) and the files Enroot reads to set up a container, which it writes
// from env, then ImageMarker holding content. It is a tar archive, written as export is read, so
// that an image is never on a disk whole.
func RootFS(export io.Reader, env []string, content string, w io.Writer) error {
	for _, kv := range env {
		if k, _, ok := strings.Cut(kv, "="); !ok || k == "" || strings.ContainsAny(kv, "\n\x00") {
			return invalid("the image's environment has %q, which is not one KEY=VALUE line", kv)
		}
	}
	tw := tar.NewWriter(w)
	r := tar.NewReader(export)
	etc := false
	for {
		h, err := r.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return fmt.Errorf("reading the image's files: %w", err)
		}
		name := entryName(h.Name)
		if name == "etc" {
			etc = true
		}
		if name == "" || name == ".dockerenv" || name == ImageMarker || replaced[name] || strings.HasPrefix(name, "dev/") {
			continue
		}
		if err := tw.WriteHeader(h); err != nil {
			return err
		}
		if _, err := io.Copy(tw, r); err != nil {
			return fmt.Errorf("reading the image's file %s: %w", h.Name, err)
		}
	}
	if !etc {
		if err := tw.WriteHeader(&tar.Header{Typeflag: tar.TypeDir, Name: "etc/", Mode: 0o755}); err != nil {
			return err
		}
	}
	var environ strings.Builder
	for _, kv := range env {
		environ.WriteString(kv + "\n")
	}
	files := []struct{ name, data string }{
		{"etc/environment", environ.String()},
		{"etc/fstab", ""},
		{ImageMarker, content + "\n"},
	}
	for _, f := range files {
		if err := tw.WriteHeader(&tar.Header{Typeflag: tar.TypeReg, Name: f.name, Mode: 0o644, Size: int64(len(f.data))}); err != nil {
			return err
		}
		if _, err := io.WriteString(tw, f.data); err != nil {
			return err
		}
	}
	return tw.Close()
}

// entryName is a tar entry's path relative to the root, as sqfstar reads it: without a leading /
// or ./, and without a trailing slash.
func entryName(name string) string {
	name = strings.TrimLeft(name, "/")
	for strings.HasPrefix(name, "./") {
		name = strings.TrimLeft(strings.TrimPrefix(name, "./"), "/")
	}
	name = path.Clean("/" + name)
	return strings.TrimPrefix(name, "/")
}
