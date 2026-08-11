import * as pdfjsLib from "./vendor/pdfjs-6.1.200/pdf.min.mjs";
import { createMultiframeDicom, createUid, decodePngPixels } from "./dicom.mjs";

const PDF_ASSET_BASE = new URL("./vendor/pdfjs-6.1.200/", import.meta.url);
pdfjsLib.GlobalWorkerOptions.workerSrc = new URL("pdf.worker.min.mjs", PDF_ASSET_BASE).href;

const PDF_DPI = 200;
const JPEG_QUALITY = 0.9;
const MAX_LOCAL_ITEMS = 10_000;
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

const dropZone = document.querySelector("#drop-zone");
const fileInput = document.querySelector("#file-input");
const chooseButton = document.querySelector("#choose-button");
const uploadTitle = document.querySelector("#upload-title");
const uploadHelp = document.querySelector("#upload-help");
const toolOptions = document.querySelectorAll("[data-tool]");
const uploadStatus = document.querySelector("#upload-status");
const uploadName = document.querySelector("#upload-name");
const progressLabel = document.querySelector("#progress-label");
const uploadProgress = document.querySelector("#upload-progress");
const message = document.querySelector("#message");
const conversionForm = document.querySelector("#conversion-form");
const dicomFields = document.querySelector("#dicom-fields");
const studyDate = document.querySelector("#study-date");
const patientId = document.querySelector("#patient-id");
const conversionState = document.querySelector("#conversion-state");
const clearFile = document.querySelector("#clear-file");
const startConversion = document.querySelector("#start-conversion");
const resultPanel = document.querySelector("#result-panel");
const resultSummary = document.querySelector("#result-summary");
const downloadResult = document.querySelector("#download-result");
const dicomManual = document.querySelector("#dicom-manual");

let activeTool = "dicom";
let selectedFile = null;
let converting = false;
let resultObjectUrl = null;

const formatBytes = bytes => {
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
};

const stem = filename => filename.replace(/\.[^.]+$/, "") || "converted";
const nextFrame = () => new Promise(resolve => requestAnimationFrame(resolve));

function setMessage(text, error = false) {
  message.textContent = text;
  message.className = error ? "message error" : "message";
}

function localizedMessage(error, fallback) {
  const text = String(error?.message || "").trim();
  return /[\u3040-\u30ff\u3400-\u9fff]/.test(text) ? text : fallback;
}

function setProgress(percent, label) {
  const bounded = Math.max(0, Math.min(100, Math.round(percent)));
  uploadProgress.style.width = `${bounded}%`;
  progressLabel.textContent = label || `${bounded}%`;
}

function releaseResult() {
  if (resultObjectUrl) URL.revokeObjectURL(resultObjectUrl);
  resultObjectUrl = null;
  downloadResult.removeAttribute("href");
  resultPanel.hidden = true;
}

function resetSelection() {
  selectedFile = null;
  fileInput.value = "";
  uploadStatus.hidden = true;
  clearFile.disabled = true;
  startConversion.disabled = true;
  releaseResult();
  setProgress(0, "準備完了");
  setMessage("");
  conversionState.textContent = activeTool === "pdf"
    ? "PDFファイルを選択すると、ローカル変換を開始できます。"
    : "ZIPファイルを選択すると、ローカル変換を開始できます。";
}

function acceptFile(file) {
  const expected = activeTool === "pdf" ? ".pdf" : ".zip";
  if (!file || !file.name.toLowerCase().endsWith(expected)) {
    setMessage(`${activeTool === "pdf" ? "PDF" : "ZIP"}ファイルを選択してください。`, true);
    return;
  }
  releaseResult();
  selectedFile = file;
  uploadStatus.hidden = false;
  uploadName.textContent = `${file.name} · ${formatBytes(file.size)}`;
  setProgress(0, "ローカルで準備完了");
  clearFile.disabled = false;
  startConversion.disabled = false;
  conversionState.textContent = "準備完了です。「変換を開始」を押してください。ファイルはアップロードされません。";
  setMessage("ファイルをブラウザ内で選択しました。サーバーには送信されていません。");
}

function activateTool(tool) {
  if (converting) {
    setMessage("実行中のローカル変換が完了してからツールを切り替えてください。", true);
    return;
  }
  activeTool = tool;
  const isPdf = tool === "pdf";
  toolOptions.forEach(option => {
    const selected = option.dataset.tool === tool;
    option.classList.toggle("active", selected);
    option.setAttribute("aria-pressed", String(selected));
  });
  fileInput.accept = isPdf ? ".pdf,application/pdf" : ".zip,application/zip";
  uploadTitle.textContent = isPdf ? "PDFファイルを選択" : "ZIPファイルを選択";
  uploadHelp.textContent = isPdf
    ? "各ページをブラウザ内で高画質JPGとして変換します。"
    : "PNG・JPEGスライスを並べ、1つのマルチフレームDCMにまとめます。";
  dicomFields.hidden = isPdf;
  dicomManual.hidden = isPdf;
  resetSelection();
}

