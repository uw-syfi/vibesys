package upgrade_test

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/upgrade"
	"github.com/uw-syfi/vibesys/cli/internal/upgrade/upgradetest"
)

// Every Source passes one contract: the fake release, and the HTTP implementation reading the
// same release through a transport that serves it.
func TestSourceContract(t *testing.T) {
	sources := map[string]func(*upgradetest.Release) upgrade.Source{
		"fake": func(r *upgradetest.Release) upgrade.Source { return r },
		"http": func(r *upgradetest.Release) upgrade.Source {
			return upgrade.HTTPSource{Client: &http.Client{Transport: r}, BaseURL: "https://releases.invalid/cli-latest"}
		},
	}
	for name, newSource := range sources {
		t.Run(name, func(t *testing.T) {
			release := upgradetest.New("1.2.3", nil)
			release.Set("present", []byte("content"))
			release.Inject("broken", upgradetest.Fault{Fail: true, FailAfter: 2})
			release.Set("broken", []byte("content"))
			source := newSource(release)

			if got := readAll(t, source, "present"); got != "content" {
				t.Fatalf("present = %q, want %q", got, "content")
			}
			if _, err := source.Open(context.Background(), "absent"); !errors.Is(err, upgrade.ErrAssetNotFound) {
				t.Fatalf("absent: error = %v, want ErrAssetNotFound", err)
			}
			body, err := source.Open(context.Background(), "broken")
			if err != nil {
				t.Fatal(err)
			}
			defer func() { _ = body.Close() }()
			if _, err := io.ReadAll(body); !errors.Is(err, upgradetest.ErrInjected) {
				t.Fatalf("broken read: error = %v, want the injected failure", err)
			}
		})
	}
}

func readAll(t *testing.T, source upgrade.Source, asset string) string {
	t.Helper()
	body, err := source.Open(context.Background(), asset)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = body.Close() }()
	data, err := io.ReadAll(body)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestHTTPSourceRequestsTheAssetUnderTheBaseURL(t *testing.T) {
	var requested []string
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		requested = append(requested, r.Method+" "+r.URL.String())
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader("x")), Header: http.Header{}}, nil
	})}
	// A trailing slash on the base must not double up.
	source := upgrade.HTTPSource{Client: client, BaseURL: "https://example.invalid/releases/download/cli-latest/"}
	readAll(t, source, "checksums.txt")
	want := []string{"GET https://example.invalid/releases/download/cli-latest/checksums.txt"}
	if strings.Join(requested, "|") != strings.Join(want, "|") {
		t.Fatalf("requests = %v, want %v", requested, want)
	}
}

func TestHTTPSourceRefusesAServerError(t *testing.T) {
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: http.StatusBadGateway, Status: "502 Bad Gateway", Body: io.NopCloser(strings.NewReader("")), Header: http.Header{}}, nil
	})}
	_, err := upgrade.HTTPSource{Client: client, BaseURL: "https://example.invalid"}.Open(context.Background(), "VERSION")
	if err == nil || !strings.Contains(err.Error(), "502") || errors.Is(err, upgrade.ErrAssetNotFound) {
		t.Fatalf("error = %v, want a 502 that is not ErrAssetNotFound", err)
	}
}

func TestLatestSourceReadsTheRollingTagOfTheReleasesURL(t *testing.T) {
	var requested string
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		requested = r.URL.String()
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader("")), Header: http.Header{}}, nil
	})}
	for _, releases := range []string{upgrade.DefaultReleasesURL, upgrade.DefaultReleasesURL + "/"} {
		source, err := upgrade.NewLatestSource(client, releases)
		if err != nil {
			t.Fatal(err)
		}
		readAll(t, source, "VERSION")
		if want := upgrade.DefaultReleasesURL + "/cli-latest/VERSION"; requested != want {
			t.Fatalf("requested %q, want %q", requested, want)
		}
	}
}

// The binary a source serves gets executed, so a URL that is not https is refused up front.
func TestLatestSourceRefusesAnythingButHTTPS(t *testing.T) {
	for _, url := range []string{"", "http://example.invalid/releases", "file:///tmp/releases", "ftp://example.invalid", "example.invalid"} {
		if _, err := upgrade.NewLatestSource(http.DefaultClient, url); err == nil {
			t.Errorf("%q was accepted", url)
		}
	}
}
