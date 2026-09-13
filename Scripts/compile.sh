#!/bin/sh
set -eu
mkdir -p Build/ModuleCache
SDK_PATH=$(xcrun --show-sdk-path)
xcrun swiftc -parse-as-library -swift-version 5 -O \
  -target arm64-apple-macos14.0 -sdk "$SDK_PATH" \
  -module-cache-path Build/ModuleCache \
  -framework Cocoa -framework SwiftUI -framework AVKit -framework AVFoundation \
  App/TrackerPlayer.swift App/Navigation.swift App/DeliveryViews.swift \
  -o Build/TrackerPlayer
