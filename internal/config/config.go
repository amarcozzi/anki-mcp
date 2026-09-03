// Package config reads the server's configuration from the environment.
// Secrets are only ever read here and are never logged.
package config

import (
	"cmp"
	"fmt"
	"os"
	"strings"
)

// Config is everything the server needs. The four secret fields are supplied
// via Secret Manager on Cloud Run (--set-secrets) and via a git-ignored .env
// locally.
type Config struct {
	AnkiWebUsername string // secret
	AnkiWebPassword string // secret
	OwnerPassword   string // secret: what you type on the OAuth consent page
	JWTSigningKey   string // secret: HMAC key for auth codes and tokens

	BaseURL           string   // public URL of this server, e.g. https://anki-mcp-xxxx.run.app
	Port              string   // listen port, default 8080 (Cloud Run sets PORT)
	ExtraRedirectURIs []string // additional OAuth redirect URIs beyond claude.ai/claude.com
}

// Load reads the environment and fails if any required value is missing.
func Load() (Config, error) {
	c := Config{
		AnkiWebUsername: os.Getenv("ANKIWEB_USERNAME"),
		AnkiWebPassword: os.Getenv("ANKIWEB_PASSWORD"),
		OwnerPassword:   os.Getenv("OWNER_PASSWORD"),
		JWTSigningKey:   os.Getenv("JWT_SIGNING_KEY"),
		BaseURL:         strings.TrimRight(os.Getenv("BASE_URL"), "/"),
		Port:            cmp.Or(os.Getenv("PORT"), "8080"),
	}
	if v := os.Getenv("EXTRA_REDIRECT_URIS"); v != "" {
		for _, u := range strings.Split(v, ",") {
			if u = strings.TrimSpace(u); u != "" {
				c.ExtraRedirectURIs = append(c.ExtraRedirectURIs, u)
			}
		}
	}
	required := []struct{ name, val string }{
		{"ANKIWEB_USERNAME", c.AnkiWebUsername},
		{"ANKIWEB_PASSWORD", c.AnkiWebPassword},
		{"OWNER_PASSWORD", c.OwnerPassword},
		{"JWT_SIGNING_KEY", c.JWTSigningKey},
		{"BASE_URL", c.BaseURL},
	}
	var missing []string
	for _, r := range required {
		if r.val == "" {
			missing = append(missing, r.name)
		}
	}
	if len(missing) > 0 {
		return c, fmt.Errorf("missing required environment variables: %s", strings.Join(missing, ", "))
	}
	if len(c.JWTSigningKey) < 32 {
		return c, fmt.Errorf("JWT_SIGNING_KEY must be at least 32 characters (try: openssl rand -base64 48)")
	}
	return c, nil
}
