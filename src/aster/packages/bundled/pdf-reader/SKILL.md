---
name: pdf-reader
description: Read a PDF with source-linked text extraction and bounded visual page inspection.
license: MIT; see LICENSE.txt and NOTICE.md
---

# PDF reader

Adapted from Kocoro's own MIT-licensed pdf-reader workflow. No proprietary Anthropic pdf skill or Office layout engine is included.

1. Confirm the requested pages or question and the accessible input path. Discover `pdf_to_text,pdf_read_pages,image_read` with `tool_search`. Inspect tool schemas before calling them.
2. Use `pdf_to_text(path=...)` for searchable text. Retain the returned source path, source hash and page coordinates. Text extraction may omit diagrams, equations, scanned text and reading order. A page with no extracted text is not necessarily blank.
3. Use `pdf_read_pages(path=...,pages="1-3,5",max_pages=4)` for visual evidence. Pages are one-based; each call supports at most 10 pages. Respect the returned total_pages/pages and note which pages were inspected. For a long document, batch only the relevant pages; never claim a full-document review after seeing a subset.
4. Rendered images enter the model's normal multimodal tool output with artifact references. If the active provider cannot consume images, report that limitation; a filename is not visual evidence. `image_read` can inspect a separately supplied figure but cannot recover text never present in the image.
5. For each answer, distinguish extracted text, visual observations and inference. Cite the source filename and actual PDF page number. Cross-check numeric/table claims against the relevant page image when layout changes interpretation. Flag uncertain or unreadable content; do not pretend OCR was performed by the text extractor.
6. Use `templates/reading-notes.md` to capture coverage, claims and open questions. Encrypted or malformed documents should yield a clear limitation. Do not install ad hoc packages, modify the source PDF, or promise arbitrary PDF editing through this workflow.
