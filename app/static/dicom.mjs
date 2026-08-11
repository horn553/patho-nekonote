const encoder = new TextEncoder();

const LEGACY_CONVERTED_ENHANCED_CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2.2";
const EXPLICIT_VR_LITTLE_ENDIAN = "1.2.840.10008.1.2.1";
const IMPLEMENTATION_CLASS_UID = "2.25.130330477636078750424803936495522835506";
const LONG_VRS = new Set(["OB", "OD", "OF", "OL", "OV", "OW", "SQ", "UC", "UR", "UT", "UN"]);
let fallbackUidCounter = 0;

function concatBytes(parts) {
  const length = parts.reduce((total, part) => total + part.length, 0);
  const output = new Uint8Array(length);
  let offset = 0;
  for (const part of parts) {
    output.set(part, offset);
    offset += part.length;
  }
  return output;
}

function binaryValue(vr, value) {
  const values = Array.isArray(value) ? value : [value];
  const bytesPerValue = vr === "UL" ? 4 : 2;
  const output = new Uint8Array(values.length * bytesPerValue);
  const view = new DataView(output.buffer);
  values.forEach((item, index) => {
    if (vr === "UL") view.setUint32(index * 4, Number(item), true);
    else view.setUint16(index * 2, Number(item), true);
  });
  return output;
}

function textValue(vr, value) {
  let output = encoder.encode(String(value));
  if (output.length % 2) {
    const padded = new Uint8Array(output.length + 1);
    padded.set(output);
    padded[padded.length - 1] = vr === "UI" ? 0 : 0x20;
    output = padded;
  }
  return output;
}

function element(group, tag, vr, value) {
  let bytes;
  if (value instanceof Uint8Array) bytes = value;
  else if (vr === "US" || vr === "UL" || vr === "AT") bytes = binaryValue(vr, value);
  else bytes = textValue(vr, value);

  if (bytes.length % 2) {
    const padded = new Uint8Array(bytes.length + 1);
    padded.set(bytes);
    bytes = padded;
  }

  const longLength = LONG_VRS.has(vr);
  const header = new Uint8Array(longLength ? 12 : 8);
  const view = new DataView(header.buffer);
  view.setUint16(0, group, true);
  view.setUint16(2, tag, true);
  header[4] = vr.charCodeAt(0);
  header[5] = vr.charCodeAt(1);
  if (longLength) view.setUint32(8, bytes.length, true);
  else view.setUint16(6, bytes.length, true);
  return concatBytes([header, bytes]);
}

function item(dataset) {
  const header = new Uint8Array(8);
  const view = new DataView(header.buffer);
  view.setUint16(0, 0xfffe, true);
  view.setUint16(2, 0xe000, true);
  view.setUint32(4, dataset.length, true);
  return concatBytes([header, dataset]);
}

function sequence(group, tag, datasets) {
  return element(group, tag, "SQ", concatBytes(datasets.map(item)));
}

function decimal(value) {
  if (Number.isInteger(value)) return String(value);
  return Number(value.toFixed(6)).toString();
}

export function createUid() {
  const cryptography = globalThis.crypto;
  if (typeof cryptography?.randomUUID === "function") {
    const hexadecimal = cryptography.randomUUID().replace(/-/g, "");
    return `2.25.${BigInt(`0x${hexadecimal}`).toString(10)}`;
  }

  const bytes = new Uint8Array(16);
  if (typeof cryptography?.getRandomValues === "function") {
    cryptography.getRandomValues(bytes);
  } else {
    // Very old/non-standard browsers may lack Web Crypto entirely. Mix time and
    // a per-page counter into Math.random output so UID creation still works.
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
    let timestamp = Date.now();
    for (let index = 5; index >= 0; index -= 1) {
      bytes[index] ^= timestamp % 256;
      timestamp = Math.floor(timestamp / 256);
    }
    fallbackUidCounter = (fallbackUidCounter + 1) >>> 0;
    bytes[12] ^= fallbackUidCounter >>> 24;
    bytes[13] ^= fallbackUidCounter >>> 16;
    bytes[14] ^= fallbackUidCounter >>> 8;
    bytes[15] ^= fallbackUidCounter;
  }

  // Represent the random 128-bit value using the UUID-derived 2.25 UID root.
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hexadecimal = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  return `2.25.${BigInt(`0x${hexadecimal}`).toString(10)}`;
}

