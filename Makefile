SHELL := /bin/bash
PYTHON ?= python3
VERSION ?= $(shell sed -n 's/.*<!ENTITY version "\([^"]*\)".*/\1/p' unraid/libvirt-balloon-keeper.plg)
DIST ?= dist
PACKAGE := libvirt-balloon-keeper-$(VERSION).tar.gz

.PHONY: all check test package package-repro package-manifest clean

all: check package-repro

check:
	$(PYTHON) -m unittest discover -s tests -v
	$(PYTHON) -m coverage run --branch -m unittest discover -s tests
	$(PYTHON) -m coverage report --fail-under=90
	$(PYTHON) -m compileall -q balloon_keeper.py web_server.py libvirt_balloon_keeper tests
	bash -n unraid/*.sh
	@if command -v php >/dev/null 2>&1; then php -l unraid/api.php; else echo 'SKIP: php not installed'; fi
	git diff --check

# Short alias for the complete test gate.
test: check

package:
	VERSION=$(VERSION) bash unraid/build-package.sh $(DIST)

package-repro:
	@set -euo pipefail; \
	tmp_a=$$(mktemp -d); tmp_b=$$(mktemp -d); \
	trap 'rm -rf "$$tmp_a" "$$tmp_b"' EXIT; \
	VERSION=$(VERSION) bash unraid/build-package.sh "$$tmp_a"; \
	VERSION=$(VERSION) bash unraid/build-package.sh "$$tmp_b"; \
	cmp "$$tmp_a/$(PACKAGE)" "$$tmp_b/$(PACKAGE)"; \
	( cd "$$tmp_a" && sha256sum -c "$(PACKAGE).sha256" ); \
	actual=$$(sha256sum "$$tmp_a/$(PACKAGE)" | cut -d' ' -f1); \
	printf 'candidate package checksum: %s\n' "$$actual"; \
	! tar -tzf "$$tmp_a/$(PACKAGE)" | grep -E '(__pycache__/|\.pyc$$)'

# Compare against the pinned release manifest. This is intentionally separate:
# local Python/tar implementations must first be proven byte-identical to CI.
package-manifest:
	@set -euo pipefail; \
	tmp=$$(mktemp -d); \
	trap 'rm -rf "$$tmp"' EXIT; \
	VERSION=$(VERSION) bash unraid/build-package.sh "$$tmp"; \
	actual=$$(sha256sum "$$tmp/$(PACKAGE)" | cut -d' ' -f1); \
	expected=$$(sed -n 's/.*<!ENTITY sha256[[:space:]]*"\([0-9a-f]\{64\}\)".*/\1/p' unraid/libvirt-balloon-keeper.plg); \
	printf 'manifest checksum: %s\nbuilt checksum: %s\n' "$$expected" "$$actual"; \
	test -n "$$expected" && test "$$actual" = "$$expected"

clean:
	$(PYTHON) -c 'import shutil; shutil.rmtree("$(DIST)", ignore_errors=True)'
