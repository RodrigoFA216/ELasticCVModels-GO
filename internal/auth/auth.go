package auth

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"errors"
	"strings"
	"time"

	"kinich-go/internal/domain"
	"kinich-go/internal/store"
)

type Auth struct{ Store *store.Store }

func Prepare(scopes []string, expires time.Time) (string, string, []byte, error) {
	if expires.Before(time.Now()) || len(scopes) == 0 {
		return "", "", nil, domain.Error(422, "invalid_key", "Caducidad y scopes requeridos")
	}
	allowed := map[string]bool{"read": true, "control": true, "events": true, "predict": true, "admin": true, "*": true}
	for _, s := range scopes {
		if !allowed[s] {
			return "", "", nil, domain.Error(422, "invalid_scope", "Scope desconocido")
		}
	}
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", "", nil, err
	}
	token := "kg_" + base64.RawURLEncoding.EncodeToString(b)
	h := sha256.Sum256([]byte(token))
	return domain.ID(), token, h[:], nil
}
func (a Auth) Issue(ctx context.Context, o domain.Owner, scopes []string, expires time.Time) (string, string, error) {
	id, token, h, err := Prepare(scopes, expires)
	if err != nil {
		return "", "", err
	}
	_, err = a.Store.DB.ExecContext(ctx, "INSERT INTO credentials(id,environment,tenant_id,hash,scopes,expires_at) VALUES(?,?,?,?,?,?)", id, o.Environment, o.TenantID, h, domain.JSON(scopes), expires.UTC().Format(time.RFC3339Nano))
	return id, token, err
}
func (a Auth) Verify(ctx context.Context, token string) (domain.Principal, error) {
	var p domain.Principal
	if !strings.HasPrefix(token, "kg_") || len(token) != 46 {
		return p, domain.Error(401, "unauthorized", "Credencial inválida")
	}
	h := sha256.Sum256([]byte(token))
	var scopes, expiry string
	err := a.Store.DB.QueryRowContext(ctx, "SELECT id,environment,tenant_id,scopes,expires_at FROM credentials WHERE hash=? AND revoked=0", h[:]).Scan(&p.KeyID, &p.Environment, &p.TenantID, &scopes, &expiry)
	if errors.Is(err, sql.ErrNoRows) || store.Expired(expiry) {
		return p, domain.Error(401, "unauthorized", "Credencial inválida")
	}
	if err != nil {
		return p, err
	}
	p.Scopes, err = store.Decode[[]string](scopes)
	return p, err
}
func (a Auth) Revoke(ctx context.Context, o domain.Owner, id string) error {
	res, err := a.Store.DB.ExecContext(ctx, "UPDATE credentials SET revoked=1 WHERE environment=? AND tenant_id=? AND id=?", o.Environment, o.TenantID, id)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return domain.NotFound
	}
	return nil
}
