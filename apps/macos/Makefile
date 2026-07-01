APP_NAME := Librarian
APP_BUNDLE := dist/$(APP_NAME).app

# Extra swift build flags, e.g. ARCH_FLAGS="--arch arm64 --arch x86_64"
ARCH_FLAGS ?=
# Wheel path or PyPI requirement for the bundled backend.
WHEEL ?= $(wildcard ../../dist/nampara_librarian-*.whl)
# Target architecture for the bundled Python runtime.
BUNDLE_ARCH ?= $(shell uname -m)
# Code signing identity ("-" = ad-hoc).
IDENTITY ?= -

BIN_PATH = $(shell swift build -c release $(ARCH_FLAGS) --show-bin-path)

# Single source of truth for the app version: the Python package version.
# Parse `__version__` from src/librarian/version.py so the built app can never
# drift from the package/tag (see the `app` target's plist stamping below).
VERSION_FILE := ../../src/librarian/version.py
APP_VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' $(VERSION_FILE))

.PHONY: run build app bundled-app dmg clean

run:
	swift run

build:
	swift build -c release $(ARCH_FLAGS)

app: build
	@test -n "$(APP_VERSION)" || (echo "Could not parse __version__ from $(VERSION_FILE)" && exit 1)
	rm -rf $(APP_BUNDLE)
	mkdir -p $(APP_BUNDLE)/Contents/MacOS $(APP_BUNDLE)/Contents/Resources
	cp "$(BIN_PATH)/$(APP_NAME)" $(APP_BUNDLE)/Contents/MacOS/$(APP_NAME)
	cp Support/Info.plist $(APP_BUNDLE)/Contents/
	cp Support/AppIcon.icns $(APP_BUNDLE)/Contents/Resources/
	# Stamp the built app's Info.plist from the Python package version so the
	# marketing version can never drift from the package/tag. CFBundleVersion is
	# a monotonic integer derived from the semver (major*10000+minor*100+patch),
	# e.g. 1.7.1 -> 10701; it must strictly increase across releases.
	@version="$(APP_VERSION)"; \
	  build_number="$$(printf '%s' "$$version" | awk -F. '{printf "%d", ($$1*10000)+($$2*100)+$$3}')"; \
	  /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $$version" $(APP_BUNDLE)/Contents/Info.plist; \
	  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $$build_number" $(APP_BUNDLE)/Contents/Info.plist; \
	  echo "Stamped app version $$version (build $$build_number)"
	codesign --force --sign - $(APP_BUNDLE)
	@echo "Built $(APP_BUNDLE)"

# Self-contained app: bundles a relocatable Python + the Librarian wheel.
bundled-app: app
	@test -n "$(WHEEL)" || (echo "Set WHEEL=path/to/nampara_librarian-*.whl (build one with 'python -m build --wheel' in the repo root)" && exit 1)
	scripts/bundle_backend.sh --app $(APP_BUNDLE) --wheel "$(WHEEL)" --arch $(BUNDLE_ARCH)
	scripts/sign_app.sh --app $(APP_BUNDLE) --identity "$(IDENTITY)"

dmg:
	scripts/make_dmg.sh --app $(APP_BUNDLE) --output dist/$(APP_NAME).dmg

clean:
	rm -rf .build dist
