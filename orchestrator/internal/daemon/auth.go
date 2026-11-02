package daemon

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// Authority mints and checks the token that proves a connection speaks for one session of one
// agent. The token is derived from a secret only the daemon and its children's environments hold,
// so a tool cannot name another agent in its arguments and be believed: the daemon never reads the
// caller from a request. All the agents of a user share a Unix account, so this fences an agent's
// own mistakes and a stale session, and does not hold against a hostile process of the same user.
type Authority struct{ secret []byte }

// NewAuthority returns an authority over a secret of at least 16 bytes.
func NewAuthority(secret []byte) (Authority, error) {
	if len(secret) < 16 {
		return Authority{}, fmt.Errorf("daemon: the token secret has %d bytes, and needs at least 16", len(secret))
	}
	return Authority{secret: append([]byte(nil), secret...)}, nil
}

// Token is what a session carries in its environment.
func (a Authority) Token(s core.Session) string {
	mac := hmac.New(sha256.New, a.secret)
	fmt.Fprintf(mac, "%s/%d", s.Agent, s.N)
	return hex.EncodeToString(mac.Sum(nil))
}

// Valid says whether a token belongs to a session.
func (a Authority) Valid(s core.Session, token string) bool {
	want, err := hex.DecodeString(a.Token(s))
	got, gotErr := hex.DecodeString(token)
	return err == nil && gotErr == nil && hmac.Equal(want, got)
}
