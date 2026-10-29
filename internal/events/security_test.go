package events

import (
	"net/netip"
	"testing"
)

func TestCallbackNetworkPolicy(t *testing.T) {
	p := NetworkPolicy{}
	for _, raw := range []string{"127.0.0.1", "::1", "169.254.169.254", "100.100.100.200", "10.0.0.1", "192.168.0.1", "0.0.0.0", "224.0.0.1", "::ffff:127.0.0.1", "fe80::1", "2001:db8::1"} {
		if p.allowed(netip.MustParseAddr(raw)) {
			t.Errorf("allowed forbidden %s", raw)
		}
	}
	p.ApprovedCIDRs = []netip.Prefix{netip.MustParsePrefix("10.10.0.0/24")}
	if !p.allowed(netip.MustParseAddr("10.10.0.42")) || p.allowed(netip.MustParseAddr("10.11.0.42")) {
		t.Fatal("LAN scope")
	}
	if !p.allowed(netip.MustParseAddr("8.8.8.8")) {
		t.Fatal("public address blocked")
	}
	for _, u := range []string{"file:///etc/passwd", "http://example.com/", "https://user:secret@example.com/", "https://example.com/#fragment"} {
		if p.Validate(u) == nil {
			t.Errorf("invalid URL accepted: %s", u)
		}
	}
	if err := p.Validate("https://events.example.com/v2"); err != nil {
		t.Fatal(err)
	}
}

func TestCallbackTokenCannotBeControlKey(t *testing.T) {
	if ValidateCallbackToken("kg_abcdefghijklmnopqrstuvwxyz0123456789012345678") == nil {
		t.Fatal("control audience reused for callback")
	}
	if err := ValidateCallbackToken("separate-callback-secret"); err != nil {
		t.Fatal(err)
	}
}
