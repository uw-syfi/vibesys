package release

import (
	"strconv"
	"strings"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// Relate says how a host's release compares with a client's, by version precedence
// (MAJOR.MINOR.PATCH, then a version with a -suffix is older than the same without one). A version
// that is not a release's, such as the "dev" of a local build or an empty one, relates to nothing.
func Relate(host, client string) pb.ReleaseRelation {
	h, hok := parseVersion(host)
	c, cok := parseVersion(client)
	switch {
	case !hok || !cok:
		return pb.ReleaseRelation_RELEASE_RELATION_UNKNOWN
	case h.compare(c) < 0:
		return pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER
	case h.compare(c) > 0:
		return pb.ReleaseRelation_RELEASE_RELATION_HOST_NEWER
	}
	return pb.ReleaseRelation_RELEASE_RELATION_SAME
}

type version struct {
	core [3]uint64
	pre  []string
}

func parseVersion(text string) (version, bool) {
	core, pre, hasPre := strings.Cut(text, "-")
	parts := strings.Split(core, ".")
	if len(parts) != 3 || (hasPre && pre == "") {
		return version{}, false
	}
	var v version
	for i, p := range parts {
		n, err := strconv.ParseUint(p, 10, 64)
		if err != nil {
			return version{}, false
		}
		v.core[i] = n
	}
	if hasPre {
		v.pre = strings.Split(pre, ".")
	}
	return v, true
}

func (v version) compare(o version) int {
	for i := range v.core {
		if v.core[i] != o.core[i] {
			if v.core[i] < o.core[i] {
				return -1
			}
			return 1
		}
	}
	switch {
	case len(v.pre) == 0 && len(o.pre) == 0:
		return 0
	case len(v.pre) == 0:
		return 1
	case len(o.pre) == 0:
		return -1
	}
	for i := 0; i < min(len(v.pre), len(o.pre)); i++ {
		if c := comparePre(v.pre[i], o.pre[i]); c != 0 {
			return c
		}
	}
	return len(v.pre) - len(o.pre)
}

// comparePre orders two pre-release identifiers: numbers by value, and before words; words by text.
func comparePre(a, b string) int {
	an, aerr := strconv.ParseUint(a, 10, 64)
	bn, berr := strconv.ParseUint(b, 10, 64)
	switch {
	case aerr == nil && berr == nil:
		if an < bn {
			return -1
		} else if an > bn {
			return 1
		}
		return 0
	case aerr == nil:
		return -1
	case berr == nil:
		return 1
	}
	return strings.Compare(a, b)
}
