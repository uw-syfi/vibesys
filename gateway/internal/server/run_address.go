package server

import (
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"buf.build/go/protovalidate"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"golang.org/x/text/unicode/norm"
	"google.golang.org/protobuf/proto"
)

// runProject mirrors Project.from_log_directory: only the canonical physical
// layout establishes routing. Recorded epoch roots and sidecars cannot redirect it.
func runProject(id, dir string) (string, error) {
	canonical, err := filepath.EvalSymlinks(dir)
	if err != nil {
		return "", err
	}
	at := canonical
	for _, part := range []string{"logs", id, "runs", "local", "state", ".vibesys"} {
		if filepath.Base(at) != part {
			return "", os.ErrNotExist
		}
		at = filepath.Dir(at)
	}
	if err := protovalidate.Validate(&apiv1.RunEntry{ProjectRoot: proto.String(at)}); err != nil {
		return "", err
	}
	return at, nil
}

// externalProject validates Project's external state key before using any saved path.
func externalProject(home, id, dir, recorded string) (string, error) {
	if recorded == "" || !filepath.IsAbs(recorded) {
		return "", os.ErrNotExist
	}
	home, err := filepath.EvalSymlinks(home)
	if err != nil {
		return "", err
	}
	root, err := filepath.EvalSymlinks(recorded)
	if err != nil {
		return "", err
	}
	info, err := os.Stat(root)
	if err != nil {
		return "", err
	}
	if !info.IsDir() {
		return "", os.ErrNotExist
	}
	if relative, err := filepath.Rel(root, dir); err != nil || filepath.IsLocal(relative) {
		return "", os.ErrNotExist
	}
	var ascii strings.Builder
	for _, ch := range norm.NFKD.String(filepath.Base(root)) {
		if ch < 128 {
			ascii.WriteRune(ch)
		}
	}
	slug := strings.Trim(regexp.MustCompile("[^a-z0-9]+").ReplaceAllString(strings.ToLower(ascii.String()), "-"), "-")
	if slug == "" {
		slug = "project"
	}
	if len(slug) > 64 {
		slug = strings.TrimRight(slug[:64], "-")
	}
	digest := sha256.Sum256([]byte(filepath.ToSlash(root)))
	key := fmt.Sprintf("%s-%x", slug, digest[:6])
	if dir != filepath.Join(home, "projects", key, "runs", id, "logs") {
		return "", os.ErrNotExist
	}
	return root, nil
}

func (o *headObserver) project(home, id, dir string, head *storev1.RunHead) (string, error) {
	if project, err := runProject(id, dir); err == nil {
		return project, nil
	}
	canonical, err := filepath.EvalSymlinks(dir)
	if err != nil {
		return "", err
	}
	home, err = filepath.EvalSymlinks(home)
	if err != nil {
		return "", err
	}
	if data, err := readFile(home, filepath.Join("run-addresses", id+".pb"), 1<<18); err == nil {
		address := &storev1.RunAddress{}
		if proto.Unmarshal(data, address) == nil && protovalidate.Validate(address) == nil && address.LogDirectory == canonical {
			if root, err := externalProject(home, id, canonical, address.ProjectRoot); err == nil {
				return root, nil
			}
		}
	}
	f, err := openRegular(canonical, "run.log")
	if err != nil {
		return "", err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return "", err
	}
	if head == nil {
		head = &storev1.RunHead{}
	}
	facts, err := o.recovered(f, info, head)
	if err != nil {
		return "", err
	}
	return externalProject(home, id, canonical, facts.project)
}
