APP_NAME := Librarian
BUILD_DIR := .build/release
APP_BUNDLE := dist/$(APP_NAME).app

.PHONY: run build app clean

run:
	swift run

build:
	swift build -c release

app: build
	rm -rf $(APP_BUNDLE)
	mkdir -p $(APP_BUNDLE)/Contents/MacOS $(APP_BUNDLE)/Contents/Resources
	cp $(BUILD_DIR)/$(APP_NAME) $(APP_BUNDLE)/Contents/MacOS/$(APP_NAME)
	cp Support/Info.plist $(APP_BUNDLE)/Contents/
	cp Support/AppIcon.icns $(APP_BUNDLE)/Contents/Resources/
	codesign --force --sign - $(APP_BUNDLE)
	@echo "Built $(APP_BUNDLE)"

clean:
	rm -rf .build dist
