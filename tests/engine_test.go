package tests

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"kinich-go/internal/admission"
	"kinich-go/internal/api"
	"kinich-go/internal/auth"
	"kinich-go/internal/catalog"
	"kinich-go/internal/domain"
	"kinich-go/internal/events"
	"kinich-go/internal/reconcile"
	"kinich-go/internal/store"
	"kinich-go/internal/supervisor"
)

type fixture struct {
	t       *testing.T
	store   *store.Store
	server  *api.Server
	rec     *reconcile.Reconciler
	auth    auth.Auth
	owners  []domain.Owner
	tokens  []string
	keys    []string
	handler http.Handler
	ctx     context.Context
	cancel  context.CancelFunc
}

func setup(t *testing.T) *fixture {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	key := make([]byte, 32)
	key[0] = 42
	st, err := store.Open(filepath.Join(t.TempDir(), "kinich.sqlite"), key)
	if err != nil {
		t.Fatal(err)
	}
	cat, err := catalog.Load("")
	if err != nil {
		t.Fatal(err)
	}
	worker, _ := filepath.Abs("../worker")
	mgr := supervisor.New("python3", worker)
	ev := &events.Service{Store: st}
	rec := &reconcile.Reconciler{Store: st, Catalog: cat, Manager: mgr, Budget: admission.Budget{MemoryMiB: 2048, MaxWorkers: 8}, Event: ev.Accept, Gate: admission.NewGate(2)}
	a := auth.Auth{Store: st}
	srv := &api.Server{Store: st, Auth: a, Catalog: cat, Reconciler: rec, Events: ev, Outbox: &events.Outbox{Store: st}, AllowSynthetic: true}
	f := &fixture{t: t, store: st, server: srv, rec: rec, auth: a, handler: srv.Handler(), ctx: ctx, cancel: cancel}
	for _, o := range []domain.Owner{{Environment: "qa", TenantID: "a"}, {Environment: "qa", TenantID: "b"}, {Environment: "qa", TenantID: "c"}, {Environment: "prod", TenantID: "a"}} {
		if err = st.PutTenant(ctx, domain.Tenant{Owner: o, Quota: domain.Quota{Cameras: 64, Executions: 4, MemoryMiB: 2048, Queue: 64}}); err != nil {
			t.Fatal(err)
		}
		id, token, err := a.Issue(ctx, o, []string{"*"}, time.Now().Add(time.Hour))
		if err != nil {
			t.Fatal(err)
		}
		f.owners = append(f.owners, o)
		f.tokens = append(f.tokens, token)
		f.keys = append(f.keys, id)
	}
	t.Cleanup(func() {
		cancel()
		c, stop := context.WithTimeout(context.Background(), 5*time.Second)
		defer stop()
		mgr.Shutdown(c)
		time.Sleep(50 * time.Millisecond)
		st.Close()
	})
	return f
}
func (f *fixture) request(tenant int, method, path, key string, rev int64, body any) (int, []byte) {
	f.t.Helper()
	var data string
	if body != nil {
		data = domain.JSON(body)
	}
	r := httptest.NewRequest(method, path, strings.NewReader(data))
	if tenant >= 0 {
		r.Header.Set("Authorization", "Bearer "+f.tokens[tenant])
	}
	if key != "" {
		r.Header.Set("Idempotency-Key", key)
	}
	if rev > 0 {
		r.Header.Set("If-Match", fmt.Sprint(rev))
	}
	w := httptest.NewRecorder()
	f.handler.ServeHTTP(w, r)
	return w.Code, w.Body.Bytes()
}
func decode[T any](t *testing.T, b []byte) T {
	t.Helper()
	var v T
	if err := json.Unmarshal(b, &v); err != nil {
		t.Fatalf("decode: %v %s", err, b)
	}
	return v
}
func (f *fixture) camera(owner int, code, key string, members []domain.Member) domain.Camera {
	f.t.Helper()
	status, b := f.request(owner, "POST", "/v2/cameras", key, 0, map[string]any{"code": code, "name": "Camera " + code, "source": "synthetic://same", "members": members})
	if status != 200 {
		f.t.Fatalf("create camera %d %s", status, b)
	}
	op := decode[domain.Operation](f.t, b)
	return decode[domain.Camera](f.t, []byte(domain.JSON(op.Result)))
}
func pulse(id string) domain.Member {
	return domain.Member{ID: id, Model: "pulso", Config: map[string]any{"on_s": 1., "off_s": 1.}, FPS: 2}
}
func (f *fixture) start(owner int, c domain.Camera, key string) domain.Execution {
	f.t.Helper()
	status, b := f.request(owner, "POST", "/v2/cameras/"+c.ID+"/executions", key, 0, map[string]int{"queue_seconds": 60})
	if status != 202 {
		f.t.Fatalf("start %d %s", status, b)
	}
	op := decode[domain.Operation](f.t, b)
	return decode[domain.Execution](f.t, []byte(domain.JSON(op.Result)))
}
func eventually(t *testing.T, timeout time.Duration, fn func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if fn() {
			return
		}
		time.Sleep(25 * time.Millisecond)
	}
	t.Fatal("condition timed out")
}
func (f *fixture) run() { go f.rec.Run(f.ctx) }
func (f *fixture) waitState(e domain.Execution, state string) domain.Execution {
	f.t.Helper()
	var current domain.Execution
	eventually(f.t, 8*time.Second, func() bool {
		var err error
		current, err = f.store.Execution(f.ctx, e.Owner, e.ID)
		if current.State == "failed" {
			f.t.Fatalf("execution failed: %+v", current)
		}
		return err == nil && current.State == state
	})
	return current
}
func TestOwnershipIdempotencyAndRevisions(t *testing.T) {
	f := setup(t)
	var cameras []domain.Camera
	for i := range f.owners {
		cameras = append(cameras, f.camera(i, "SAME", "same-key-0001", nil))
	}
	if cameras[0].ID == cameras[1].ID {
		t.Fatal("camera identity reused")
	}
	again := f.camera(0, "SAME", "same-key-0001", nil)
	if again.ID != cameras[0].ID {
		t.Fatal("idempotency lost")
	}
	status, _ := f.request(0, "POST", "/v2/cameras", "same-key-0001", 0, map[string]any{"code": "OTHER", "name": "Changed", "source": "synthetic://same"})
	if status != 409 {
		t.Fatalf("different payload %d", status)
	}
	for i := 1; i < len(cameras); i++ {
		for _, path := range []string{"/v2/cameras/" + cameras[i].ID, "/v2/operations/" + cameras[i].ID, "/v2/executions/" + cameras[i].ID} {
			status, _ := f.request(0, "GET", path, "", 0, nil)
			if status != 404 {
				t.Fatalf("cross ownership %d %s", status, path)
			}
		}
		status, _ := f.request(0, "POST", "/v2/cameras/"+cameras[i].ID+"/disconnect", "foreign-key-001", 1, nil)
		if status != 404 {
			t.Fatalf("cross mutation %d", status)
		}
	}
	status, _ = f.request(0, "POST", "/v2/cameras", "spoof-key-001", 0, map[string]any{"code": "SPOOF", "name": "name", "source": "synthetic://same", "tenant_id": "b"})
	if status != 400 {
		t.Fatalf("body spoof %d", status)
	}
	var wg sync.WaitGroup
	statuses := make(chan int, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			status, _ := f.request(0, "PATCH", "/v2/cameras/"+cameras[0].ID, fmt.Sprintf("concurrent-%02d", i), 1, map[string]string{"name": fmt.Sprint(i)})
			statuses <- status
		}(i)
	}
	wg.Wait()
	close(statuses)
	counts := map[int]int{}
	for code := range statuses {
		counts[code]++
	}
	if counts[200] != 1 || counts[412] != 1 {
		t.Fatalf("revision counts %v", counts)
	}
	status, _ = f.request(0, "PATCH", "/v2/cameras/"+cameras[0].ID, "missing-match", 0, map[string]string{"name": "x"})
	if status != 428 {
		t.Fatalf("revision required %d", status)
	}
}
func TestAuthAndEncryptedSecrets(t *testing.T) {
	f := setup(t)
	status, _ := f.request(-1, "GET", "/v2/models", "", 0, nil)
	if status != 401 {
		t.Fatal(status)
	}
	_, token, err := f.auth.Issue(f.ctx, f.owners[0], []string{"read"}, time.Now().Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	f.tokens[0] = token
	status, _ = f.request(0, "POST", "/v2/cameras", "scope-denied", 0, map[string]string{})
	if status != 403 {
		t.Fatal(status)
	}
	id, token, err := f.auth.Issue(f.ctx, f.owners[0], []string{"*"}, time.Now().Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	f.tokens[0] = token
	source := "rtsp://private-user:private-password@192.168.1.42/live"
	status, b := f.request(0, "POST", "/v2/cameras", "encrypt-source", 0, map[string]any{"code": "SECRET", "name": "private", "source": source})
	if status != 200 {
		t.Fatalf("%d %s", status, b)
	}
	if strings.Contains(string(b), "private-password") {
		t.Fatal("source leaked")
	}
	op := decode[domain.Operation](t, b)
	var ciphertext []byte
	if err = f.store.DB.QueryRow("SELECT source FROM cameras WHERE id=?", op.ResourceID).Scan(&ciphertext); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(ciphertext), "private-password") {
		t.Fatal("source plaintext")
	}
	plain, err := f.store.Unseal(f.owners[0], ciphertext)
	if err != nil || plain != source {
		t.Fatal("decrypt")
	}
	if _, err = f.store.Unseal(f.owners[1], ciphertext); err == nil {
		t.Fatal("cross-scope decrypt")
	}
	if err = f.auth.Revoke(f.ctx, f.owners[0], id); err != nil {
		t.Fatal(err)
	}
	status, _ = f.request(0, "GET", "/v2/models", "", 0, nil)
	if status != 401 {
		t.Fatal(status)
	}
}
func TestContinuousWorkerCompositionAndDisconnect(t *testing.T) {
	f := setup(t)
	c := f.camera(0, "LIVE", "live-camera-001", []domain.Member{pulse("a"), pulse("b")})
	e := f.start(0, c, "live-start-001")
	f.run()
	running := f.waitState(e, "running")
	if running.ObservedRevision != 1 {
		t.Fatal("unobserved ready")
	}
	eventually(t, 4*time.Second, func() bool { es, _ := f.server.Events.List(f.ctx, e.Owner, e.ID, 0, 100); return len(es) >= 2 })
	originalA := running.Members[0].Generation
	status, b := f.request(0, "PUT", "/v2/cameras/"+c.ID+"/composition", "add-member-001", 1, map[string]any{"members": []domain.Member{pulse("a"), pulse("b"), pulse("c")}})
	if status != 202 {
		t.Fatalf("configure %d %s", status, b)
	}
	eventually(t, 4*time.Second, func() bool {
		current, _ := f.store.Execution(f.ctx, e.Owner, e.ID)
		return current.ObservedRevision == 2
	})
	current, _ := f.store.Execution(f.ctx, e.Owner, e.ID)
	if current.Members[0].Generation != originalA {
		t.Fatal("neighbor session reset")
	}
	oldB := current.Members[1]
	status, b = f.request(0, "DELETE", "/v2/cameras/"+c.ID+"/members/b", "remove-member-001", 2, nil)
	if status != 202 {
		t.Fatalf("remove %d %s", status, b)
	}
	eventually(t, 4*time.Second, func() bool {
		current, _ := f.store.Execution(f.ctx, e.Owner, e.ID)
		return current.ObservedRevision == 3
	})
	current, _ = f.store.Execution(f.ctx, e.Owner, e.ID)
	if len(current.Members) != 2 || current.Members[0].Generation != originalA {
		t.Fatal("composition mismatch")
	}
	late := makeEvent(current, oldB)
	if err := f.server.Events.Accept(f.ctx, current, late); err == nil {
		t.Fatal("retired member accepted")
	}
	status, b = f.request(0, "POST", "/v2/cameras/"+c.ID+"/disconnect", "disconnect-001", 3, nil)
	if status != 202 {
		t.Fatalf("disconnect %d %s", status, b)
	}
	op := decode[domain.Operation](t, b)
	f.waitState(e, "cancelled")
	eventually(t, 2*time.Second, func() bool { op, _ := f.store.Operation(f.ctx, e.Owner, op.ID); return op.State == "succeeded" })
	var count int
	f.store.DB.QueryRow("SELECT COUNT(*) FROM reservations").Scan(&count)
	if count != 0 {
		t.Fatal("reservation leaked")
	}
	camera, _ := f.store.Camera(f.ctx, e.Owner, c.ID)
	if camera.Enabled {
		t.Fatal("disconnect not persisted")
	}
	status, _ = f.request(0, "POST", "/v2/cameras/"+c.ID+"/executions", "disabled-start-001", 0, map[string]int{})
	if status != 409 {
		t.Fatal(status)
	}
	status, _ = f.request(0, "POST", "/v2/cameras/"+c.ID+"/reactivate", "reactivate-001", 4, nil)
	if status != 200 {
		t.Fatal(status)
	}
	current, _ = f.store.Execution(f.ctx, e.Owner, e.ID)
	if current.State != "cancelled" {
		t.Fatal("reactivate resurrected execution")
	}
}
func makeEvent(e domain.Execution, m domain.Member) domain.Event {
	return domain.Event{ID: domain.ID(), MemberID: m.ID, MemberGeneration: m.Generation, Model: m.Model, Type: "CONTEO", Frame: domain.Frame{ID: domain.ID(), CapturedAt: domain.Now(), Width: 640, Height: 360, SourceWidth: 640, SourceHeight: 360, Transform: []float64{1, 0, 0, 0, 1, 0}}, Detections: []domain.Detection{{Label: "pulse", Score: 1, BBox: []float64{1, 2, 100, 200}}}}
}
func (f *fixture) manualRunning(owner int) (domain.Camera, domain.Execution) {
	c := f.camera(owner, "MANUAL", "manual-camera-001", []domain.Member{pulse("a")})
	e := f.start(owner, c, "manual-start-001")
	e.State = "running"
	e.WorkerID = domain.ID()
	if err := f.store.Tx(f.ctx, func(tx *sql.Tx) error { return store.SaveExecution(tx, e) }); err != nil {
		f.t.Fatal(err)
	}
	return c, e
}
func TestEventsOutboxAtomicityReplayAndFencing(t *testing.T) {
	f := setup(t)
	_, e := f.manualRunning(0)
	if err := f.server.Outbox.SetDestination(f.ctx, domain.Destination{Owner: e.Owner, URL: "https://events.example.com/receive", Token: "private-callback-token"}); err != nil {
		t.Fatal(err)
	}
	ev := makeEvent(e, e.Members[0])
	ev.Owner = f.owners[1]
	ev.CameraID = "foreign"
	if err := f.server.Events.Accept(f.ctx, e, ev); err != nil {
		t.Fatal(err)
	}
	if err := f.server.Events.Accept(f.ctx, e, ev); err != nil {
		t.Fatal(err)
	}
	es, err := f.server.Events.List(f.ctx, e.Owner, e.ID, 0, 100)
	if err != nil || len(es) != 1 || es[0].Owner != e.Owner || es[0].CameraID != e.CameraID {
		t.Fatal("event ownership/dedupe", err)
	}
	ds, err := f.server.Outbox.List(f.ctx, e.Owner)
	if err != nil || len(ds) != 1 {
		t.Fatal("outbox missing", err)
	}
	cursor := events.Cursor(f.store, e.Owner, e.ID, es[0].Sequence)
	if _, err = events.ParseCursor(f.store, f.owners[1], e.ID, cursor); err == nil {
		t.Fatal("cross cursor")
	}
	if _, err = events.ParseCursor(f.store, e.Owner, "another", cursor); err == nil {
		t.Fatal("cross execution cursor")
	}
	if _, err = events.ParseCursor(f.store, e.Owner, e.ID, cursor); err != nil {
		t.Fatal(err)
	}
	if err = f.store.Tx(f.ctx, func(tx *sql.Tx) error { return store.Retire(tx, &e, "cancelled", "test") }); err != nil {
		t.Fatal(err)
	}
	if err = f.server.Events.Accept(f.ctx, e, ev); err != nil {
		t.Fatal("confirmed retry lost ACK", err)
	}
	ev.ID = domain.ID()
	if err = f.server.Events.Accept(f.ctx, e, ev); err == nil {
		t.Fatal("late event accepted")
	}
	status, _ := f.request(1, "GET", "/v2/events?execution_id="+e.ID, "", 0, nil)
	if status != 404 {
		t.Fatal(status)
	}
	status, _ = f.request(1, "POST", "/v2/deliveries/"+ds[0].ID+"/retry", "foreign-delivery", 0, nil)
	if status != 404 {
		t.Fatal(status)
	}
}

type transport func(*http.Request) (*http.Response, error)

func (fn transport) RoundTrip(r *http.Request) (*http.Response, error) { return fn(r) }
func TestOutboxRetryKeepsPayloadAndDestination(t *testing.T) {
	f := setup(t)
	_, e := f.manualRunning(0)
	o := f.server.Outbox
	if err := o.SetDestination(f.ctx, domain.Destination{Owner: e.Owner, URL: "https://old.example.com/receive", Token: "first-private-token"}); err != nil {
		t.Fatal(err)
	}
	ev := makeEvent(e, e.Members[0])
	if err := f.server.Events.Accept(f.ctx, e, ev); err != nil {
		t.Fatal(err)
	}
	if err := o.SetDestination(f.ctx, domain.Destination{Owner: e.Owner, URL: "https://new.example.com/receive", Token: "second-private-token"}); err != nil {
		t.Fatal(err)
	}
	attempt := 0
	payload := ""
	o.Client = &http.Client{Transport: transport(func(r *http.Request) (*http.Response, error) {
		attempt++
		b, _ := io.ReadAll(r.Body)
		if attempt == 1 {
			payload = string(b)
		}
		if payload != string(b) || r.URL.Host != "old.example.com" || r.Header.Get("Idempotency-Key") != ev.ID || r.Header.Get("Authorization") != "Bearer first-private-token" {
			t.Error("delivery changed identity, body or destination")
		}
		status := 500
		if attempt == 2 {
			status = 429
		}
		if attempt == 3 {
			status = 204
		}
		return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader("")), Header: make(http.Header)}, nil
	})}
	for i := 0; i < 3; i++ {
		if _, err := f.store.DB.Exec("UPDATE deliveries SET next_at=?", domain.Now()); err != nil {
			t.Fatal(err)
		}
		if err := o.Tick(f.ctx); err != nil {
			t.Fatal(err)
		}
	}
	ds, _ := o.List(f.ctx, e.Owner)
	if len(ds) != 1 || ds[0].State != "delivered" || ds[0].Attempts != 3 {
		t.Fatal(ds)
	}
}
func TestRollbackBackupAndRestart(t *testing.T) {
	f := setup(t)
	c := f.camera(0, "PERSIST", "persist-camera-001", []domain.Member{pulse("a")})
	e := f.start(0, c, "persist-start-001")
	status, _ := f.request(0, "POST", "/v2/cameras/"+c.ID+"/disconnect", "persist-disconnect", 1, nil)
	if status != 200 {
		t.Fatal(status)
	}
	e, _ = f.store.Execution(f.ctx, e.Owner, e.ID)
	if e.State != "cancelled" {
		t.Fatal(e.State)
	}
	count := 0
	f.store.DB.QueryRow("SELECT COUNT(*) FROM cameras").Scan(&count)
	err := f.store.Tx(f.ctx, func(tx *sql.Tx) error {
		_, err := tx.Exec("UPDATE cameras SET enabled=1")
		if err != nil {
			return err
		}
		return fmt.Errorf("inject rollback")
	})
	if err == nil {
		t.Fatal("expected rollback")
	}
	camera, _ := f.store.Camera(f.ctx, c.Owner, c.ID)
	if camera.Enabled {
		t.Fatal("rollback failed")
	}
	path := filepath.Join(t.TempDir(), "backup.sqlite")
	if err = f.store.Backup(f.ctx, path); err != nil {
		t.Fatal(err)
	}
	key := make([]byte, 32)
	key[0] = 42
	restored, err := store.Open(path, key)
	if err != nil {
		t.Fatal(err)
	}
	defer restored.Close()
	if err = restored.Recover(f.ctx); err != nil {
		t.Fatal(err)
	}
	camera, err = restored.Camera(f.ctx, c.Owner, c.ID)
	if err != nil || camera.Enabled {
		t.Fatal("restored disabled camera lost", err)
	}
	op, err := restored.Operation(f.ctx, e.Owner, e.OperationID)
	if err != nil || op.ResourceID != e.ID {
		t.Fatal("operation lost", err)
	}
	plain, err := restored.Source(f.ctx, c.Owner, c.ID)
	if err != nil || plain != "synthetic://same" {
		t.Fatal("restored decrypt", err)
	}
	_, err = store.Open(path, key)
	if err == nil {
		t.Fatal("second controller acquired same DB")
	}
	if _, err = os.Stat(path); err != nil {
		t.Fatal(err)
	}
}
func TestQuotaUnknownProfilesAndForeignKeys(t *testing.T) {
	f := setup(t)
	status, b := f.request(0, "POST", "/v2/cameras", "unknown-profile", 0, map[string]any{"code": "GAS", "name": "Gas", "source": "synthetic://same", "members": []domain.Member{{ID: "gas", Model: "whitegas", Config: map[string]any{}}}})
	if status != 409 {
		t.Fatalf("unknown profile %d %s", status, b)
	}
	status, _ = f.request(0, "POST", "/v2/cameras", "unknown-config", 0, map[string]any{"code": "INVALID", "name": "Invalid", "source": "synthetic://same", "members": []domain.Member{{ID: "p", Model: "pulso", Config: map[string]any{"typo": true}}}})
	if status != 422 {
		t.Fatal(status)
	}
	c := f.camera(0, "FK", "foreignkey-camera", []domain.Member{pulse("a")})
	_, err := f.store.DB.Exec("INSERT INTO executions VALUES(?,?,?,?,?,?,?,?)", domain.ID(), f.owners[1].Environment, f.owners[1].TenantID, c.ID, "queued", "run", domain.ID(), "{}")
	if err == nil {
		t.Fatal("cross-scope FK accepted")
	}
	e := f.start(0, c, "quota-start-001")
	budget := admission.Budget{MemoryMiB: 16, MaxWorkers: 1}
	if err = f.store.Tx(f.ctx, func(tx *sql.Tx) error { return budget.Reserve(f.ctx, tx, e, 16) }); err != nil {
		t.Fatal(err)
	}
	c2 := f.camera(1, "FK", "other-camera-001", []domain.Member{pulse("a")})
	e2 := f.start(1, c2, "other-start-001")
	if err = f.store.Tx(f.ctx, func(tx *sql.Tx) error { return budget.Reserve(f.ctx, tx, e2, 16) }); err == nil {
		t.Fatal("capacity overallocated")
	}
	var reserved int
	f.store.DB.QueryRow("SELECT SUM(memory_mib) FROM reservations").Scan(&reserved)
	if reserved != 16 {
		t.Fatal(reserved)
	}
}

