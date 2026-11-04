package upgrade

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
)

// Where releases live. A version's assets are under tag cli-v<version>; the newest release's
// are also under the rolling tag cli-latest, which is what Upgrade reads. GitHub's own "latest"
// marker is not used: the repository publishes other releases, and whichever was created last
// would take it.
const (
	// DefaultReleasesURL is the prefix GitHub serves release assets under.
	DefaultReleasesURL = "https://github.com/uw-syfi/vibesys/releases/download"
	// LatestTag is the tag whose assets are always those of the newest release.
	LatestTag = "cli-latest"
	// ReleasesURLEnv names the environment variable that replaces DefaultReleasesURL, for a mirror
	// or a prerelease check. install.sh reads the same variable.
	ReleasesURLEnv = "VIBESYS_CLI_RELEASES_URL"
)

// NewLatestSource returns the Source for the newest release under releasesURL, which is
// DefaultReleasesURL or its replacement. The URL must be https, because the binary it serves is
// about to be executed.
func NewLatestSource(client *http.Client, releasesURL string) (Source, error) {
	if !strings.HasPrefix(releasesURL, "https://") {
		return nil, fmt.Errorf("%s must start with https://, got %q", ReleasesURLEnv, releasesURL)
	}
	return HTTPSource{Client: client, BaseURL: strings.TrimSuffix(releasesURL, "/") + "/" + LatestTag}, nil
}

// ErrAssetNotFound means the release has no asset of that name.
var ErrAssetNotFound = errors.New("release asset not found")

// Source is where releases come from. It always speaks of the newest release.
type Source interface {
	// Open returns the content of the named asset of the newest release. The caller closes it.
	// A name the release does not have is an error that wraps ErrAssetNotFound.
	Open(ctx context.Context, asset string) (io.ReadCloser, error)
}

// HTTPSource reads a release from a base URL that serves each asset at <BaseURL>/<name>, which
// is how GitHub serves the assets of a release tag.
type HTTPSource struct {
	// Client makes the requests; the caller sets its transport and its limits.
	Client *http.Client
	// BaseURL has no trailing slash.
	BaseURL string
}

// Open implements Source.
func (s HTTPSource) Open(ctx context.Context, asset string) (io.ReadCloser, error) {
	url := strings.TrimSuffix(s.BaseURL, "/") + "/" + asset
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("request %s: %w", url, err)
	}
	response, err := s.Client.Do(request)
	if err != nil {
		return nil, fmt.Errorf("fetch %s: %w", url, err)
	}
	switch response.StatusCode {
	case http.StatusOK:
		return response.Body, nil
	case http.StatusNotFound:
		_ = response.Body.Close()
		return nil, fmt.Errorf("%w: %s", ErrAssetNotFound, url)
	default:
		_ = response.Body.Close()
		return nil, fmt.Errorf("fetch %s: %s", url, response.Status)
	}
}
