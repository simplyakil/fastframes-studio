#!/bin/zsh
# Build "FastFrames Studio.app" in ~/Applications: a native window with a Dock icon that runs this folder's server.
set -e
S="${0:A:h:h}"
A="$HOME/Applications/FastFrames Studio.app"
mkdir -p "$A/Contents/MacOS" "$A/Contents/Resources"
rm -f "$A/Contents/MacOS/launch"
if ! command -v swiftc >/dev/null; then echo "swiftc not found: install Xcode or the Command Line Tools (xcode-select --install)"; exit 1; fi
swiftc -O -o "$A/Contents/MacOS/FastFramesStudio" "$S/tools/FastFramesStudio.swift" -framework AppKit -framework WebKit
cat > "$A/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>FastFrames Studio</string>
  <key>CFBundleDisplayName</key><string>FastFrames Studio</string>
  <key>CFBundleIdentifier</key><string>app.fastframes-studio</string>
  <key>CFBundleExecutable</key><string>FastFramesStudio</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.2</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>FFSRepoPath</key><string>$S</string>
</dict></plist>
PLIST
if command -v swiftc >/dev/null; then
  W=$(mktemp -d); mkdir "$W/AppIcon.iconset"
  swiftc -o "$W/icon" "$S/tools/icon.swift" && "$W/icon" "$W/icon.png"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$W/icon.png" --out "$W/AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
    sips -z $((s*2)) $((s*2)) "$W/icon.png" --out "$W/AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$W/AppIcon.iconset" -o "$A/Contents/Resources/AppIcon.icns"; rm -rf "$W"
fi
touch "$A"; echo "Built $A"
