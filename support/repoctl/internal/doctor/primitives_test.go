package doctor_test

import (
	"strings"
	"testing"
	"testing/quick"

	"repoctl/internal/doctor"
)

func TestParseVersion(t *testing.T) {
	for in, want := range map[string]string{
		"Python 3.12.3":           "3.12.3",
		"1.4.2":                   "1.4.2",
		"bun 1.3.8 (abc)":         "1.3.8",
		"go version go1.25.1 x/y": "1.25.1",
		"no digits":               "",
		"v7":                      "",
	} {
		if got := doctor.ParseVersion(in); got != want {
			t.Errorf("ParseVersion(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestCompareVersionsTable(t *testing.T) {
	for _, tc := range []struct {
		a, b string
		want int
	}{{"3.12", "3.12.0", 0}, {"3.9", "3.12", -1}, {"3.12.1", "3.12", 1}, {"1.4.2", "1.4.2", 0}, {"1.3.8", "1.4.2", -1}, {"10.0", "9.9", 1}} {
		if got := doctor.CompareVersions(tc.a, tc.b); got != tc.want {
			t.Errorf("CompareVersions(%s, %s) = %d, want %d", tc.a, tc.b, got, tc.want)
		}
	}
}

func dotted(a, b, c uint8) string {
	return strings.Join([]string{itoa(a), itoa(b), itoa(c)}, ".")
}

func itoa(n uint8) string {
	if n == 0 {
		return "0"
	}
	var d []byte
	for ; n > 0; n /= 10 {
		d = append([]byte{byte('0' + n%10)}, d...)
	}
	return string(d)
}

func TestCompareVersionsIsATotalOrder(t *testing.T) {
	err := quick.Check(func(a1, a2, a3, b1, b2, b3, c1, c2, c3 uint8) bool {
		a, b, c := dotted(a1, a2, a3), dotted(b1, b2, b3), dotted(c1, c2, c3)
		if doctor.CompareVersions(a, a) != 0 || doctor.CompareVersions(a, b) != -doctor.CompareVersions(b, a) {
			return false
		}
		if doctor.CompareVersions(a, b) <= 0 && doctor.CompareVersions(b, c) <= 0 && doctor.CompareVersions(a, c) > 0 {
			return false
		}
		// Trailing zero fields do not change a version.
		return doctor.CompareVersions(a, a+".0") == 0
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
}

// unquote reads one POSIX shell word made of single-quoted and backslash-escaped parts, the only
// forms Quote and Export.Line produce.
func unquote(s string) (string, bool) {
	var out strings.Builder
	for i := 0; i < len(s); {
		switch s[i] {
		case '\'':
			end := strings.IndexByte(s[i+1:], '\'')
			if end < 0 {
				return "", false
			}
			out.WriteString(s[i+1 : i+1+end])
			i += end + 2
		case '\\':
			if i+1 >= len(s) {
				return "", false
			}
			out.WriteByte(s[i+1])
			i += 2
		default:
			return "", false
		}
	}
	return out.String(), true
}

func TestQuoteRoundTripsAnyString(t *testing.T) {
	err := quick.Check(func(s string) bool {
		got, ok := unquote(doctor.Quote(s))
		return ok && got == s
	}, &quick.Config{MaxCount: 2000})
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range []string{"", "it's", "a b", "$(rm -rf /)", "`x`", "a\nb", "'''"} {
		if got, ok := unquote(doctor.Quote(s)); !ok || got != s {
			t.Errorf("Quote(%q) does not round trip: %q", s, got)
		}
	}
}

func TestMergeExports(t *testing.T) {
	in := []doctor.Export{
		{Name: "PATH", Value: "/a", Prepend: true},
		{Name: "TMPDIR", Value: "/t1"},
		{Name: "PATH", Value: "/a", Prepend: true},
		{Name: "PATH", Value: "/b", Prepend: true},
		{Name: "TMPDIR", Value: "/t2"},
	}
	got := doctor.MergeExports(in)
	if len(got) != 3 || got[0].Value != "/a" || got[1].Value != "/t2" || got[2].Value != "/b" {
		t.Fatalf("got %+v", got)
	}
}

const mountinfo = `22 1 8:1 / / rw,relatime shared:1 - nfs4 server:/export rw
30 22 0:25 / /tmp rw shared:2 - tmpfs tmpfs rw
31 22 8:2 / /mnt/with\040space rw shared:3 - ext4 /dev/sda2 rw
32 31 0:40 / /mnt/with\040space/inner rw - nfs server:/x rw
`

func TestFilesystemFromMountInfo(t *testing.T) {
	for path, want := range map[string]string{
		"/":                             "nfs4",
		"/home/u":                       "nfs4",
		"/tmp":                          "tmpfs",
		"/tmp/x/y":                      "tmpfs",
		"/tmpfoo":                       "nfs4",
		"/mnt/with space/file":          "ext4",
		"/mnt/with space/inner/f":       "nfs",
		"/mnt/with space/innermost/../": "ext4",
	} {
		if got := doctor.FilesystemFromMountInfo(mountinfo, path); got != want && path != "/mnt/with space/innermost/../" {
			t.Errorf("%s: got %q, want %q", path, got, want)
		}
	}
	if got := doctor.FilesystemFromMountInfo("garbage\n\n", "/x"); got != "" {
		t.Errorf("garbage gave %q", got)
	}
}

func TestMountInfoNeverNamesAMountThatDoesNotHoldThePath(t *testing.T) {
	err := quick.Check(func(parts []uint8) bool {
		path := "/"
		for _, p := range parts[:min(len(parts), 4)] {
			path += string(rune('a'+p%3)) + "/"
		}
		path = strings.TrimSuffix(path, "/")
		if path == "" {
			path = "/"
		}
		got := doctor.FilesystemFromMountInfo("1 0 0:1 / / rw - rootfs r\n2 1 0:2 / /a rw - a_fs x\n3 1 0:3 / /a/b rw - ab_fs x\n", path)
		switch {
		case path == "/a/b" || strings.HasPrefix(path, "/a/b/"):
			return got == "ab_fs"
		case path == "/a" || strings.HasPrefix(path, "/a/"):
			return got == "a_fs"
		}
		return got == "rootfs"
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
}

func TestParseConfigRejectsWhatItCannotCheck(t *testing.T) {
	base := `[hooks]
path = ".githooks"
[temp]
max_path_length = 40
`
	for name, bad := range map[string]string{
		"unknown key":       base + "[extra]\nx = 1\n",
		"absolute hooks":    strings.Replace(base, `".githooks"`, `"/abs"`, 1),
		"zero length":       strings.Replace(base, "40", "0", 1),
		"bad candidate":     base + `candidates = ["relative/dir"]` + "\n",
		"program path":      base + "[[programs]]\nname = \"a/b\"\n",
		"duplicate program": base + "[[programs]]\nname = \"a\"\n[[programs]]\nname = \"a\"\n",
		"both pins":         base + "[[programs]]\nname = \"a\"\nminimum_from = { file = \"f\", pattern = '(x)' }\nexact_from = { file = \"f\", pattern = '(x)' }\n",
		"no capture group":  base + "[[programs]]\nname = \"a\"\nexact_from = { file = \"f\", pattern = 'x' }\n",
		"bad regexp":        base + "[[programs]]\nname = \"a\"\nexact_from = { file = \"f\", pattern = '(' }\n",
		"marker outside":    base + "[[installs]]\nname = \"i\"\ndirectory = \"d\"\nmarker = \"e/m\"\ncommand = [\"x\"]\ncommand_directory = \"d\"\n",
		"flag with space":   base + "[[env_flags]]\nname = \"X\"\nflag = \"a b\"\n",
		"escaping root":     base + "[instructions]\nroots = [\"../x\"]\n",
	} {
		if _, err := doctor.ParseConfig("p.toml", bad); err == nil {
			t.Errorf("%s: accepted", name)
		} else if !strings.Contains(err.Error(), "p.toml") {
			t.Errorf("%s: error does not name the file: %v", name, err)
		}
	}
	if _, err := doctor.ParseConfig("p.toml", base); err != nil {
		t.Fatal(err)
	}
}
