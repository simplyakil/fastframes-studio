import AppKit
let size = 1024.0
let img = NSImage(size: NSSize(width: size, height: size))
img.lockFocus()
let r = NSRect(x: 100, y: 100, width: 824, height: 824)
let path = NSBezierPath(roundedRect: r, xRadius: 185, yRadius: 185)
NSGradient(colors: [NSColor(red:0.31,green:0.61,blue:1,alpha:1), NSColor(red:0.61,green:0.36,blue:1,alpha:1), NSColor(red:1,green:0.42,blue:0.53,alpha:1)])!.draw(in: path, angle: -45)
NSColor.white.setFill()
let body = NSBezierPath(roundedRect: NSRect(x: 250, y: 345, width: 380, height: 334), xRadius: 54, yRadius: 54); body.fill()
let tri = NSBezierPath(); tri.move(to: NSPoint(x: 650, y: 470)); tri.line(to: NSPoint(x: 790, y: 380)); tri.line(to: NSPoint(x: 790, y: 644)); tri.line(to: NSPoint(x: 650, y: 554)); tri.close(); tri.fill()
NSColor(red:0.55,green:0.4,blue:1,alpha:1).setFill()
for i in 0..<3 { NSBezierPath(ovalIn: NSRect(x: 300 + Double(i)*100, y: 485, width: 56, height: 56)).fill() }
img.unlockFocus()
let rep = NSBitmapImageRep(data: img.tiffRepresentation!)!
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
