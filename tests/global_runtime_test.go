package tests

import (
	"database/sql"
	"testing"

	"kinich-go/internal/domain"
	"kinich-go/internal/store"
)

func TestGlobalRuntimeChangesRequireNewProcess(t *testing.T) {
	f := setup(t)
	for _, name := range []string{"whitegas", "ppe-detector"} {
		m := f.server.Catalog.Models[name]
		m.Available = true
		m.Profile = &domain.Profile{ResidentMiB: 32, SessionMiB: 16, WarmupMiB: 16, InFlight: 1, Measured: true, Hardware: "fixture-validation-only"}
		f.server.Catalog.Models[name] = m
	}
	gas := domain.Member{ID: "gas", Model: "whitegas", Config: map[string]any{"prompt": "white gas"}}
	if _, err := f.server.Catalog.Validate([]domain.Member{gas, {ID: "ppe", Model: "ppe-detector"}}, "stream"); err == nil {
		t.Fatal("unverified global cores mixed")
	}
	c := f.camera(0, "GLOBAL", "global-camera", []domain.Member{gas})
	e := f.start(0, c, "global-start")
	e.State = "running"
	e.ObservedRevision = e.Revision
	e.WorkerID = domain.ID()
	if err := f.store.Tx(f.ctx, func(tx *sql.Tx) error { return store.SaveExecution(tx, e) }); err != nil {
		t.Fatal(err)
	}
	gas.Config["prompt"] = "different plume"
	status, b := f.request(0, "PUT", "/v2/cameras/"+c.ID+"/composition", "global-change", 1, map[string]any{"members": []domain.Member{gas}})
	if status != 409 {
		t.Fatalf("%d %s", status, b)
	}
	current, err := f.store.Camera(f.ctx, c.Owner, c.ID)
	if err != nil || current.Revision != 1 || current.Members[0].Config["prompt"] != "white gas" {
		t.Fatal("rejected replacement mutated state", err)
	}
}
func TestWrongMasterKeyIsRejected(t *testing.T) {
	f := setup(t)
	path := f.store.Path + ".backup"
	if err := f.store.Backup(f.ctx, path); err != nil {
		t.Fatal(err)
	}
	wrong := make([]byte, 32)
	wrong[0] = 43
	if s, err := store.Open(path, wrong); err == nil {
		s.Close()
		t.Fatal("wrong key opened operational store")
	}
}
