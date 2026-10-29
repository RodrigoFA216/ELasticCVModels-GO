GO ?= go
PYTHON ?= python3

.PHONY: build check check-cv long cycles run init release
build:
	mkdir -p bin
	$(GO) build -trimpath -o bin/kinichd ./cmd/kinichd
check:
	$(GO) vet ./...
	$(GO) test -race ./... -count=1
	PYTHONPATH=worker $(PYTHON) -m unittest discover -s worker/tests -v
	$(PYTHON) scripts/check_contracts.py
check-cv:
	KINICH_CV_PYTHON=$(CV_PYTHON) $(GO) test ./tests -run TestImageAndClip -count=1 -v
long:
	KINICH_LONG=1 $(GO) test ./tests -run TestLongContinuous -count=1 -v
cycles:
	KINICH_CYCLES=1 $(GO) test ./tests -run TestHundredCycles -count=1 -v
init: build
	./bin/kinichd -command init
run: build
	./bin/kinichd -allow-synthetic
release:
	GO=$(GO) sh scripts/release.sh
