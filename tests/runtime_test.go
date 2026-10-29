package tests

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"kinich-go/internal/admission"
	"kinich-go/internal/domain"
	"kinich-go/internal/schedule"
)

func TestGateFairnessAndOwnership(t *testing.T) {
	g := admission.NewGate(1)
	type grant struct{ tenant, token string }
	ch := make(chan grant, 4)
	request := func(worker, tenant, key string) {
		if !g.Request(worker, tenant, key, func(token string) { ch <- grant{tenant, token} }) {
			t.Fatal("request rejected")
		}
	}
	request("wa", "a", "m1")
	first := <-ch
	request("wa", "a", "m2")
	request("wb", "b", "m1")
	g.Done("other", first.token)
	active, waiting := g.Count()
	if active != 1 || waiting != 2 {
		t.Fatal("foreign release")
	}
	g.Done("wa", first.token)
	second := <-ch
	if second.tenant != "b" {
		t.Fatal("tenant starved")
	}
	g.WorkerExited("wb")
	third := <-ch
	if third.tenant != "a" {
		t.Fatal("queue stalled")
	}
	g.WorkerExited("wa")
	active, waiting = g.Count()
	if active != 0 || waiting != 0 {
		t.Fatal("permits leaked")
	}
}
func TestSchedulesDSTAndDisabledOccurrences(t *testing.T) {
	sc := domain.Schedule{Timezone: "America/New_York", Hour: 1, Minute: 30, Weekdays: []int{0}, Enabled: true, Overlap: "skip"}
	if err := schedule.Validate(sc); err != nil {
		t.Fatal(err)
	}
	first, _ := time.Parse(time.RFC3339, "2026-11-01T05:30:00Z")
	second, _ := time.Parse(time.RFC3339, "2026-11-01T06:30:00Z")
	a, ok := schedule.Occurrence(sc, first)
	b, ok2 := schedule.Occurrence(sc, second)
	if !ok || !ok2 || a != b {
		t.Fatal("fold occurrence must deduplicate")
	}
	sc.Hour = 2
	spring, _ := time.Parse(time.RFC3339, "2026-03-08T07:30:00Z")
	if _, ok = schedule.Occurrence(sc, spring); ok {
		t.Fatal("nonexistent time replayed")
	}
	f := setup(t)
	c := f.camera(0, "SCHEDULE", "schedule-camera", []domain.Member{pulse("a")})
	now := time.Now().UTC()
	sc = domain.Schedule{Owner: c.Owner, ID: domain.ID(), CameraID: c.ID, Revision: 1, Timezone: "UTC", Hour: now.Hour(), Minute: now.Minute(), Weekdays: []int{int(now.Weekday())}, Enabled: true, Overlap: "skip"}
	if _, err := f.store.DB.Exec("INSERT INTO schedules VALUES(?,?,?,?,?)", sc.ID, sc.Environment, sc.TenantID, c.ID, domain.JSON(sc)); err != nil {
		t.Fatal(err)
	}
	scheduler := f.server.Scheduler()
	if err := scheduler.Tick(f.ctx, now); err != nil {
		t.Fatal(err)
	}
	if err := scheduler.Tick(f.ctx, now); err != nil {
		t.Fatal(err)
	}
	var count int
	f.store.DB.QueryRow("SELECT COUNT(*) FROM executions").Scan(&count)
	if count != 1 {
		t.Fatal("occurrence duplicate")
	}
}
func TestLostEventACKAndProcessCrash(t *testing.T) {
	f := setup(t)
	var count atomic.Int32
	f.rec.Event = func(ctx context.Context, e domain.Execution, event domain.Event) error {
		err := f.server.Events.Accept(ctx, e, event)
		if err == nil && count.Add(1) == 1 {
			return errors.New("injected_after_commit_before_ack")
		}
		return err
	}
	c := f.camera(0, "ACK", "ack-camera-001", []domain.Member{pulse("a")})
	e := f.start(0, c, "ack-start-001")
	f.run()
	running := f.waitState(e, "running")
	eventually(t, 4*time.Second, func() bool { return count.Load() >= 2 })
	es, _ := f.server.Events.List(f.ctx, e.Owner, e.ID, 0, 100)
	ids := map[string]bool{}
	for _, ev := range es {
		if ids[ev.ID] {
			t.Fatal("event duplicated")
		}
		ids[ev.ID] = true
	}
	p := f.rec.Manager.Get(e.ID)
	if p == nil {
		t.Fatal("worker missing")
	}
	p.Kill()
	eventually(t, 4*time.Second, func() bool { cur, _ := f.store.Execution(f.ctx, e.Owner, e.ID); return cur.State == "failed" })
	if err := f.server.Events.Accept(f.ctx, running, makeEvent(running, running.Members[0])); err == nil {
		t.Fatal("crashed generation accepted")
	}
	var n int
	f.store.DB.QueryRow("SELECT COUNT(*) FROM reservations").Scan(&n)
	if n != 0 {
		t.Fatal("crash reservation leaked")
	}
}
func TestUnconfirmedStopKeepsReservation(t *testing.T) {
	f := setup(t)
	c := f.camera(0, "STOP", "stop-camera-001", []domain.Member{pulse("a")})
	e := f.start(0, c, "stop-start-001")
	f.run()
	f.waitState(e, "running")
	p := f.rec.Manager.Get(e.ID)
	if err := syscall.Kill(-p.Cmd.Process.Pid, syscall.SIGSTOP); err != nil {
		t.Fatal(err)
	}
	status, b := f.request(0, "POST", "/v2/executions/"+e.ID+"/stop", "stop-execution-001", 0, nil)
	if status != 202 {
		t.Fatalf("%d %s", status, b)
	}
	current, _ := f.store.Execution(f.ctx, e.Owner, e.ID)
	var n int
	f.store.DB.QueryRow("SELECT COUNT(*) FROM reservations").Scan(&n)
	if current.State != "stopping" || current.FinishedAt != "" || n != 1 {
		t.Fatal("optimistic cleanup confirmation")
	}
	f.waitState(e, "cancelled")
}

