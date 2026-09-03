// Package auth is a minimal, single-user OAuth 2.1 authorization server that
// satisfies what claude.ai requires of a custom connector: metadata discovery,
// dynamic client registration, PKCE (S256), and refresh tokens.
//
// It keeps no state. Auth codes and tokens are signed JWTs, and the "consent"
// step is a single password known to the owner. Redirect URIs are restricted
// to an allowlist so a stolen code cannot be sent anywhere else.
package auth

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"html/template"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	sdkauth "github.com/modelcontextprotocol/go-sdk/auth"
	"github.com/modelcontextprotocol/go-sdk/oauthex"
)

const (
	codeTTL    = 5 * time.Minute
	accessTTL  = 24 * time.Hour
	refreshTTL = 180 * 24 * time.Hour
)

// Default redirect URIs used by Anthropic's hosted clients (web, desktop, mobile).
var defaultRedirects = []string{
	"https://claude.ai/api/mcp/auth_callback",
	"https://claude.com/api/mcp/auth_callback",
}

// Server implements the OAuth endpoints and the bearer-token check.
type Server struct {
	baseURL   string
	password  []byte
	key       []byte
	redirects map[string]bool
	now       func() time.Time
	log       *slog.Logger
	limiter   *failLimiter
}

// New builds a Server. extraRedirects are allowed in addition to the claude.ai ones.
func New(baseURL, ownerPassword string, signingKey []byte, extraRedirects []string, log *slog.Logger) *Server {
	s := &Server{
		baseURL:   strings.TrimRight(baseURL, "/"),
		password:  []byte(ownerPassword),
		key:       signingKey,
		redirects: map[string]bool{},
		now:       time.Now,
		log:       log,
		limiter:   newFailLimiter(5, time.Minute),
	}
	for _, u := range append(defaultRedirects, extraRedirects...) {
		s.redirects[u] = true
	}
	return s
}

// Register mounts the OAuth endpoints on mux.
func (a *Server) Register(mux *http.ServeMux) {
	mux.Handle("GET /.well-known/oauth-authorization-server", a.authServerMetadata())
	mux.Handle("GET /.well-known/oauth-protected-resource", a.resourceMetadata())
	mux.Handle("GET /.well-known/oauth-protected-resource/{rest...}", a.resourceMetadata())
	mux.HandleFunc("POST /register", a.handleRegister)
	mux.HandleFunc("GET /authorize", a.handleAuthorizeForm)
	mux.HandleFunc("POST /authorize", a.handleAuthorizeSubmit)
	mux.HandleFunc("POST /token", a.handleToken)
}

// RequireBearer wraps h so that only requests carrying a valid access token pass.
func (a *Server) RequireBearer(h http.Handler) http.Handler {
	verifier := func(ctx context.Context, token string, _ *http.Request) (*sdkauth.TokenInfo, error) {
		c, err := a.verify(token, "access", a.now())
		if err != nil {
			return nil, err
		}
		return &sdkauth.TokenInfo{UserID: "owner", Expiration: time.Unix(c.Expires, 0)}, nil
	}
	return sdkauth.RequireBearerToken(verifier, &sdkauth.RequireBearerTokenOptions{
		ResourceMetadataURL: a.baseURL + "/.well-known/oauth-protected-resource",
	})(h)
}

func (a *Server) authServerMetadata() http.Handler {
	meta := oauthex.AuthServerMeta{
		Issuer:                            a.baseURL,
		AuthorizationEndpoint:             a.baseURL + "/authorize",
		TokenEndpoint:                     a.baseURL + "/token",
		RegistrationEndpoint:              a.baseURL + "/register",
		ResponseTypesSupported:            []string{"code"},
		GrantTypesSupported:               []string{"authorization_code", "refresh_token"},
		TokenEndpointAuthMethodsSupported: []string{"none"},
		CodeChallengeMethodsSupported:     []string{"S256"},
	}
	return jsonHandler(meta)
}

func (a *Server) resourceMetadata() http.Handler {
	return sdkauth.ProtectedResourceMetadataHandler(&oauthex.ProtectedResourceMetadata{
		Resource:               a.baseURL + "/mcp",
		AuthorizationServers:   []string{a.baseURL},
		BearerMethodsSupported: []string{"header"},
		ResourceName:           "anki-mcp",
	})
}

