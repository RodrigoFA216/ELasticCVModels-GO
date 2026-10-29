package store

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"kinich-go/internal/domain"
	_ "modernc.org/sqlite"
)

//go:embed schema.sql
var schema string

type Store struct {
	DB        *sql.DB
	aead      cipher.AEAD
	lock      *os.File
	Path      string
	cursorKey []byte
}

func Open(path string, key []byte) (*Store, error) {
	if len(key) != 32 {
		return nil, fmt.Errorf("se requiere una clave AES de 32 bytes")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path+".lock", os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	if err = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return nil, fmt.Errorf("ya existe un controlador activo: %w", err)
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		f.Close()
		return nil, err
	}
	db.SetMaxOpenConns(1)
	if _, err = db.Exec(schema); err != nil {
		db.Close()
		f.Close()
		return nil, err
	}
	if err = os.Chmod(path, 0600); err != nil {
		db.Close()
		f.Close()
		return nil, err
	}
	block, _ := aes.NewCipher(key)
	aead, _ := cipher.NewGCM(block)
	s := &Store{DB: db, aead: aead, lock: f, Path: path, cursorKey: append([]byte(nil), key...)}
	var check []byte
	owner := domain.Owner{Environment: "system", TenantID: "key-verification"}
	err = db.QueryRow("SELECT value FROM metadata WHERE key='key_check'").Scan(&check)
	if errors.Is(err, sql.ErrNoRows) {
		check, err = s.Seal(owner, "kinich-store-v1")
		if err == nil {
			_, err = db.Exec("INSERT INTO metadata VALUES('key_check',?)", check)
		}
	} else if err == nil {
		var plain string
		plain, err = s.Unseal(owner, check)
		if err == nil && plain != "kinich-store-v1" {
			err = errors.New("clave del almacén incompatible")
		}
	}
	if err != nil {
		s.Close()
		return nil, fmt.Errorf("clave del almacén incompatible: %w", err)
	}
	return s, nil
}
func (s *Store) Close() error {
	err := s.DB.Close()
	if s.lock != nil {
		s.lock.Close()
	}
	return err
}
func (s *Store) Seal(o domain.Owner, value string) ([]byte, error) {
	n := make([]byte, s.aead.NonceSize())
	if _, err := rand.Read(n); err != nil {
		return nil, err
	}
	return s.aead.Seal(n, n, []byte(value), []byte(o.Key())), nil
}
func (s *Store) Unseal(o domain.Owner, value []byte) (string, error) {
	n := s.aead.NonceSize()
	if len(value) < n {
		return "", errors.New("secreto cifrado inválido")
	}
	p, err := s.aead.Open(nil, value[:n], value[n:], []byte(o.Key()))
	return string(p), err
}
func Digest(v string) string            { h := sha256.Sum256([]byte(v)); return hex.EncodeToString(h[:]) }
func Decode[T any](s string) (T, error) { var v T; err := json.Unmarshal([]byte(s), &v); return v, err }
func Missing(err error) error {
	if errors.Is(err, sql.ErrNoRows) {
		return domain.NotFound
	}
	return err
}
func Conflict(err error) error {
	if err != nil && strings.Contains(err.Error(), "UNIQUE constraint") {
		return domain.Conflict
	}
	return err
}
func (s *Store) Tenant(ctx context.Context, o domain.Owner) (domain.Tenant, error) {
	var q string
	err := s.DB.QueryRowContext(ctx, "SELECT quota FROM tenants WHERE environment=? AND tenant_id=?", o.Environment, o.TenantID).Scan(&q)
	if err != nil {
		return domain.Tenant{}, Missing(err)
	}
	quota, err := Decode[domain.Quota](q)
	return domain.Tenant{Owner: o, Quota: quota}, err
}
func (s *Store) PutTenant(ctx context.Context, t domain.Tenant) error {
	_, err := s.DB.ExecContext(ctx, "INSERT INTO tenants VALUES(?,?,?) ON CONFLICT(environment,tenant_id) DO UPDATE SET quota=excluded.quota", t.Environment, t.TenantID, domain.JSON(t.Quota))
	return err
}
func (s *Store) Command(ctx context.Context, p domain.Principal, key, digest, kind, requestID string, fn func(*sql.Tx, *domain.Operation) error) (domain.Operation, error) {
	var op domain.Operation
	if len(key) < 8 || len(key) > 120 {
		return op, domain.Error(400, "idempotency_required", "Idempotency-Key debe tener de 8 a 120 caracteres")
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return op, err
	}
	defer tx.Rollback()
	var data, old string
	err = tx.QueryRowContext(ctx, "SELECT digest,data FROM operations WHERE environment=? AND tenant_id=? AND key=?", p.Environment, p.TenantID, key).Scan(&old, &data)
	if err == nil {
		if old != digest {
			return op, domain.Conflict
		}
		return Decode[domain.Operation](data)
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return op, err
	}
	op = domain.Operation{Owner: p.Owner, ID: domain.ID(), RequestID: requestID, Kind: kind, State: "succeeded", CreatedAt: domain.Now()}
	if err = fn(tx, &op); err != nil {
		return op, err
	}
	_, err = tx.ExecContext(ctx, "INSERT INTO operations VALUES(?,?,?,?,?,?)", op.ID, p.Environment, p.TenantID, key, digest, domain.JSON(op))
	if err != nil {
		return op, Conflict(err)
	}
	if _, err = tx.ExecContext(ctx, "INSERT INTO audit(environment,tenant_id,actor,action,resource,request_id,result,created_at) VALUES(?,?,?,?,?,?,?,?)", p.Environment, p.TenantID, p.KeyID, kind, op.ResourceID, requestID, op.State, domain.Now()); err != nil {
		return op, err
	}
	return op, tx.Commit()
}
func (s *Store) Operation(ctx context.Context, o domain.Owner, id string) (domain.Operation, error) {
	var d string
	err := s.DB.QueryRowContext(ctx, "SELECT data FROM operations WHERE environment=? AND tenant_id=? AND id=?", o.Environment, o.TenantID, id).Scan(&d)
	if err != nil {
		return domain.Operation{}, Missing(err)
	}
	return Decode[domain.Operation](d)
}
func UpdateOperation(tx *sql.Tx, o domain.Owner, id, state string, result any) error {
	var d string
	if err := tx.QueryRow("SELECT data FROM operations WHERE environment=? AND tenant_id=? AND id=?", o.Environment, o.TenantID, id).Scan(&d); err != nil {
		return err
	}
	op, err := Decode[domain.Operation](d)
	if err != nil {
		return err
	}
	op.State = state
	op.Result = result
	_, err = tx.Exec("UPDATE operations SET data=? WHERE environment=? AND tenant_id=? AND id=?", domain.JSON(op), o.Environment, o.TenantID, id)
	return err
}