func TestCredentialCreationIsIdempotentWithoutPersistingSecret(t *testing.T) {
	f := setup(t)
	in := map[string]any{"scopes": []string{"read"}, "expires_at": time.Now().Add(time.Hour).UTC().Format(time.RFC3339)}
	status, b := f.request(0, "POST", "/v2/credentials", "credential-create", 0, in)
	if status != 201 {
		t.Fatalf("%d %s", status, b)
	}
	first := decode[map[string]json.RawMessage](t, b)
	var secret string
	json.Unmarshal(first["key"], &secret)
	if !strings.HasPrefix(secret, "kg_") {
		t.Fatal("secret not returned at creation")
	}
	status, b = f.request(0, "POST", "/v2/credentials", "credential-create", 0, in)
	if status != 201 {
		t.Fatal(status)
	}
	second := decode[map[string]json.RawMessage](t, b)
	var replay string
	json.Unmarshal(second["key"], &replay)
	if replay != "" {
		t.Fatal("secret persisted for replay")
	}
	var n int
	f.store.DB.QueryRow("SELECT COUNT(*) FROM credentials").Scan(&n)
	if n != 5 {
		t.Fatal("duplicate credential")
	}
	var count int
	f.store.DB.QueryRow("SELECT COUNT(*) FROM operations WHERE data LIKE ?", "%"+secret+"%").Scan(&count)
	if count != 0 {
		t.Fatal("secret persisted in operation")
	}
}
func TestRetentionExpiresCursorAndPreservesPendingDelivery(t *testing.T) {
	f := setup(t)
	_, e := f.manualRunning(0)
	ev := makeEvent(e, e.Members[0])
	if err := f.server.Events.Accept(f.ctx, e, ev); err != nil {
		t.Fatal(err)
	}
	es, _ := f.server.Events.List(f.ctx, e.Owner, e.ID, 0, 100)
	cursor := events.Cursor(f.store, e.Owner, e.ID, 0)
	if err := f.server.Events.Prune(f.ctx, time.Now().Add(time.Hour)); err != nil {
		t.Fatal(err)
	}
	if err := f.server.Events.CheckCursor(f.ctx, e.Owner, 0); err == nil {
		t.Fatal("expired cursor accepted")
	}
	status, _ := f.request(0, "GET", "/v2/events?execution_id="+e.ID+"&cursor="+cursor, "", 0, nil)
	if status != 410 {
		t.Fatal(status)
	}
	if len(es) != 1 {
		t.Fatal("fixture")
	}
	if err := f.server.Outbox.SetDestination(f.ctx, domain.Destination{Owner: e.Owner, URL: "https://events.example.com/", Token: "long-private-token"}); err != nil {
		t.Fatal(err)
	}
	ev.ID = domain.ID()
	if err := f.server.Events.Accept(f.ctx, e, ev); err != nil {
		t.Fatal(err)
	}
	if err := f.server.Events.Prune(f.ctx, time.Now().Add(time.Hour)); err != nil {
		t.Fatal(err)
	}
	es, _ = f.server.Events.List(f.ctx, e.Owner, e.ID, 0, 100)
	if len(es) != 1 || es[0].ID != ev.ID {
		t.Fatal("pending delivery pruned")
	}
}
