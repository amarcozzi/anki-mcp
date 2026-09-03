package auth

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func newTestServer(t *testing.T) (*Server, *httptest.Server) {
	t.Helper()
	a := New("http://example.test", "hunter2", []byte("0123456789abcdef0123456789abcdef"), nil, slog.New(slog.DiscardHandler))
	mux := http.NewServeMux()
	a.Register(mux)
	mux.Handle("/mcp", a.RequireBearer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.Write([]byte("ok")) })))
	ts := httptest.NewServer(mux)
	t.Cleanup(ts.Close)
	return a, ts
}

func TestFullFlow(t *testing.T) {
	_, ts := newTestServer(t)
	client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}

	// unauthenticated /mcp gets a 401 pointing at resource metadata
	resp, _ := client.Get(ts.URL + "/mcp")
	if resp.StatusCode != 401 || !strings.Contains(resp.Header.Get("WWW-Authenticate"), "oauth-protected-resource") {
		t.Fatalf("unauthenticated: %d %q", resp.StatusCode, resp.Header.Get("WWW-Authenticate"))
	}

	// dynamic registration
	resp, _ = client.Post(ts.URL+"/register", "application/json",
		strings.NewReader(`{"client_name":"claude","redirect_uris":["https://claude.ai/api/mcp/auth_callback"]}`))
	if resp.StatusCode != 201 {
		t.Fatalf("register: %d", resp.StatusCode)
	}
	var reg struct {
		ClientID string `json:"client_id"`
	}
	json.NewDecoder(resp.Body).Decode(&reg)

	// registration with a foreign redirect is refused
	resp, _ = client.Post(ts.URL+"/register", "application/json",
		strings.NewReader(`{"redirect_uris":["https://evil.example/cb"]}`))
	if resp.StatusCode != 400 {
		t.Fatalf("register evil: %d", resp.StatusCode)
	}

	// PKCE
	verifier := "correct-horse-battery-staple-correct-horse-battery-staple"
	sum := sha256.Sum256([]byte(verifier))
	challenge := base64.RawURLEncoding.EncodeToString(sum[:])
	form := url.Values{"client_id": {reg.ClientID}, "redirect_uri": {"https://claude.ai/api/mcp/auth_callback"},
		"state": {"xyz"}, "code_challenge": {challenge}, "code_challenge_method": {"S256"}, "response_type": {"code"}}

	// wrong password
	resp, _ = client.PostForm(ts.URL+"/authorize", withPassword(form, "nope"))
	if resp.StatusCode != 401 {
		t.Fatalf("wrong password: %d", resp.StatusCode)
	}
	// right password -> redirect with code
	resp, _ = client.PostForm(ts.URL+"/authorize", withPassword(form, "hunter2"))
	if resp.StatusCode != 302 {
		t.Fatalf("authorize: %d", resp.StatusCode)
	}
	loc, _ := url.Parse(resp.Header.Get("Location"))
	if loc.Host != "claude.ai" || loc.Query().Get("state") != "xyz" || loc.Query().Get("code") == "" {
		t.Fatalf("redirect: %s", loc)
	}
	code := loc.Query().Get("code")

	// wrong verifier
	resp, _ = client.PostForm(ts.URL+"/token", url.Values{"grant_type": {"authorization_code"}, "code": {code},
		"client_id": {reg.ClientID}, "redirect_uri": {"https://claude.ai/api/mcp/auth_callback"}, "code_verifier": {"wrong"}})
	if resp.StatusCode != 400 {
		t.Fatalf("token bad verifier: %d", resp.StatusCode)
	}
	// right verifier
	resp, _ = client.PostForm(ts.URL+"/token", url.Values{"grant_type": {"authorization_code"}, "code": {code},
		"client_id": {reg.ClientID}, "redirect_uri": {"https://claude.ai/api/mcp/auth_callback"}, "code_verifier": {verifier}})
	if resp.StatusCode != 200 {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("token: %d %s", resp.StatusCode, b)
	}
	var tok struct {
		Access  string `json:"access_token"`
		Refresh string `json:"refresh_token"`
	}
	json.NewDecoder(resp.Body).Decode(&tok)

	// bearer works
	req, _ := http.NewRequest("GET", ts.URL+"/mcp", nil)
	req.Header.Set("Authorization", "Bearer "+tok.Access)
	resp, _ = client.Do(req)
	if resp.StatusCode != 200 {
		t.Fatalf("bearer: %d", resp.StatusCode)
	}
	// refresh token is not accepted as an access token
	req.Header.Set("Authorization", "Bearer "+tok.Refresh)
	resp, _ = client.Do(req)
	if resp.StatusCode != 401 {
		t.Fatalf("refresh as access: %d", resp.StatusCode)
	}
	// refresh grant
	resp, _ = client.PostForm(ts.URL+"/token", url.Values{"grant_type": {"refresh_token"}, "refresh_token": {tok.Refresh}, "client_id": {reg.ClientID}})
	if resp.StatusCode != 200 {
		t.Fatalf("refresh: %d", resp.StatusCode)
	}
}

func TestExpiry(t *testing.T) {
	a, _ := newTestServer(t)
	now := time.Now()
	tok := a.sign(claims{Type: "access", Nonce: "n", IssuedAt: now.Unix(), Expires: now.Add(time.Hour).Unix()})
	if _, err := a.verify(tok, "access", now.Add(2*time.Hour)); err == nil {
		t.Fatal("expected expired token to fail")
	}
	if _, err := a.verify(tok+"x", "access", now); err == nil {
		t.Fatal("expected tampered token to fail")
	}
}

func withPassword(v url.Values, pw string) url.Values {
	out := url.Values{}
	for k, vs := range v {
		out[k] = vs
	}
	out.Set("password", pw)
	return out
}