export function createMultiframeDicom({
  frames,
  width,
  height,
  studyUid,
  seriesUid,
  frameUid,
  studyDate,
  patientId,
  sliceThickness = 1,
  pixelSpacing = 1,
}) {
  if (!Array.isArray(frames) || !frames.length) {
    throw new Error("画像フレームが1つ以上必要です");
  }
  if (frames.some(frame => !(frame instanceof Uint16Array) || frame.length !== width * height)) {
    throw new Error("フレームのピクセルデータと画像サイズが一致しません");
  }
  if (width < 1 || height < 1 || width > 65535 || height > 65535) {
    throw new Error("画像サイズがDICOMの上限を超えています");
  }

  let minimum = 65535;
  let maximum = 0;
  const pixelByteLength = frames.length * width * height * 2;
  if (pixelByteLength > 0xfffffffe) {
    throw new Error("マルチフレームDICOMのピクセルデータが4GBの上限を超えています");
  }
  const pixelBytes = new Uint8Array(pixelByteLength);
  const pixelView = new DataView(pixelBytes.buffer);
  let pixelIndex = 0;
  frames.forEach(frame => frame.forEach(value => {
    minimum = Math.min(minimum, value);
    maximum = Math.max(maximum, value);
    pixelView.setUint16(pixelIndex * 2, value, true);
    pixelIndex += 1;
  }));

  const sopInstanceUid = createUid();
  const metadataBody = concatBytes([
    element(0x0002, 0x0001, "OB", new Uint8Array([0, 1])),
    element(0x0002, 0x0002, "UI", LEGACY_CONVERTED_ENHANCED_CT_STORAGE),
    element(0x0002, 0x0003, "UI", sopInstanceUid),
    element(0x0002, 0x0010, "UI", EXPLICIT_VR_LITTLE_ENDIAN),
    element(0x0002, 0x0012, "UI", IMPLEMENTATION_CLASS_UID),
    element(0x0002, 0x0013, "SH", "NEKONOTE_120"),
  ]);
  const metadata = concatBytes([
    element(0x0002, 0x0000, "UL", metadataBody.length),
    metadataBody,
  ]);

  const dateValue = studyDate.replaceAll("-", "");
  const sharedFunctionalGroups = concatBytes([
    sequence(0x0018, 0x9329, [concatBytes([
      element(0x0008, 0x9007, "CS", "DERIVED\\SECONDARY\\AXIAL\\NONE"),
      element(0x0008, 0x9205, "CS", "MONOCHROME"),
      element(0x0008, 0x9206, "CS", "VOLUME"),
      element(0x0008, 0x9207, "CS", "NONE"),
    ])]),
    sequence(0x0020, 0x9116, [
      element(0x0020, 0x0037, "DS", "1\\0\\0\\0\\1\\0"),
    ]),
    sequence(0x0020, 0x9170, [new Uint8Array()]),
    sequence(0x0028, 0x9110, [concatBytes([
      element(0x0018, 0x0050, "DS", decimal(sliceThickness)),
      element(0x0018, 0x0088, "DS", decimal(sliceThickness)),
      element(0x0028, 0x0030, "DS", `${decimal(pixelSpacing)}\\${decimal(pixelSpacing)}`),
    ])]),
    sequence(0x0028, 0x9132, [concatBytes([
      element(0x0028, 0x1050, "DS", decimal((minimum + maximum) / 2)),
      element(0x0028, 0x1051, "DS", decimal(Math.max(1, maximum - minimum))),
    ])]),
    sequence(0x0028, 0x9145, [concatBytes([
      element(0x0028, 0x1052, "DS", "0"),
      element(0x0028, 0x1053, "DS", "1"),
      element(0x0028, 0x1054, "LO", "US"),
    ])]),
  ]);

  const perFrameFunctionalGroups = frames.map((_, index) => concatBytes([
    sequence(0x0020, 0x9111, [concatBytes([
      element(0x0020, 0x9056, "SH", "1"),
      element(0x0020, 0x9057, "UL", index + 1),
    ])]),
    sequence(0x0020, 0x9113, [
      element(0x0020, 0x0032, "DS", `0\\0\\${decimal(-index * sliceThickness)}`),
    ]),
    sequence(0x0020, 0x9171, [new Uint8Array()]),
  ]));

  const dataset = concatBytes([
    element(0x0008, 0x0008, "CS", "DERIVED\\SECONDARY\\AXIAL\\NONE"),
    element(0x0008, 0x0016, "UI", LEGACY_CONVERTED_ENHANCED_CT_STORAGE),
    element(0x0008, 0x0018, "UI", sopInstanceUid),
    element(0x0008, 0x0020, "DA", dateValue),
    element(0x0008, 0x0030, "TM", ""),
    element(0x0008, 0x0050, "SH", ""),
    element(0x0008, 0x0060, "CS", "CT"),
    element(0x0008, 0x0090, "PN", ""),
    element(0x0010, 0x0010, "PN", ""),
    element(0x0010, 0x0020, "LO", patientId),
    element(0x0010, 0x0030, "DA", ""),
    element(0x0010, 0x0040, "CS", ""),
    element(0x0020, 0x000d, "UI", studyUid),
    element(0x0020, 0x000e, "UI", seriesUid),
    element(0x0020, 0x0010, "SH", ""),
    element(0x0020, 0x0011, "IS", ""),
    element(0x0020, 0x0012, "IS", ""),
    element(0x0020, 0x0013, "IS", 1),
    element(0x0020, 0x0052, "UI", frameUid),
    element(0x0028, 0x0002, "US", 1),
    element(0x0028, 0x0004, "CS", "MONOCHROME2"),
    element(0x0028, 0x0008, "IS", frames.length),
    element(0x0028, 0x0009, "AT", [0x5200, 0x9230]),
    element(0x0028, 0x0010, "US", height),
    element(0x0028, 0x0011, "US", width),
    element(0x0028, 0x0100, "US", 16),
    element(0x0028, 0x0101, "US", 16),
    element(0x0028, 0x0102, "US", 15),
    element(0x0028, 0x0103, "US", 0),
    element(0x0028, 0x6010, "US", 1),
    sequence(0x0040, 0x0555, []),
    element(0x2050, 0x0020, "CS", "IDENTITY"),
    sequence(0x5200, 0x9229, [sharedFunctionalGroups]),
    sequence(0x5200, 0x9230, perFrameFunctionalGroups),
    element(0x7fe0, 0x0010, "OW", pixelBytes),
  ]);

  const preamble = new Uint8Array(132);
  preamble.set(encoder.encode("DICM"), 128);
  return concatBytes([preamble, metadata, dataset]);
}

