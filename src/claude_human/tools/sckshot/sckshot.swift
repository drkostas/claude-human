// sckshot takes one screenshot through ScreenCaptureKit.
//
// Usage: sckshot --out <path> [--window <CGWindowID>] [--max-width <N>]
//
// The output format follows the --out extension (.jpg or .jpeg gives JPEG at quality 0.7, anything
// else gives PNG). --max-width scales the image down to that width, keeping the aspect ratio.
// On success it prints "OK <width>x<height>" and exits 0. On failure it prints the reason to stderr
// and exits 1. It gives up after 15 seconds.
//
// ScreenCaptureKit is the capture path on recent macOS that is not throttled (the older
// CGWindowListCreateImage call is rate limited there), and its async completion does not fire
// through PyObjC, which is why this is a small compiled tool. It scales and encodes the image
// itself, because a process started by launchd can have a broken CoreGraphics and ImageIO pipeline
// (blank frames, invalid JPEG files) while a GUI-session app like this one encodes correctly. The
// caller only has to run it and read the file.
import ScreenCaptureKit
import AppKit
import Foundation

func die(_ m: String) -> Never {
    FileHandle.standardError.write((m + "\n").data(using: .utf8)!)
    exit(1)
}

func scaled(_ img: CGImage, toWidth maxW: Int) -> CGImage {
    guard maxW > 0, img.width > maxW else { return img }
    let nw = maxW
    let nh = max(1, Int(Double(img.height) * Double(maxW) / Double(img.width)))
    let cs = CGColorSpaceCreateDeviceRGB()
    guard let ctx = CGContext(data: nil, width: nw, height: nh, bitsPerComponent: 8,
                              bytesPerRow: 0, space: cs,
                              bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue) else { return img }
    ctx.interpolationQuality = .medium
    ctx.draw(img, in: CGRect(x: 0, y: 0, width: nw, height: nh))
    return ctx.makeImage() ?? img
}

@main
struct SckShot {
    static func main() {
        var windowID: CGWindowID? = nil
        var out = "sckshot.png"
        var maxWidth = 0
        var i = 1
        let argv = CommandLine.arguments
        while i < argv.count {
            switch argv[i] {
            case "--window":    i += 1; if i < argv.count { windowID = CGWindowID(UInt32(argv[i]) ?? 0) }
            case "--out":       i += 1; if i < argv.count { out = argv[i] }
            case "--max-width": i += 1; if i < argv.count { maxWidth = Int(argv[i]) ?? 0 }
            case "--help", "-h":
                print("usage: sckshot --out <path> [--window <CGWindowID>] [--max-width <N>]")
                exit(0)
            default: break
            }
            i += 1
        }
        if !CGPreflightScreenCaptureAccess() { _ = CGRequestScreenCaptureAccess() }

        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        DispatchQueue.main.asyncAfter(deadline: .now() + 15) { die("timeout") }

        Task {
            do {
                let content = try await SCShareableContent.current
                let cfg = SCStreamConfiguration()
                let filter: SCContentFilter
                if let wid = windowID {
                    guard let win = content.windows.first(where: { $0.windowID == wid }) else {
                        die("window \(wid) not found")
                    }
                    cfg.width  = max(1, Int(win.frame.width))
                    cfg.height = max(1, Int(win.frame.height))
                    // desktopIndependentWindow captures only that window, wherever it is
                    filter = SCContentFilter(desktopIndependentWindow: win)
                } else {
                    guard let disp = content.displays.first else { die("no display") }
                    cfg.width = disp.width; cfg.height = disp.height
                    filter = SCContentFilter(display: disp, excludingWindows: [])
                }
                let raw = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: cfg)
                let image = scaled(raw, toWidth: maxWidth)
                let rep = NSBitmapImageRep(cgImage: image)
                let lower = out.lowercased()
                let isJpeg = lower.hasSuffix(".jpg") || lower.hasSuffix(".jpeg")
                let data = isJpeg
                    ? rep.representation(using: .jpeg, properties: [.compressionFactor: 0.7])
                    : rep.representation(using: .png, properties: [:])
                guard let bytes = data else { die("encode failed") }
                try bytes.write(to: URL(fileURLWithPath: out))
                print("OK \(image.width)x\(image.height)")
                exit(0)
            } catch {
                die("capture: \(error)")
            }
        }
        app.run()
    }
}