// handleRegister implements RFC 7591 dynamic client registration. Since the
// server is stateless, the client id is random and nothing is stored: the
// redirect URI allowlist is what actually protects the flow.
func (a *Server) handleRegister(w http.ResponseWriter, r *http.Request) {
	var req oauthex.ClientRegistrationMetadata
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10)).Decode(&req); err != nil {
		oauthError(w, http.StatusBadRequest, "invalid_client_metadata", "bad JSON")
		return
	}
	for _, u := range req.RedirectURIs {
		if !a.redirects[u] {
			oauthError(w, http.StatusBadRequest, "invalid_redirect_uri", "redirect URI not allowed: "+u)
			return
		}
	}
	resp := oauthex.ClientRegistrationResponse{
		ClientID:                "anki-mcp-" + randomHex(8),
		ClientIDIssuedAt:        a.now(),
		RedirectURIs:            req.RedirectURIs,
		TokenEndpointAuthMethod: "none",
		GrantTypes:              []string{"authorization_code", "refresh_token"},
		ResponseTypes:           []string{"code"},
		ClientName:              req.ClientName,
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(resp)
}

type authorizeParams struct {
	ClientID, RedirectURI, State, Challenge, Method, ResponseType, Resource string
}

func paramsFrom(v url.Values) authorizeParams {
	return authorizeParams{
		ClientID: v.Get("client_id"), RedirectURI: v.Get("redirect_uri"), State: v.Get("state"),
		Challenge: v.Get("code_challenge"), Method: v.Get("code_challenge_method"),
		ResponseType: v.Get("response_type"), Resource: v.Get("resource"),
	}
}

func (a *Server) validate(p authorizeParams) string {
	switch {
	case p.ClientID == "":
		return "missing client_id"
	case !a.redirects[p.RedirectURI]:
		return "redirect_uri not allowed"
	case p.ResponseType != "code":
		return "response_type must be code"
	case p.Method != "S256" || p.Challenge == "":
		return "PKCE with S256 is required"
	}
	return ""
}

var formTmpl = template.Must(template.New("form").Parse(`<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>anki-mcp</title>
<style>body{font-family:system-ui;max-width:24rem;margin:4rem auto;padding:0 1rem}input,button{font-size:1rem;padding:.5rem;width:100%;box-sizing:border-box;margin:.25rem 0}.err{color:#b00}</style>
<h1>anki-mcp</h1>
<p><b>{{.Client}}</b> wants to review your Anki cards.</p>
{{if .Error}}<p class="err">{{.Error}}</p>{{end}}
<form method="post" action="/authorize">
{{range $k, $v := .Hidden}}<input type="hidden" name="{{$k}}" value="{{$v}}">{{end}}
<input type="password" name="password" placeholder="Owner password" autofocus autocomplete="current-password">
<button type="submit">Allow</button>
</form>`))

func (a *Server) renderForm(w http.ResponseWriter, p authorizeParams, errMsg string, status int) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(status)
	formTmpl.Execute(w, map[string]any{
		"Client": p.ClientID, "Error": errMsg,
		"Hidden": map[string]string{
			"client_id": p.ClientID, "redirect_uri": p.RedirectURI, "state": p.State,
			"code_challenge": p.Challenge, "code_challenge_method": p.Method,
			"response_type": p.ResponseType, "resource": p.Resource,
		},
	})
}

func (a *Server) handleAuthorizeForm(w http.ResponseWriter, r *http.Request) {
	p := paramsFrom(r.URL.Query())
	if msg := a.validate(p); msg != "" {
		http.Error(w, msg, http.StatusBadRequest)
		return
	}
	a.renderForm(w, p, "", http.StatusOK)
}

func (a *Server) handleAuthorizeSubmit(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseForm(); err != nil {
		http.Error(w, "bad form", http.StatusBadRequest)
		return
	}
	p := paramsFrom(r.PostForm)
	if msg := a.validate(p); msg != "" {
		http.Error(w, msg, http.StatusBadRequest)
		return
	}
	ip := clientIP(r)
	if a.limiter.blocked(ip, a.now()) {
		a.renderForm(w, p, "Too many attempts. Try again in a minute.", http.StatusTooManyRequests)
		return
	}
	if subtle.ConstantTimeCompare([]byte(r.PostForm.Get("password")), a.password) != 1 {
		a.limiter.fail(ip, a.now())
		a.log.Warn("authorize: wrong owner password", "ip", ip)
		a.renderForm(w, p, "Wrong password.", http.StatusUnauthorized)
		return
	}
	now := a.now()
	code := a.sign(claims{Type: "code", ClientID: p.ClientID, Redirect: p.RedirectURI, PKCE: p.Challenge,
		Nonce: randomHex(16), IssuedAt: now.Unix(), Expires: now.Add(codeTTL).Unix()})
	dest, _ := url.Parse(p.RedirectURI)
	q := dest.Query()
	q.Set("code", code)
	if p.State != "" {
		q.Set("state", p.State)
	}
	dest.RawQuery = q.Encode()
	a.log.Info("authorize: issued code", "client", p.ClientID)
	http.Redirect(w, r, dest.String(), http.StatusFound)
}