type Querier interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

func Camera(ctx context.Context, q Querier, o domain.Owner, id string) (domain.Camera, error) {
	var d string
	var encrypted []byte
	err := q.QueryRowContext(ctx, "SELECT data,source FROM cameras WHERE environment=? AND tenant_id=? AND id=?", o.Environment, o.TenantID, id).Scan(&d, &encrypted)
	if err != nil {
		return domain.Camera{}, Missing(err)
	}
	return Decode[domain.Camera](d)
}
func (s *Store) Camera(ctx context.Context, o domain.Owner, id string) (domain.Camera, error) {
	return Camera(ctx, s.DB, o, id)
}
func (s *Store) Source(ctx context.Context, o domain.Owner, id string) (string, error) {
	var b []byte
	err := s.DB.QueryRowContext(ctx, "SELECT source FROM cameras WHERE environment=? AND tenant_id=? AND id=?", o.Environment, o.TenantID, id).Scan(&b)
	if err != nil {
		return "", Missing(err)
	}
	return s.Unseal(o, b)
}
func SaveCamera(tx *sql.Tx, c domain.Camera) error {
	_, err := tx.Exec("UPDATE cameras SET enabled=?,revision=?,data=? WHERE environment=? AND tenant_id=? AND id=?", c.Enabled, c.Revision, domain.JSON(c), c.Environment, c.TenantID, c.ID)
	return err
}
func Execution(ctx context.Context, q Querier, o domain.Owner, id string) (domain.Execution, error) {
	var d string
	err := q.QueryRowContext(ctx, "SELECT data FROM executions WHERE environment=? AND tenant_id=? AND id=?", o.Environment, o.TenantID, id).Scan(&d)
	if err != nil {
		return domain.Execution{}, Missing(err)
	}
	return Decode[domain.Execution](d)
}
func (s *Store) Execution(ctx context.Context, o domain.Owner, id string) (domain.Execution, error) {
	return Execution(ctx, s.DB, o, id)
}
func SaveExecution(tx *sql.Tx, e domain.Execution) error {
	_, err := tx.Exec("UPDATE executions SET state=?,desired=?,generation=?,data=? WHERE environment=? AND tenant_id=? AND id=?", e.State, e.Desired, e.Generation, domain.JSON(e), e.Environment, e.TenantID, e.ID)
	return err
}
func InsertExecution(tx *sql.Tx, e domain.Execution) error {
	_, err := tx.Exec("INSERT INTO executions VALUES(?,?,?,?,?,?,?,?)", e.ID, e.Environment, e.TenantID, e.CameraID, e.State, e.Desired, e.Generation, domain.JSON(e))
	return Conflict(err)
}
func (s *Store) Executions(ctx context.Context, active bool) ([]domain.Execution, error) {
	q := "SELECT data FROM executions"
	if active {
		q += " WHERE state IN ('queued','preparing','running','stopping')"
	}
	q += " ORDER BY rowid"
	rows, err := s.DB.QueryContext(ctx, q)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []domain.Execution{}
	for rows.Next() {
		var d string
		if err = rows.Scan(&d); err != nil {
			return nil, err
		}
		v, err := Decode[domain.Execution](d)
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}
func (s *Store) Cameras(ctx context.Context, o domain.Owner) ([]domain.Camera, error) {
	rows, err := s.DB.QueryContext(ctx, "SELECT data FROM cameras WHERE environment=? AND tenant_id=? ORDER BY rowid LIMIT 1000", o.Environment, o.TenantID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []domain.Camera{}
	for rows.Next() {
		var d string
		if err = rows.Scan(&d); err != nil {
			return nil, err
		}
		v, err := Decode[domain.Camera](d)
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}
func (s *Store) Tx(ctx context.Context, fn func(*sql.Tx) error) error {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err = fn(tx); err != nil {
		return err
	}
	return tx.Commit()
}
func Retire(tx *sql.Tx, e *domain.Execution, terminal, reason string) error {
	if !e.Active() {
		return nil
	}
	e.Desired = "stop"
	e.Terminal = terminal
	e.Reason = reason
	if e.State == "queued" {
		e.State = terminal
		e.FinishedAt = domain.Now()
		if err := UpdateOperation(tx, e.Owner, e.OperationID, "succeeded", *e); err != nil {
			return err
		}
	} else {
		e.State = "stopping"
	}
	return SaveExecution(tx, *e)
}
func (s *Store) Backup(ctx context.Context, path string) error {
	if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
		return errors.New("el destino del respaldo debe ser nuevo")
	}
	_, err := s.DB.ExecContext(ctx, "VACUUM INTO ?", path)
	if err != nil {
		return err
	}
	return os.Chmod(path, 0600)
}
func (s *Store) Recover(ctx context.Context) error {
	return s.Tx(ctx, func(tx *sql.Tx) error {
		rows, err := tx.QueryContext(ctx, "SELECT data FROM executions WHERE state IN ('preparing','running','stopping')")
		if err != nil {
			return err
		}
		var es []domain.Execution
		for rows.Next() {
			var d string
			if err = rows.Scan(&d); err != nil {
				rows.Close()
				return err
			}
			e, err := Decode[domain.Execution](d)
			if err != nil {
				rows.Close()
				return err
			}
			es = append(es, e)
		}
		rows.Close()
		for _, e := range es {
			e.State = "stopping"
			e.Desired = "stop"
			if e.Terminal == "" {
				e.Terminal = "failed"
			}
			e.Reason = "controller_restart_cleanup_unconfirmed"
			if err = SaveExecution(tx, e); err != nil {
				return err
			}
		}
		_, err = tx.ExecContext(ctx, "UPDATE workers SET state='unconfirmed' WHERE state!='exited'")
		return err
	})
}
func (s *Store) Finish(ctx context.Context, e domain.Execution) error {
	return s.Tx(ctx, func(tx *sql.Tx) error {
		current, err := Execution(ctx, tx, e.Owner, e.ID)
		if err != nil {
			return err
		}
		if !current.Active() {
			return nil
		}
		if current.Terminal == "" {
			current.Terminal = e.Terminal
		}
		if current.Terminal == "" {
			current.Terminal = "failed"
		}
		current.State = current.Terminal
		current.Desired = "stop"
		current.FinishedAt = domain.Now()
		if current.Reason == "" {
			current.Reason = e.Reason
		}
		for i := range current.Members {
			current.Members[i].State = "stopped"
		}
		if err = SaveExecution(tx, current); err != nil {
			return err
		}
		if _, err = tx.ExecContext(ctx, "DELETE FROM reservations WHERE environment=? AND tenant_id=? AND execution_id=?", e.Environment, e.TenantID, e.ID); err != nil {
			return err
		}
		if _, err = tx.ExecContext(ctx, "DELETE FROM execution_media WHERE environment=? AND tenant_id=? AND execution_id=?", e.Environment, e.TenantID, e.ID); err != nil {
			return err
		}
		if _, err = tx.ExecContext(ctx, "UPDATE workers SET state='exited' WHERE id=?", current.WorkerID); err != nil {
			return err
		}
		state := "succeeded"
		if current.State == "failed" {
			state = "failed"
		}
		return UpdateOperation(tx, e.Owner, current.OperationID, state, current)
	})
}
func Expired(s string) bool {
	t, err := time.Parse(time.RFC3339Nano, s)
	return err != nil || !time.Now().Before(t)
}

func (s *Store) Sign(data string) string {
	h := hmac.New(sha256.New, s.cursorKey)
	h.Write([]byte(data))
	return hex.EncodeToString(h.Sum(nil))
}
