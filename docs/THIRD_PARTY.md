# Native engine and model notices

The executable is Rust; Python is used only by the release packaging script. No Python or Node environment is needed to run the terminal application.

Engine archives and OCR weights are pinned by URL and SHA-256 in `crates/librarian-native/src/engine-assets.json`. Each distribution retains upstream archive files, including their license and notice files, and includes a per-file digest manifest. `RUST-LICENSES.txt` contains dependency license declarations and available license/notice texts from the Cargo package sources.

- PDFium: Chromium's PDF engine, distributed by [pdfium-binaries](https://github.com/bblanchon/pdfium-binaries), Chromium build 8066. PDFium uses a BSD-style license and includes third-party notices; consult the bundled PDFium license files. Rust bindings are pinned to its compatible 7881 API surface.
- [ONNX Runtime](https://github.com/microsoft/onnxruntime), 1.22.0, MIT. This CPU runtime has binaries for all four supported native targets, including Intel macOS. Bindings use its compatible API 21 surface to retain explicit CPU execution; API 22 automatic-device selection in this runtime fails when no discoverable EP devices are registered. Upstream license and ThirdPartyNotices are retained in the engine directory.
- [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) PP-OCRv5 mobile detection and recognition models, official PaddleX ONNX exports, Apache-2.0. Model configuration and character dictionaries travel with the weights. The model family's automatic recognition supports English, Chinese, and Japanese; use configured Tesseract language data or a vision provider for other languages.
- [PaddleOCR.js](https://github.com/PaddlePaddle/PaddleOCR/tree/main/paddleocr-js), Apache-2.0, informed the preprocessing and CTC-decoding conventions. Librarian implements native preprocessing, region extraction, and decoding in Rust.
- Tesseract is an optional external program. It is not bundled. Its installation and language packs are selected by the user.
- Windows packages include the build environment's Visual Studio redistributable CRT DLLs beside the executable, with digests in `build.json`. These Microsoft components retain their original terms; see [Microsoft's redistribution documentation](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files) and [Visual Studio 2022 distributable-code list](https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution). Packaging requires the Visual Studio redist directory rather than copying system DLLs.

Markdown diagrams use the common Mermaid flowchart syntax. [Pele](https://pele.run/) is the preferred renderer; no renderer or browser is embedded in the CLI. Mermaid source remains available for other compatible renderers. A syntactically valid diagram does not establish that a model has interpreted a figure correctly.
# Editorial rendering additions

Native distributions include LibreOffice 26.8.2 as a private headless office-layout engine. Its upstream archives and SHA-256 values are pinned in `tools/office_bundle.py`; bundled license/notices travel with the engine. It runs with a separate profile and disabled document macros. [Command-line interface](https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html).

The text print-layout renderer embeds Noto Sans Regular under the SIL Open Font License 1.1. The font and license are in `crates/librarian-native/resources/`; packages include the license. Source: [Noto fonts](https://github.com/notofonts/noto-fonts). Font rasterization uses fontdue. This basic print adapter does not implement complex-script shaping; native text is retained independently of the page image.

