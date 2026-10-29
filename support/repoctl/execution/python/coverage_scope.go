package python

import (
	"fmt"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/BurntSushi/toml"
)

type coverageScope struct {
	includesRun, includesReport, omits []*regexp.Regexp
}

// Read policy from coverage's own configuration, including report-only filters.
// Local --cov directories replace run.source, as they do in pytest-cov.
func readCoverageScope(root string) (coverageScope, error) {
	type filters struct {
		Include []string
		Omit    []string
	}
	var config struct {
		Tool struct {
			Coverage struct {
				Run    filters
				Report filters
			}
		}
	}
	if _, err := toml.DecodeFile(filepath.Join(root, "pyproject.toml"), &config); err != nil {
		return coverageScope{}, fmt.Errorf("coverage configuration: %w", err)
	}
	run, report := config.Tool.Coverage.Run, config.Tool.Coverage.Report
	scope := coverageScope{}
	for _, group := range []struct {
		patterns []string
		target   *[]*regexp.Regexp
	}{
		{run.Include, &scope.includesRun}, {report.Include, &scope.includesReport},
		{append(run.Omit, report.Omit...), &scope.omits},
	} {
		for _, pattern := range group.patterns {
			matcher, err := coveragePattern(root, pattern)
			if err != nil {
				return coverageScope{}, err
			}
			*group.target = append(*group.target, matcher)
		}
	}
	return scope, nil
}

func (s coverageScope) includes(path string) bool {
	matches := func(patterns []*regexp.Regexp) bool {
		for _, pattern := range patterns {
			if pattern.MatchString(path) {
				return true
			}
		}
		return false
	}
	return (len(s.includesRun) == 0 || matches(s.includesRun)) &&
		(len(s.includesReport) == 0 || matches(s.includesReport)) && !matches(s.omits)
}

// Coverage globs treat a leading */ and trailing /* as recursive, **/ as
// zero or more directories, and other stars as one path component.
func coveragePattern(root, pattern string) (*regexp.Regexp, error) {
	pattern = strings.ReplaceAll(pattern, `\`, "/")
	if strings.Contains(pattern, "**/**") {
		return nil, fmt.Errorf("invalid coverage pattern %q: **/** is not allowed", pattern)
	}
	// A pattern without a slash matches a file name anywhere; an absolute one
	// is anchored at the repository root even after it is made relative.
	anchored := strings.Contains(pattern, "/")
	if filepath.IsAbs(pattern) {
		relative, err := filepath.Rel(root, pattern)
		if err != nil {
			return nil, err
		}
		pattern = filepath.ToSlash(relative)
	}
	// coverage.py also matches the pattern made absolute, which normalizes it.
	if !strings.HasPrefix(pattern, "*") && !strings.HasPrefix(pattern, "?") {
		pattern = filepath.ToSlash(filepath.Clean(pattern))
	}
	if !anchored {
		pattern = "**/" + pattern
	}
	if strings.HasPrefix(pattern, "*/") {
		pattern = "*" + pattern
	}
	if strings.HasSuffix(pattern, "/*") {
		pattern += "*"
	}
	var rx strings.Builder
	rx.WriteString("^")
	for i := 0; i < len(pattern); i++ {
		switch pattern[i] {
		case '*':
			if i+1 < len(pattern) && pattern[i+1] == '*' {
				if i > 0 && pattern[i-1] != '/' {
					return nil, fmt.Errorf("invalid coverage pattern %q: ** must be a whole component", pattern)
				}
				i++
				if i+1 < len(pattern) && pattern[i+1] == '/' {
					rx.WriteString("(?:.*/)?")
					i++
				} else if i == len(pattern)-1 {
					rx.WriteString(".*")
				} else {
					return nil, fmt.Errorf("invalid coverage pattern %q: ** must be a whole component", pattern)
				}
			} else {
				rx.WriteString("[^/]*")
			}
		case '?':
			rx.WriteString("[^/]")
		case '[':
			end := strings.IndexByte(pattern[i+1:], ']')
			if end < 0 {
				return nil, fmt.Errorf("invalid coverage pattern %q: unclosed character class", pattern)
			}
			rx.WriteString(pattern[i : i+end+2])
			i += end + 1
		case ']':
			return nil, fmt.Errorf("invalid coverage pattern %q: unopened character class", pattern)
		default:
			rx.WriteString(regexp.QuoteMeta(pattern[i : i+1]))
		}
	}
	rx.WriteString("$")
	result, err := regexp.Compile(rx.String())
	if err != nil {
		return nil, fmt.Errorf("coverage pattern %q: %w", pattern, err)
	}
	return result, nil
}
