#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
mabobot_user_home="${HOME:?HOME must be set}"
mabobot_local_share="${mabobot_user_home}/.local/share"
local_bin="${MABOBOT_LOCAL_BIN:-${mabobot_user_home}/.local/bin}"
default_native_prefix="${mabobot_local_share}/mabobot-file-tools"
native_prefix="${MABOBOT_FILE_TOOLS_PREFIX:-}"
if [[ -z "${native_prefix}" ]]; then
    native_prefix="${default_native_prefix}"
    if [[ ! -x "${native_prefix}/bin/pdftotext" ]]; then
        detected_native_tool="$(command -v pdftotext 2>/dev/null || command -v qpdf 2>/dev/null || true)"
        if [[ -n "${detected_native_tool}" ]]; then
            detected_native_tool="$(readlink -f "${detected_native_tool}")"
            detected_native_bin="$(dirname "${detected_native_tool}")"
            detected_native_prefix="$(dirname "${detected_native_bin}")"
            if [[ "${detected_native_prefix}" == "${mabobot_local_share}/"* ]]; then
                native_prefix="${detected_native_prefix}"
            fi
        fi
    fi
fi
tesseract_prefix="${MABOBOT_TESSERACT_PREFIX:-${mabobot_local_share}/mabobot-tesseract}"
clam_root="${MABOBOT_CLAMAV_ROOT:-${mabobot_local_share}/mabobot-clamav/usr/local}"
clam_database="${MABOBOT_CLAMAV_DATABASE:-${mabobot_local_share}/mabobot-clamav-db}"
clam_temp="${MABOBOT_CLAMAV_TEMP:-/tmp/mabobot-clamav-tmp}"
font_cache="/tmp/mabobot-fontconfig-cache"
runtime_config_dir="${mabobot_local_share}/mabobot/runtime"

install -d "${local_bin}" \
    "${native_prefix}/etc/ImageMagick-7" \
    "${native_prefix}/etc/fonts" \
    "${clam_root}/etc" \
    "${clam_database}" \
    "${clam_temp}" \
    "${font_cache}" \
    "${runtime_config_dir}"
chmod 0700 "${runtime_config_dir}"
runtime_env_tmp="$(mktemp "${runtime_config_dir}/.file-tools-env.XXXXXX")"
{
    printf 'MABOBOT_REGISTERED_FILE_TOOLS_PREFIX=%q\n' "${native_prefix}"
    printf 'MABOBOT_REGISTERED_TESSERACT_PREFIX=%q\n' "${tesseract_prefix}"
    printf 'MABOBOT_REGISTERED_CLAMAV_ROOT=%q\n' "${clam_root}"
    printf 'MABOBOT_REGISTERED_CLAMAV_DATABASE=%q\n' "${clam_database}"
} >"${runtime_env_tmp}"
chmod 0600 "${runtime_env_tmp}"
mv -f "${runtime_env_tmp}" "${runtime_config_dir}/file-tools.env"
install -m 0755 "${script_dir}/mabobot-native-tool" "${local_bin}/mabobot-native-tool"
install -m 0755 "${script_dir}/mabobot-clamav" "${local_bin}/mabobot-clamav"
install -m 0644 "${script_dir}/freshclam.conf" "${clam_root}/etc/freshclam.conf"
install -m 0644 "${script_dir}/imagemagick-policy.xml" "${native_prefix}/etc/ImageMagick-7/policy.xml"
install -m 0644 "${script_dir}/fontconfig.xml" "${native_prefix}/etc/fonts/mabobot-fonts.conf"

native_tools=(
    pdftotext pdfinfo pdftoppm pdftocairo pdfimages pdfseparate pdfunite
    qpdf gs tesseract mutool magick convert identify mogrify compare composite
    montage exiftool mediainfo file bsdtar 7z 7za 7zr 7zz jq yq xmlstarlet
    pandoc fc-list fc-match fc-cache
)
for tool_name in "${native_tools[@]}"; do
    tool_path="${native_prefix}/bin/${tool_name}"
    if [[ "${tool_name}" == "tesseract" && -x "${tesseract_prefix}/bin/tesseract" ]]; then
        tool_path="${tesseract_prefix}/bin/tesseract"
    fi
    if [[ -x "${tool_path}" ]]; then
        if [[ ! -e "${local_bin}/${tool_name}" ]] \
            || { [[ -L "${local_bin}/${tool_name}" ]] \
                && [[ "$(readlink "${local_bin}/${tool_name}")" == "mabobot-native-tool" ]]; }; then
            ln -sfn mabobot-native-tool "${local_bin}/${tool_name}"
        fi
    elif [[ -L "${local_bin}/${tool_name}" ]] \
        && [[ "$(readlink "${local_bin}/${tool_name}")" == "mabobot-native-tool" ]]; then
        unlink "${local_bin}/${tool_name}"
    fi
done
if [[ -x "${clam_root}/bin/clamscan" ]]; then
    ln -sfn mabobot-clamav "${local_bin}/clamscan"
elif [[ -L "${local_bin}/clamscan" ]] \
    && [[ "$(readlink "${local_bin}/clamscan")" == "mabobot-clamav" ]]; then
    unlink "${local_bin}/clamscan"
fi
if [[ -x "${clam_root}/bin/freshclam" ]]; then
    ln -sfn mabobot-clamav "${local_bin}/freshclam"
elif [[ -L "${local_bin}/freshclam" ]] \
    && [[ "$(readlink "${local_bin}/freshclam")" == "mabobot-clamav" ]]; then
    unlink "${local_bin}/freshclam"
fi

probe_args=(
    --json
    --write-manifest
    --trusted-root "${native_prefix}"
    --trusted-root "${tesseract_prefix}"
    --trusted-root "${clam_root}"
    --trusted-root "${clam_database}"
)
if ! python3 "${script_dir}/probe_runtime.py" "${probe_args[@]}" >/dev/null; then
    echo "warning: launchers installed, but Codex CLI was not detected in this WSL user" >&2
fi
echo "mabobot file tools registered from ${native_prefix}"