function paeth(left, above, upperLeft) {
  const estimate = left + above - upperLeft;
  const leftDistance = Math.abs(estimate - left);
  const aboveDistance = Math.abs(estimate - above);
  const diagonalDistance = Math.abs(estimate - upperLeft);
  if (leftDistance <= aboveDistance && leftDistance <= diagonalDistance) return left;
  return aboveDistance <= diagonalDistance ? above : upperLeft;
}

export async function decodePngPixels(arrayBuffer) {
  const bytes = new Uint8Array(arrayBuffer);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const signature = [137, 80, 78, 71, 13, 10, 26, 10];
  if (bytes.length < 24 || !signature.every((value, index) => bytes[index] === value)) {
    throw new Error("PNGファイルが不正です");
  }

  let width;
  let height;
  let bitDepth;
  let colorType;
  let interlace;
  const compressedParts = [];
  let offset = 8;
  while (offset + 12 <= bytes.length) {
    const length = view.getUint32(offset, false);
    const type = String.fromCharCode(...bytes.subarray(offset + 4, offset + 8));
    const start = offset + 8;
    const end = start + length;
    if (end + 4 > bytes.length) throw new Error("PNGチャンクが途中で切れています");
    if (type === "IHDR") {
      width = view.getUint32(start, false);
      height = view.getUint32(start + 4, false);
      bitDepth = bytes[start + 8];
      colorType = bytes[start + 9];
      interlace = bytes[start + 12];
    } else if (type === "IDAT") {
      compressedParts.push(bytes.slice(start, end));
    } else if (type === "IEND") {
      break;
    }
    offset = end + 4;
  }

  const channels = { 0: 1, 2: 3, 4: 2, 6: 4 }[colorType];
  if (!channels || ![8, 16].includes(bitDepth) || interlace !== 0) {
    const error = new Error("このPNG形式にはブラウザの画像デコード機能が必要です");
    error.name = "UnsupportedPngError";
    error.preserveDepth = bitDepth === 16;
    throw error;
  }
  if (!width || !height || !compressedParts.length) throw new Error("PNGデータが不完全です");

  const compressed = concatBytes(compressedParts);
  const inflatedStream = new Blob([compressed]).stream().pipeThrough(
    new DecompressionStream("deflate")
  );
  const inflated = new Uint8Array(await new Response(inflatedStream).arrayBuffer());
  const bytesPerSample = bitDepth / 8;
  const bytesPerPixel = channels * bytesPerSample;
  const stride = width * bytesPerPixel;
  if (inflated.length !== height * (stride + 1)) throw new Error("PNGの走査線データが不正です");

  const reconstructed = new Uint8Array(height * stride);
  let sourceOffset = 0;
  for (let row = 0; row < height; row += 1) {
    const filter = inflated[sourceOffset];
    sourceOffset += 1;
    const rowOffset = row * stride;
    for (let column = 0; column < stride; column += 1) {
      const raw = inflated[sourceOffset + column];
      const left = column >= bytesPerPixel ? reconstructed[rowOffset + column - bytesPerPixel] : 0;
      const above = row > 0 ? reconstructed[rowOffset + column - stride] : 0;
      const upperLeft = row > 0 && column >= bytesPerPixel
        ? reconstructed[rowOffset + column - stride - bytesPerPixel]
        : 0;
      let value;
      if (filter === 0) value = raw;
      else if (filter === 1) value = raw + left;
      else if (filter === 2) value = raw + above;
      else if (filter === 3) value = raw + Math.floor((left + above) / 2);
      else if (filter === 4) value = raw + paeth(left, above, upperLeft);
      else throw new Error("PNGで不明な走査線フィルターが使用されています");
      reconstructed[rowOffset + column] = value & 0xff;
    }
    sourceOffset += stride;
  }

  const pixels = new Uint16Array(width * height);
  const sample = index => bitDepth === 16
    ? (reconstructed[index] << 8) | reconstructed[index + 1]
    : reconstructed[index];
  for (let index = 0; index < pixels.length; index += 1) {
    const pixelOffset = index * bytesPerPixel;
    if (colorType === 0 || colorType === 4) {
      pixels[index] = sample(pixelOffset);
    } else {
      const red = sample(pixelOffset);
      const green = sample(pixelOffset + bytesPerSample);
      const blue = sample(pixelOffset + bytesPerSample * 2);
      pixels[index] = Math.round((red * 299 + green * 587 + blue * 114) / 1000);
    }
  }
  return { pixels, width, height, bitDepth };
}
