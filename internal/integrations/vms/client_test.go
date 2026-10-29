package vms

import (
	"kinich-go/internal/domain"
	"testing"
)

func TestStableOwnerAndSemanticClosure(t *testing.T) {
	owner := domain.Owner{Environment: "qa", TenantID: "a"}
	ev := domain.Event{Owner: owner, CameraID: "camera-uuid", ExecutionID: "run", ID: "event", Type: "EPP_FALTA_OFF", State: "verificando"}
	resolve := func(o domain.Owner, id string) (string, error) {
		if o != owner || id != "camera-uuid" {
			return "", domain.NotFound
		}
		return "vms-camera-uuid", nil
	}
	alert, err := ToAlert(owner, ev, resolve)
	if err != nil || alert.CameraUID != "vms-camera-uuid" || !alert.ClosesPhenomenon || alert.SemanticState != "verificando" || alert.EvidenceState != "pending" {
		t.Fatal(alert, err)
	}
	if _, err = ToAlert(domain.Owner{Environment: "qa", TenantID: "b"}, ev, resolve); err == nil {
		t.Fatal("cross-owner alert")
	}
}
