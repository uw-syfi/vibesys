package devlaunch

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
)

// File is one install input: a repository-relative path and its bytes.
type File struct {
	Path    string
	Content []byte
}

// Fingerprint digests files so that it changes when a path or its content changes and does not
// depend on the order the files are given in.
func Fingerprint(files []File) string {
	records := make([]string, 0, len(files))
	for _, f := range files {
		sum := sha256.Sum256(f.Content)
		records = append(records, fmt.Sprintf("%d:%s:%x", len(f.Path), f.Path, sum))
	}
	sort.Strings(records)
	h := sha256.New()
	for _, r := range records {
		h.Write([]byte(r))
		h.Write([]byte{'\n'})
	}
	return hex.EncodeToString(h.Sum(nil))
}
