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

Run `./scripts/file_tools/install_launchers.sh` once on each machine, and again
after changing a launcher or replacing the native environment. The installer
is idempotent: it detects an existing user-level tool environment (including a
non-default `~/.local/share/*` prefix), preserves working direct launchers, and
writes `~/.local/share/wxautox/runtime/file-tools.json`. Refresh ClamAV
signatures with `freshclam`. Python-side tools are recorded separately in
`requirements-file-tools.txt` so the main Windows chatbot environment is not
forced to install WSL-only OCR dependencies.

All defaults are derived from the runtime user's `$HOME`. Machines with a
different layout can set `WXAUTOX_LOCAL_BIN`, `WXAUTOX_FILE_TOOLS_PREFIX`,
`WXAUTOX_TESSERACT_PREFIX`, `WXAUTOX_CLAMAV_ROOT`,
`WXAUTOX_CLAMAV_DATABASE`, or `WXAUTOX_CLAMAV_TEMP` before starting the
chatbot and running the launcher installer. A machine without
these optional tools still starts normally; the chatbot probes the WSL runtime
directly and only advertises commands it can actually find. The Codex 运行中心
shows the discovered commands and can refresh the short-lived capability cache.
It also discovers WSL-native `codex*` executables on the Linux search path and
accepts a manually entered absolute WSL path. Windows-mounted launchers under
`/mnt/<drive>` are rejected. A wrapper such as `~/.local/bin/codex-model-a` can
select a separate `CODEX_HOME`, profile, model provider, or installation; the
chosen path is persisted in the application settings after its Codex protocol
compatibility check succeeds.
Adding or removing commands inside a registered environment needs only a
refresh; changing the environment root requires rerunning the installer and
restarting the Codex runtime so the new root enters the sandbox profile.

Check `tesseract --list-langs` before selecting OCR models. For mixed Chinese
and English documents, `chi_sim+chi_tra+eng` is a useful starting point when
all three are installed; add `osd` only when orientation/script detection is
needed and available.

Treat all received files as untrusted. Scan them before deeper parsing when
practical, never execute embedded macros/scripts/binaries, and keep staged
inputs read-only.
