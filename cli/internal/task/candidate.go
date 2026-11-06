package task

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"io/fs"
	"path"
	"slices"
	"strings"

	"github.com/moby/patternmatcher"
	"github.com/moby/patternmatcher/ignorefile"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// dockerignore is the file in a build context that names what the build does not see.
const dockerignore = ".dockerignore"

// CandidateContext is what builds a candidate's image: its Dockerfile and the files of its build
// context as the commit has them, and the digest of both, which names the image.
type CandidateContext struct {
	// Dockerfile is the text of the candidate's Dockerfile.
	Dockerfile []byte
	// Files are the build context's files, by path from the context directory, without those its
	// .dockerignore leaves out. The .dockerignore itself is kept, as docker keeps it. A file named
	// Dockerfile at the root of the context is the Dockerfile above, whichever file that is.
	Files Tree
	// Digest names the Dockerfile and the files: the same commit, or two commits whose candidate
	// differs only in files the context leaves out, have the same digest.
	Digest string
}

// CandidatePaths are the paths of a repository a candidate's image is built from, for a source
// that reads only what it is asked for.
func CandidatePaths(b core.CandidateBuild) []string {
	if b.Context == "." {
		return []string{"."}
	}
	return []string{b.Dockerfile, b.Context}
}

// ReadCandidate reads what builds a candidate's image from a repository whose root is repo, as one
// commit has it: never a working tree. It refuses, naming the path, a Dockerfile or a context the
// commit does not have, and a Dockerfile whose first FROM is not one of the allowed bases.
func ReadCandidate(repo fs.FS, b core.CandidateBuild) (CandidateContext, error) {
	text, err := fs.ReadFile(repo, b.Dockerfile)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return CandidateContext{}, invalid("candidate.dockerfile: there is no file %s at this commit", b.Dockerfile)
		}
		return CandidateContext{}, unsupported("candidate.dockerfile: reading %s: %v", b.Dockerfile, err)
	}
	if err := checkBase(b, text); err != nil {
		return CandidateContext{}, err
	}
	if info, err := fs.Stat(repo, b.Context); err != nil || !info.IsDir() {
		return CandidateContext{}, invalid("candidate.context: there is no directory %s at this commit", b.Context)
	}
	all, err := filesUnder(repo, b.Context)
	if err != nil {
		return CandidateContext{}, err
	}
	files := Tree{}
	ignore, ok := all[dockerignore]
	var matcher *patternmatcher.PatternMatcher
	if ok {
		patterns, err := ignorefile.ReadAll(bytes.NewReader(ignore.Data))
		if err != nil {
			return CandidateContext{}, invalid("candidate.context: %s: %v", path.Join(b.Context, dockerignore), err)
		}
		if matcher, err = patternmatcher.New(patterns); err != nil {
			return CandidateContext{}, invalid("candidate.context: %s: %v", path.Join(b.Context, dockerignore), err)
		}
	}
	for name, f := range all {
		if matcher != nil && name != dockerignore {
			left, err := matcher.MatchesOrParentMatches(name)
			if err != nil {
				return CandidateContext{}, invalid("candidate.context: %s: %v", path.Join(b.Context, dockerignore), err)
			}
			if left {
				continue
			}
		}
		files[name] = f
	}
	files[dockerfile] = File{Data: text}
	return CandidateContext{Dockerfile: text, Files: files, Digest: versionOf(fingerprints(files))}, nil
}

// filesUnder reads the regular files under dir, by their path from it.
func filesUnder(repo fs.FS, dir string) (Tree, error) {
	files := Tree{}
	err := fs.WalkDir(repo, dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return nil
		}
		data, err := fs.ReadFile(repo, p)
		if err != nil {
			return err
		}
		rel := p
		if dir != "." {
			rel = strings.TrimPrefix(p, dir+"/")
		}
		files[rel] = File{Data: data, Executable: info.Mode()&0o111 != 0}
		return nil
	})
	if err != nil {
		return nil, unsupported("candidate.context: reading the files of %s: %v", dir, err)
	}
	return files, nil
}

// checkBase refuses a Dockerfile whose first FROM names an image the task does not allow, when it
// allows only some. A FROM that names its image through a build argument cannot be checked, and is
// refused too.
func checkBase(b core.CandidateBuild, text []byte) error {
	if len(b.AllowedBases) == 0 {
		return nil
	}
	base, err := FirstBase(text)
	if err != nil {
		return invalid("candidate.dockerfile: %s: %v", b.Dockerfile, err)
	}
	if strings.Contains(base, "$") {
		return invalid("candidate.dockerfile: %s: the first FROM names its image through a build argument (%q), which allowed_bases cannot check; name one of %s", b.Dockerfile, base, strings.Join(b.AllowedBases, ", "))
	}
	if !slices.Contains(b.AllowedBases, base) {
		return invalid("candidate.dockerfile: %s: the first FROM is %q, which is not one of the task's allowed_bases (%s)", b.Dockerfile, base, strings.Join(b.AllowedBases, ", "))
	}
	return nil
}

// FirstBase is the image the first FROM of a Dockerfile names, as written: its flags (such as
// --platform) and its stage name are not part of it. Comments, blank lines and line continuations
// are read as docker reads them.
func FirstBase(text []byte) (string, error) {
	sc := bufio.NewScanner(bytes.NewReader(text))
	sc.Buffer(make([]byte, 0, 64<<10), 1<<20)
	var logical strings.Builder
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if logical.Len() == 0 && (line == "" || strings.HasPrefix(line, "#")) {
			continue
		}
		if strings.HasPrefix(line, "#") {
			continue // a comment inside a continued instruction
		}
		if cont, ok := strings.CutSuffix(line, `\`); ok {
			logical.WriteString(cont)
			logical.WriteString(" ")
			continue
		}
		logical.WriteString(line)
		words := strings.Fields(logical.String())
		logical.Reset()
		if len(words) == 0 {
			continue
		}
		switch strings.ToUpper(words[0]) {
		case "ARG":
			continue
		case "FROM":
			for _, w := range words[1:] {
				if !strings.HasPrefix(w, "--") {
					return w, nil
				}
			}
			return "", fmt.Errorf("its first FROM names no image")
		default:
			return "", fmt.Errorf("its first instruction is %s, not FROM", words[0])
		}
	}
	if err := sc.Err(); err != nil {
		return "", err
	}
	return "", fmt.Errorf("it has no FROM")
}