type streamWriter struct {
	mu      sync.Mutex
	bytes   strings.Builder
	header  map[string][]string
	flushed chan struct{}
}

func (w *streamWriter) Header() http.Header { return w.header }
func (w *streamWriter) WriteHeader(int)     {}
func (w *streamWriter) Write(b []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.bytes.Write(b)
}
func (w *streamWriter) Flush() {
	select {
	case w.flushed <- struct{}{}:
	default:
	}
}
func TestSSERevocationAndScope(t *testing.T) {
	f := setup(t)
	_, e := f.manualRunning(0)
	ev := makeEvent(e, e.Members[0])
	if err := f.server.Events.Accept(f.ctx, e, ev); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(f.ctx)
	defer cancel()
	req := httptest.NewRequest("GET", "/v2/executions/"+e.ID+"/events/live", nil).WithContext(ctx)
	req.Header.Set("Authorization", "Bearer "+f.tokens[0])
	w := &streamWriter{header: make(map[string][]string), flushed: make(chan struct{}, 1)}
	done := make(chan struct{})
	go func() { f.handler.ServeHTTP(w, req); close(done) }()
	select {
	case <-w.flushed:
	case <-time.After(2 * time.Second):
		t.Fatal("SSE did not replay")
	}
	if err := f.auth.Revoke(f.ctx, e.Owner, f.keys[0]); err != nil {
		t.Fatal(err)
	}
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("revocation did not close SSE")
	}
	status, _ := f.request(1, "GET", "/v2/executions/"+e.ID+"/events/live", "", 0, nil)
	if status != 404 {
		t.Fatal(status)
	}
}
func TestLongContinuous(t *testing.T) {
	if os.Getenv("KINICH_LONG") != "1" {
		t.Skip("KINICH_LONG=1 para ensayo de 181 segundos")
	}
	f := setup(t)
	c := f.camera(0, "LONG", "long-camera-001", []domain.Member{pulse("a")})
	e := f.start(0, c, "long-start-001")
	f.run()
	f.waitState(e, "running")
	started := time.Now()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for time.Since(started) < 181*time.Second {
		<-ticker.C
		current, err := f.store.Execution(f.ctx, e.Owner, e.ID)
		if err != nil || current.State != "running" {
			t.Fatalf("continuous ended at %s: %+v %v", time.Since(started), current, err)
		}
	}
	es, err := f.server.Events.List(f.ctx, e.Owner, e.ID, 0, 200)
	if err != nil || len(es) < 100 {
		t.Fatalf("continuous events %d %v", len(es), err)
	}
	t.Logf("181s continuos, %d eventos confirmados", len(es))
}
func TestHundredCycles(t *testing.T) {
	if os.Getenv("KINICH_CYCLES") != "1" {
		t.Skip("KINICH_CYCLES=1 para 100 ciclos")
	}
	f := setup(t)
	c := f.camera(0, "CYCLES", "cycles-camera-001", []domain.Member{pulse("a")})
	f.run()
	for i := 0; i < 100; i++ {
		e := f.start(0, c, fmt.Sprintf("cycle-start-%03d", i))
		f.waitState(e, "running")
		status, b := f.request(0, "POST", "/v2/executions/"+e.ID+"/stop", fmt.Sprintf("cycle-stop-%03d", i), 0, nil)
		if status != 202 {
			t.Fatalf("%d %s", status, b)
		}
		f.waitState(e, "cancelled")
	}
	var n int
	f.store.DB.QueryRow("SELECT COUNT(*) FROM reservations").Scan(&n)
	if n != 0 {
		t.Fatal("cycle reservations leaked")
	}
	t.Log("100 altas/paradas, cero reservas finales")
}
func TestRestartKeepsUnconfirmedResources(t *testing.T) {
	f := setup(t)
	c, e := f.manualRunning(0)
	if err := f.store.Tx(f.ctx, func(tx *sql.Tx) error {
		if _, err := tx.Exec("INSERT INTO workers VALUES(?,?,?,?,?,?,?)", e.WorkerID, e.Environment, e.TenantID, e.ID, "previous-controller", os.Getpid(), "alive"); err != nil {
			return err
		}
		_, err := tx.Exec("INSERT INTO reservations VALUES(?,?,?,?,?)", e.Environment, e.TenantID, e.ID, 16, "held")
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if err := f.store.Recover(f.ctx); err != nil {
		t.Fatal(err)
	}
	current, _ := f.store.Execution(f.ctx, e.Owner, e.ID)
	if current.State != "stopping" || current.Desired != "stop" {
		t.Fatal("restart did not fence")
	}
	camera, _ := f.store.Camera(f.ctx, c.Owner, c.ID)
	if !camera.Enabled {
		t.Fatal("source failure disabled camera")
	}
	var n int
	f.store.DB.QueryRow("SELECT COUNT(*) FROM reservations").Scan(&n)
	if n != 1 {
		t.Fatal("unconfirmed release")
	}
}
