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

.PHONY: run build app bundled-app dmg clean

run:
	swift run

build:
	swift build -c release $(ARCH_FLAGS)

app: build
	rm -rf $(APP_BUNDLE)
	mkdir -p $(APP_BUNDLE)/Contents/MacOS $(APP_BUNDLE)/Contents/Resources
	cp "$(BIN_PATH)/$(APP_NAME)" $(APP_BUNDLE)/Contents/MacOS/$(APP_NAME)
	cp Support/Info.plist $(APP_BUNDLE)/Contents/
	cp Support/AppIcon.icns $(APP_BUNDLE)/Contents/Resources/
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
