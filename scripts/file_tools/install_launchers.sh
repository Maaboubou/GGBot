#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
wxautox_user_home="${HOME:?HOME must be set}"
wxautox_local_share="${wxautox_user_home}/.local/share"
local_bin="${WXAUTOX_LOCAL_BIN:-${wxautox_user_home}/.local/bin}"
native_prefix="${WXAUTOX_FILE_TOOLS_PREFIX:-${wxautox_local_share}/wxautox-file-tools}"
tesseract_prefix="${WXAUTOX_TESSERACT_PREFIX:-${wxautox_local_share}/wxautox-tesseract}"
clam_root="${WXAUTOX_CLAMAV_ROOT:-${wxautox_local_share}/wxautox-clamav/usr/local}"
clam_database="${WXAUTOX_CLAMAV_DATABASE:-${wxautox_local_share}/wxautox-clamav-db}"
clam_temp="${WXAUTOX_CLAMAV_TEMP:-/tmp/wxautox-clamav-tmp}"
font_cache="/tmp/wxautox-fontconfig-cache"

install -d "${local_bin}" \
    "${native_prefix}/etc/ImageMagick-7" \
    "${native_prefix}/etc/fonts" \
    "${clam_root}/etc" \
    "${clam_database}" \
    "${clam_temp}" \
    "${font_cache}"
install -m 0755 "${script_dir}/wxautox-native-tool" "${local_bin}/wxautox-native-tool"
install -m 0755 "${script_dir}/wxautox-clamav" "${local_bin}/wxautox-clamav"
install -m 0644 "${script_dir}/freshclam.conf" "${clam_root}/etc/freshclam.conf"
install -m 0644 "${script_dir}/imagemagick-policy.xml" "${native_prefix}/etc/ImageMagick-7/policy.xml"
install -m 0644 "${script_dir}/fontconfig.xml" "${native_prefix}/etc/fonts/wxautox-fonts.conf"

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
        ln -sfn wxautox-native-tool "${local_bin}/${tool_name}"
    elif [[ -L "${local_bin}/${tool_name}" ]] \
        && [[ "$(readlink "${local_bin}/${tool_name}")" == "wxautox-native-tool" ]]; then
        unlink "${local_bin}/${tool_name}"
    fi
done
if [[ -x "${clam_root}/bin/clamscan" ]]; then
    ln -sfn wxautox-clamav "${local_bin}/clamscan"
elif [[ -L "${local_bin}/clamscan" ]] \
    && [[ "$(readlink "${local_bin}/clamscan")" == "wxautox-clamav" ]]; then
    unlink "${local_bin}/clamscan"
fi
if [[ -x "${clam_root}/bin/freshclam" ]]; then
    ln -sfn wxautox-clamav "${local_bin}/freshclam"
elif [[ -L "${local_bin}/freshclam" ]] \
    && [[ "$(readlink "${local_bin}/freshclam")" == "wxautox-clamav" ]]; then
    unlink "${local_bin}/freshclam"
fi
