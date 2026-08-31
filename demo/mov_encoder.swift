// Encode a directory of PNG frames into an H.264 .mov, using only what ships
// with macOS. Built on demand by record.py; no ffmpeg required.
//
//   swiftc -O mov_encoder.swift -o mov_encoder
//   ./mov_encoder out.mov 12 /path/to/frames

import AVFoundation
import CoreGraphics
import Foundation
import ImageIO

let args = CommandLine.arguments
guard args.count >= 4, let fps = Int32(args[2]) else {
    FileHandle.standardError.write("usage: mov_encoder <out.mov> <fps> <framesDir>\n".data(using: .utf8)!)
    exit(2)
}
let outURL = URL(fileURLWithPath: args[1])
let framesDir = args[3]
let fm = FileManager.default

let files = ((try? fm.contentsOfDirectory(atPath: framesDir)) ?? [])
    .filter { $0.hasSuffix(".png") }.sorted()
guard !files.isEmpty else {
    FileHandle.standardError.write("no frames in \(framesDir)\n".data(using: .utf8)!)
    exit(1)
}

func loadCG(_ path: String) -> CGImage? {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil)
    else { return nil }
    return CGImageSourceCreateImageAtIndex(src, 0, nil)
}

guard let first = loadCG(framesDir + "/" + files[0]) else { exit(1) }
let w = first.width, h = first.height

try? fm.removeItem(at: outURL)
guard let writer = try? AVAssetWriter(outputURL: outURL, fileType: .mov) else { exit(1) }

let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
    AVVideoCodecKey: AVVideoCodecType.h264,
    AVVideoWidthKey: w,
    AVVideoHeightKey: h,
    AVVideoCompressionPropertiesKey: [AVVideoQualityKey: 0.9],
])
input.expectsMediaDataInRealTime = false
let adaptor = AVAssetWriterInputPixelBufferAdaptor(
    assetWriterInput: input,
    sourcePixelBufferAttributes: [
        kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32ARGB),
        kCVPixelBufferWidthKey as String: w,
        kCVPixelBufferHeightKey as String: h,
    ])
writer.add(input)
guard writer.startWriting() else { exit(1) }
writer.startSession(atSourceTime: .zero)

let space = CGColorSpaceCreateDeviceRGB()
var frame: Int64 = 0
for name in files {
    guard let img = loadCG(framesDir + "/" + name),
          let pool = adaptor.pixelBufferPool else { continue }
    var maybeBuf: CVPixelBuffer?
    CVPixelBufferPoolCreatePixelBuffer(nil, pool, &maybeBuf)
    guard let buf = maybeBuf else { continue }

    CVPixelBufferLockBaseAddress(buf, [])
    if let ctx = CGContext(data: CVPixelBufferGetBaseAddress(buf),
                           width: w, height: h, bitsPerComponent: 8,
                           bytesPerRow: CVPixelBufferGetBytesPerRow(buf),
                           space: space,
                           bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue) {
        ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
    }
    CVPixelBufferUnlockBaseAddress(buf, [])

    while !input.isReadyForMoreMediaData { usleep(2000) }
    adaptor.append(buf, withPresentationTime: CMTime(value: frame, timescale: fps))
    frame += 1
}

input.markAsFinished()
let done = DispatchSemaphore(value: 0)
writer.finishWriting { done.signal() }
done.wait()

if writer.status == .completed {
    print("wrote \(outURL.path) (\(frame) frames, \(w)x\(h) @ \(fps)fps)")
} else {
    FileHandle.standardError.write("encode failed: \(String(describing: writer.error))\n".data(using: .utf8)!)
    exit(1)
}
