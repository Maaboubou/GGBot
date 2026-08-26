# Chatbot file tools

The chatbot's isolated Codex runtime uses user-level tools instead of requiring
root access. Native binaries live under `~/.local/share/wxautox-file-tools`
and ClamAV under `~/.local/share/wxautox-clamav`. The official Tesseract
overlay uses the stable path `~/.local/share/wxautox-tesseract`; its version is
reported by the binary and is not encoded in the repository configuration.

The launchers in this directory provide the required library/data paths and
bounded ImageMagick resources. They are installed into `~/.local/bin`; native
command names are symlinked to `wxautox-native-tool`.

LibreOffice is intentionally not exposed to isolated chatbot runs: its Linux
single-instance startup requires local IPC capabilities that the untrusted-file
sandbox denies. Use python-docx/openpyxl/python-pptx for structured Office data,
and MarkItDown, Mammoth, Pandoc, or WeasyPrint for supported conversions.

Run `./scripts/file_tools/install_launchers.sh` after changing a launcher or
after updating the native environment. Refresh ClamAV signatures with
`freshclam`. Python-side tools are recorded separately in
`requirements-file-tools.txt` so the main Windows chatbot environment is not
forced to install WSL-only OCR dependencies.

All defaults are derived from the runtime user's `$HOME`. Machines with a
different layout can set `WXAUTOX_LOCAL_BIN`, `WXAUTOX_FILE_TOOLS_PREFIX`,
`WXAUTOX_TESSERACT_PREFIX`, `WXAUTOX_CLAMAV_ROOT`,
`WXAUTOX_CLAMAV_DATABASE`, or `WXAUTOX_CLAMAV_TEMP` before starting the
chatbot and running the launcher installer. A machine without
these optional tools still starts normally; the chatbot probes the runtime and
only advertises commands it can actually find. Restart the chatbot after
installing or removing tools so the cached capability probe is refreshed.

Check `tesseract --list-langs` before selecting OCR models. For mixed Chinese
and English documents, `chi_sim+chi_tra+eng` is a useful starting point when
all three are installed; add `osd` only when orientation/script detection is
needed and available.

Treat all received files as untrusted. Scan them before deeper parsing when
practical, never execute embedded macros/scripts/binaries, and keep staged
inputs read-only.
