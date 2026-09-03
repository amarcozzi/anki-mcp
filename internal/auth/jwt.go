package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	sdkauth "github.com/modelcontextprotocol/go-sdk/auth"
)

// claims is the payload of every token this server issues. Tokens are HS256
// JWTs so the server needs no storage: everything it must remember is inside
// the token and protected by the signature.
type claims struct {
	Type     string `json:"typ"`           // "code", "access", "refresh"
	ClientID string `json:"cid,omitempty"` // OAuth client the token was issued to
	Redirect string `json:"ru,omitempty"`  // code only: redirect URI it must be exchanged with
	PKCE     string `json:"cc,omitempty"`  // code only: S256 code challenge
	Nonce    string `json:"jti"`
	IssuedAt int64  `json:"iat"`
	Expires  int64  `json:"exp"`
}

var b64 = base64.RawURLEncoding

func (a *Server) sign(c claims) string {
	header := b64.EncodeToString([]byte(`{"alg":"HS256","typ":"JWT"}`))
	payload, _ := json.Marshal(c)
	signingInput := header + "." + b64.EncodeToString(payload)
	mac := hmac.New(sha256.New, a.key)
	mac.Write([]byte(signingInput))
	return signingInput + "." + b64.EncodeToString(mac.Sum(nil))
}

// errBadToken wraps the SDK's sentinel so RequireBearerToken answers 401, not 500.
var errBadToken = fmt.Errorf("%w", sdkauth.ErrInvalidToken)

// verify checks signature and expiry and that the token is of the wanted type.
func (a *Server) verify(token, wantType string, now time.Time) (claims, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return claims{}, errBadToken
	}
	mac := hmac.New(sha256.New, a.key)
	mac.Write([]byte(parts[0] + "." + parts[1]))
	sig, err := b64.DecodeString(parts[2])
	if err != nil || !hmac.Equal(sig, mac.Sum(nil)) {
		return claims{}, errBadToken
	}
	payload, err := b64.DecodeString(parts[1])
	if err != nil {
		return claims{}, errBadToken
	}
	var c claims
	if err := json.Unmarshal(payload, &c); err != nil {
		return claims{}, errBadToken
	}
	if c.Type != wantType {
		return claims{}, fmt.Errorf("%w: wrong type", errBadToken)
	}
	if now.Unix() >= c.Expires {
		return claims{}, fmt.Errorf("%w: expired", errBadToken)
	}
	return c, nil
}