func (a *Server) handleToken(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseForm(); err != nil {
		oauthError(w, http.StatusBadRequest, "invalid_request", "bad form")
		return
	}
	now := a.now()
	clientID := r.PostForm.Get("client_id")
	switch r.PostForm.Get("grant_type") {
	case "authorization_code":
		c, err := a.verify(r.PostForm.Get("code"), "code", now)
		if err != nil {
			oauthError(w, http.StatusBadRequest, "invalid_grant", "code invalid or expired")
			return
		}
		if clientID != "" && clientID != c.ClientID {
			oauthError(w, http.StatusBadRequest, "invalid_grant", "client_id mismatch")
			return
		}
		if ru := r.PostForm.Get("redirect_uri"); ru != "" && ru != c.Redirect {
			oauthError(w, http.StatusBadRequest, "invalid_grant", "redirect_uri mismatch")
			return
		}
		sum := sha256.Sum256([]byte(r.PostForm.Get("code_verifier")))
		if base64.RawURLEncoding.EncodeToString(sum[:]) != c.PKCE {
			oauthError(w, http.StatusBadRequest, "invalid_grant", "PKCE verification failed")
			return
		}
		a.issueTokens(w, c.ClientID, now)
	case "refresh_token":
		c, err := a.verify(r.PostForm.Get("refresh_token"), "refresh", now)
		if err != nil {
			oauthError(w, http.StatusBadRequest, "invalid_grant", "refresh token invalid or expired")
			return
		}
		if clientID != "" && clientID != c.ClientID {
			oauthError(w, http.StatusBadRequest, "invalid_grant", "client_id mismatch")
			return
		}
		a.issueTokens(w, c.ClientID, now)
	default:
		oauthError(w, http.StatusBadRequest, "unsupported_grant_type", "")
	}
}

func (a *Server) issueTokens(w http.ResponseWriter, clientID string, now time.Time) {
	access := a.sign(claims{Type: "access", ClientID: clientID, Nonce: randomHex(16),
		IssuedAt: now.Unix(), Expires: now.Add(accessTTL).Unix()})
	refresh := a.sign(claims{Type: "refresh", ClientID: clientID, Nonce: randomHex(16),
		IssuedAt: now.Unix(), Expires: now.Add(refreshTTL).Unix()})
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	json.NewEncoder(w).Encode(map[string]any{
		"access_token": access, "token_type": "Bearer", "expires_in": int(accessTTL.Seconds()),
		"refresh_token": refresh,
	})
}

// --- helpers ---------------------------------------------------------------------------

func jsonHandler(v any) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(v)
	})
}

func oauthError(w http.ResponseWriter, status int, code, desc string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]string{"error": code, "error_description": desc})
}

func randomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}

func clientIP(r *http.Request) string {
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" { // Cloud Run sets this
		return strings.TrimSpace(strings.Split(xff, ",")[0])
	}
	return r.RemoteAddr
}

// failLimiter blocks an IP after too many failed password attempts. Per-instance
// only, which is fine: it exists to slow brute force, not to be a full WAF.
type failLimiter struct {
	mu     sync.Mutex
	max    int
	window time.Duration
	fails  map[string][]time.Time
}

func newFailLimiter(max int, window time.Duration) *failLimiter {
	return &failLimiter{max: max, window: window, fails: map[string][]time.Time{}}
}

func (l *failLimiter) prune(ip string, now time.Time) []time.Time {
	var keep []time.Time
	for _, t := range l.fails[ip] {
		if now.Sub(t) < l.window {
			keep = append(keep, t)
		}
	}
	l.fails[ip] = keep
	return keep
}

func (l *failLimiter) blocked(ip string, now time.Time) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return len(l.prune(ip, now)) >= l.max
}

func (l *failLimiter) fail(ip string, now time.Time) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.fails[ip] = append(l.prune(ip, now), now)
}
