package doctor

import (
	"regexp"
	"strconv"
	"strings"
)

var versionPattern = regexp.MustCompile(`\d+(?:\.\d+)+`)

// ParseVersion returns the first dotted version in the text a program printed for --version
// ("Python 3.12.3" gives "3.12.3"), or "" when it printed none.
func ParseVersion(output string) string {
	return versionPattern.FindString(output)
}

// CompareVersions orders two dotted versions numerically, field by field, a missing field being
// zero: "3.12" equals "3.12.0" and is below "3.12.1" and "3.9" is below "3.12". A field that is
// not a number counts as zero. It returns -1, 0 or 1.
func CompareVersions(a, b string) int {
	as, bs := strings.Split(a, "."), strings.Split(b, ".")
	for i := 0; i < max(len(as), len(bs)); i++ {
		x, y := field(as, i), field(bs, i)
		switch {
		case x < y:
			return -1
		case x > y:
			return 1
		}
	}
	return 0
}

func field(fields []string, i int) int {
	if i >= len(fields) {
		return 0
	}
	n, err := strconv.Atoi(fields[i])
	if err != nil {
		return 0
	}
	return n
}
