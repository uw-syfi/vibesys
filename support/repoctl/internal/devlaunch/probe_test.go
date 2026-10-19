package devlaunch

import (
	"reflect"
	"strconv"
	"strings"
	"testing"
	"testing/quick"
)

func TestParseProbeTable(t *testing.T) {
	for _, tc := range []struct {
		out   string
		want  RemoteState
		found bool
		bad   bool
	}{
		{out: "missing\n"},
		{out: "plain\n/srv/v\n", want: RemoteState{Kind: CheckoutPlain, Checkout: "/srv/v"}, found: true},
		{out: "git\n/srv/v\nabc\nmain\n0\n", want: RemoteState{Kind: CheckoutGit, Checkout: "/srv/v", Head: "abc", Branch: "main"}, found: true},
		{out: "git\n/srv/v\nabc\ndev/x\n       3\n", want: RemoteState{Kind: CheckoutGit, Checkout: "/srv/v", Head: "abc", Branch: "dev/x", Dirty: true}, found: true},
		{out: "git\n/srv/v\nabc\nmain\n", bad: true},
		{out: "git\n/srv/v\nabc\nmain\nmany\n", bad: true},
		{out: "plain\n", bad: true},
		{out: "something else\n", bad: true},
		{out: "", bad: true},
	} {
		got, found, err := ParseProbe(tc.out)
		if (err != nil) != tc.bad || found != tc.found || !reflect.DeepEqual(got, tc.want) {
			t.Errorf("ParseProbe(%q) = %+v, %v, %v", tc.out, got, found, err)
		}
	}
}

func TestParseProbeRoundTripsAnyGitState(t *testing.T) {
	check := func(head, branch string, changed uint8) bool {
		clean := func(s string) string {
			return "x" + strings.Map(func(r rune) rune {
				if r == '\n' || r == '\r' {
					return -1
				}
				return r
			}, s)
		}
		head, branch = clean(head), clean(branch)
		out := "git\n/p\n" + head + "\n" + branch + "\n" + strconv.Itoa(int(changed)) + "\n"
		got, found, err := ParseProbe(out)
		return err == nil && found && got.Head == strings.TrimSpace(head) && got.Branch == strings.TrimSpace(branch) && got.Dirty == (changed > 0)
	}
	if err := quick.Check(check, nil); err != nil {
		t.Fatal(err)
	}
}

func TestValidateRemotePath(t *testing.T) {
	for path, ok := range map[string]bool{"/srv/v": true, "~": true, "~/v": true, "": false, "v": false, "-x": false, "/a'b": false, "/a\nb": false, "/a\\b": false, "~user/v": false} {
		if (ValidateRemotePath(path) == nil) != ok {
			t.Errorf("ValidateRemotePath(%q) ok=%v, want %v", path, !ok, ok)
		}
	}
}

func TestResolveCheckout(t *testing.T) {
	saved, _ := SeedHosts("host-a", "~/vs")
	files := []HostsFile{{Path: "~/one", Found: false}, {Path: "~/two", Data: saved, Found: true}}
	if got, err := ResolveCheckout("", "host-a", files); err != nil || got != "~/vs" {
		t.Fatalf("saved: got %q, %v", got, err)
	}
	if got, err := ResolveCheckout("/named", "host-a", files); err != nil || got != "/named" {
		t.Fatalf("an override beats the saved one: got %q, %v", got, err)
	}
	_, err := ResolveCheckout("", "host-b", files)
	if err == nil || !strings.Contains(err.Error(), "~/one (missing)") || !strings.Contains(err.Error(), "~/two (no entry") || !strings.Contains(err.Error(), "host-b") {
		t.Fatalf("an unsaved machine must name the files and the machine: %v", err)
	}
}

func TestHelloReadiness(t *testing.T) {
	h, err := ParseHello([]byte(`{"type":"hello","request_id":"1","protocol_version":1,"features":[],"vibesys_version":"0.1.1","checkout":{"root":"/r","commit":"abc","dirty":false}}`))
	if err != nil {
		t.Fatal(err)
	}
	if ok, _ := HelloReady(h, "abc"); !ok {
		t.Error("matching commit is not ready")
	}
	if ok, seen := HelloReady(h, "def"); ok || !strings.Contains(seen, "abc") {
		t.Errorf("another commit: ready=%v seen=%q", ok, seen)
	}
	if ok, _ := HelloReady(h, ""); !ok {
		t.Error("no wanted commit: any checkout is ready")
	}
	dist, _ := ParseHello([]byte(`{"type":"hello","vibesys_version":"0.1.1","checkout":null}`))
	if ok, _ := HelloReady(dist, ""); ok {
		t.Error("a helper from an installed distribution is not the shipped checkout")
	}
	for _, bad := range []string{`not json`, `{"type":"error","message":"incompatible"}`} {
		if _, err := ParseHello([]byte(bad)); err == nil {
			t.Errorf("ParseHello(%q) accepted", bad)
		}
	}
}

func TestSavedCheckoutAndSeedRoundTrip(t *testing.T) {
	data, err := SeedHosts("host-a", "/srv/v")
	if err != nil {
		t.Fatal(err)
	}
	if got := SavedCheckout(data, "host-a"); got != "/srv/v" {
		t.Fatalf("got %q", got)
	}
	if SavedCheckout(data, "host-b") != "" || SavedCheckout([]byte("{"), "host-a") != "" || SavedCheckout([]byte(`{"version":1,"hosts":{}}`), "host-a") != "" {
		t.Fatal("unrelated or malformed files must save nothing")
	}
}
