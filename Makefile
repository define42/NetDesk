.PHONY: build check test-ca test-ntp verify smoke

build:
	./scripts/build.sh

check:
	./scripts/check.sh
	python3 -m py_compile scripts/verify-image.py scripts/smoke-test.py scripts/test-ca.py scripts/test-ntp.py

test-ca:
	python3 scripts/test-ca.py

test-ntp:
	python3 scripts/test-ntp.py

verify:
	python3 scripts/verify-image.py dist/netdesk.efi

smoke:
	python3 scripts/smoke-test.py dist/netdesk.efi