function browserDecodedPixels(bytes, mimeType) {
  return createImageBitmap(new Blob([bytes], { type: mimeType })).then(bitmap => {
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    context.drawImage(bitmap, 0, 0);
    const rgba = context.getImageData(0, 0, bitmap.width, bitmap.height).data;
    const pixels = new Uint16Array(bitmap.width * bitmap.height);
    for (let index = 0; index < pixels.length; index += 1) {
      const offset = index * 4;
      pixels[index] = Math.round(
        (rgba[offset] * 299 + rgba[offset + 1] * 587 + rgba[offset + 2] * 114) / 1000
      );
    }
    const result = { pixels, width: bitmap.width, height: bitmap.height, bitDepth: 8 };
    bitmap.close();
    canvas.width = 1;
    canvas.height = 1;
    return result;
  });
}

async function decodeImage(entry) {
  const bytes = await entry.async("uint8array");
  const lowerName = entry.name.toLowerCase();
  if (lowerName.endsWith(".png")) {
    try {
      return await decodePngPixels(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
    } catch (error) {
      if (error.preserveDepth) {
        throw new Error(`${entry.name}: ${error.message}。16ビット精度を安全に保持できません`);
      }
      if (error.name !== "UnsupportedPngError") throw error;
      return browserDecodedPixels(bytes, "image/png");
    }
  }
  return browserDecodedPixels(bytes, "image/jpeg");
}

async function convertImagesToDicom(file, metadata, onProgress) {
  if (!window.JSZip) throw new Error("ローカルZIPライブラリを読み込めませんでした");
  onProgress(2, "ZIPをブラウザ内で開いています…");
  const source = await window.JSZip.loadAsync(file);
  const entries = Object.values(source.files)
    .filter(entry => !entry.dir && /\.(png|jpe?g)$/i.test(entry.name))
    .sort((left, right) => collator.compare(left.name, right.name));
  if (!entries.length) throw new Error("ZIPにPNGまたはJPEG画像が含まれていません");
  if (entries.length > MAX_LOCAL_ITEMS) {
    throw new Error(`ZIP内の画像数が上限の${MAX_LOCAL_ITEMS.toLocaleString()}件を超えています`);
  }

  const studyUid = createUid();
  const seriesUid = createUid();
  const frameUid = createUid();
  const frames = [];
  let expectedWidth = null;
  let expectedHeight = null;
  let sourceBitDepth = 8;

  for (let index = 0; index < entries.length; index += 1) {
    const entry = entries[index];
    onProgress(5 + (index / entries.length) * 78, `スライスを変換中：${index + 1} / ${entries.length}…`);
    const image = await decodeImage(entry);
    if (expectedWidth === null) {
      expectedWidth = image.width;
      expectedHeight = image.height;
    } else if (image.width !== expectedWidth || image.height !== expectedHeight) {
      throw new Error(
        `画像サイズが一致しません：${entry.name} は ${image.width}×${image.height}、必要なサイズは ${expectedWidth}×${expectedHeight} です`
      );
    }
    sourceBitDepth = Math.max(sourceBitDepth, image.bitDepth);
    frames.push(image.pixels);
    await nextFrame();
  }

  onProgress(86, "マルチフレームDCMをブラウザ内で作成しています…");
  const dicom = createMultiframeDicom({
    frames,
    width: expectedWidth,
    height: expectedHeight,
    studyUid,
    seriesUid,
    frameUid,
    studyDate: metadata.studyDate,
    patientId: metadata.patientId,
  });
  const blob = new Blob([dicom], { type: "application/dicom" });
  onProgress(100, "マルチフレームDCMの準備完了");
  return {
    blob,
    filename: `${stem(file.name)}_multiframe.dcm`,
    summary: `1つのDCMに${entries.length}フレーム · 元画像${sourceBitDepth}ビット · ${formatBytes(blob.size)}`,
  };
}

function canvasBlob(canvas, type, quality) {
  return new Promise((resolve, reject) => {
    canvas.toBlob(blob => {
      if (blob) resolve(blob);
      else reject(new Error("ブラウザでページ画像をエンコードできませんでした"));
    }, type, quality);
  });
}

async function convertPdfToJpg(file, onProgress) {
  if (!window.JSZip) throw new Error("ローカルZIPライブラリを読み込めませんでした");
  onProgress(2, "PDFをブラウザ内で開いています…");
  const loadingTask = pdfjsLib.getDocument({
    data: new Uint8Array(await file.arrayBuffer()),
    cMapUrl: new URL("cmaps/", PDF_ASSET_BASE).href,
    cMapPacked: true,
    standardFontDataUrl: new URL("standard_fonts/", PDF_ASSET_BASE).href,
    wasmUrl: new URL("wasm/", PDF_ASSET_BASE).href,
    iccUrl: new URL("iccs/", PDF_ASSET_BASE).href,
  });
  let documentProxy;
  try {
    documentProxy = await loadingTask.promise;
    if (documentProxy.numPages > MAX_LOCAL_ITEMS) {
      throw new Error(`PDFのページ数が上限の${MAX_LOCAL_ITEMS.toLocaleString()}ページを超えています`);
    }
    const output = new window.JSZip();
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d", { alpha: false });
    for (let pageNumber = 1; pageNumber <= documentProxy.numPages; pageNumber += 1) {
      onProgress(
        5 + ((pageNumber - 1) / documentProxy.numPages) * 78,
        `ページを変換中：${pageNumber} / ${documentProxy.numPages}…`
      );
      const page = await documentProxy.getPage(pageNumber);
      const viewport = page.getViewport({ scale: PDF_DPI / 72 });
      canvas.width = Math.ceil(viewport.width);
      canvas.height = Math.ceil(viewport.height);
      context.fillStyle = "#fff";
      context.fillRect(0, 0, canvas.width, canvas.height);
      await page.render({ canvasContext: context, viewport }).promise;
      const jpg = await canvasBlob(canvas, "image/jpeg", JPEG_QUALITY);
      output.file(`page_${String(pageNumber).padStart(4, "0")}.jpg`, jpg, {
        compression: "STORE",
      });
      page.cleanup();
      await nextFrame();
    }
    canvas.width = 1;
    canvas.height = 1;
    onProgress(85, "JPGをZIPにまとめています…");
    const blob = await output.generateAsync(
      { type: "blob", compression: "STORE", mimeType: "application/zip" },
      status => onProgress(85 + status.percent * 0.15, `ZIP作成中… ${Math.round(status.percent)}%`)
    );
    return {
      blob,
      filename: `${stem(file.name)}_jpg.zip`,
      summary: `${documentProxy.numPages}ページをブラウザ内で変換 · ${formatBytes(blob.size)}`,
    };
  } finally {
    await loadingTask.destroy();
  }
}

function validateDicomMetadata() {
  if (!studyDate.value) throw new Error("検査日を選択してください");
  const id = patientId.value.trim();
  if (id.length > 64) throw new Error("患者IDは64文字以内で入力してください");
  if (id.includes("\\")) throw new Error("患者IDにバックスラッシュは使用できません");
  if ([...id].some(character => character.charCodeAt(0) < 0x20 || character.charCodeAt(0) > 0x7e)) {
    throw new Error("患者IDには印刷可能なASCII文字のみ使用できます");
  }
  return { studyDate: studyDate.value, patientId: id };
}

conversionForm.addEventListener("submit", async event => {
  event.preventDefault();
  if (!selectedFile || converting) return;
  let metadata = {};
  try {
    if (activeTool === "dicom") metadata = validateDicomMetadata();
  } catch (error) {
    setMessage(error.message, true);
    return;
  }

  converting = true;
  startConversion.disabled = true;
  clearFile.disabled = true;
  toolOptions.forEach(option => { option.disabled = true; });
  releaseResult();
  setMessage("100%オフライン：このブラウザ内だけで変換しています。");
  try {
    const result = activeTool === "pdf"
      ? await convertPdfToJpg(selectedFile, setProgress)
      : await convertImagesToDicom(selectedFile, metadata, setProgress);
    resultObjectUrl = URL.createObjectURL(result.blob);
    downloadResult.href = resultObjectUrl;
    downloadResult.download = result.filename;
    downloadResult.textContent = `${result.filename} をダウンロード`;
    resultSummary.textContent = result.summary;
    resultPanel.hidden = false;
    setProgress(100, "完了");
    conversionState.textContent = "変換が完了しました。結果はページを再読み込みするまで利用できます。";
    setMessage("オフライン変換が完了しました。元ファイルと結果はアップロード・保存されていません。");
    downloadResult.click();
    resultPanel.scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (error) {
    console.error(error);
    setProgress(0, "失敗");
    conversionState.textContent = "ローカル変換に失敗しました。ファイルを確認して再度お試しください。";
    setMessage(localizedMessage(error, "変換中にエラーが発生しました。ファイルを確認して再度お試しください。"), true);
  } finally {
    converting = false;
    startConversion.disabled = false;
    clearFile.disabled = false;
    toolOptions.forEach(option => { option.disabled = false; });
  }
});

chooseButton.addEventListener("click", event => {
  event.stopPropagation();
  fileInput.click();
});
dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("keydown", event => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    fileInput.click();
  }
});
fileInput.addEventListener("change", () => acceptFile(fileInput.files[0]));
["dragenter", "dragover"].forEach(name => dropZone.addEventListener(name, event => {
  event.preventDefault();
  dropZone.classList.add("dragging");
}));
["dragleave", "drop"].forEach(name => dropZone.addEventListener(name, event => {
  event.preventDefault();
  dropZone.classList.remove("dragging");
}));
dropZone.addEventListener("drop", event => acceptFile(event.dataTransfer.files[0]));
toolOptions.forEach(option => option.addEventListener("click", () => activateTool(option.dataset.tool)));
clearFile.addEventListener("click", resetSelection);

window.addEventListener("beforeunload", releaseResult);
if (!studyDate.value) studyDate.value = `${new Date().getFullYear()}-01-01`;
activateTool("dicom");
