#!/bin/sh
{
# Contexto installer. https://github.com/VentureForgre/contexto
#
#   curl -fsSL https://raw.githubusercontent.com/VentureForgre/contexto/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/VentureForgre/contexto/main/install.sh | sh -s -- --dry-run
#   sh install.sh --help
#
# What it does, in this order:
#   1. finds the latest release (or the one asked for with --version);
#   2. downloads that release's SHA256SUMS and its wheel into a private temporary directory;
#   3. checks the wheel's SHA-256 against SHA256SUMS, and with --attest its build attestation;
#   4. when an earlier Contexto is installed, runs the checked wheel once without installing it;
#   5. removes an earlier Contexto install that would provide the same `contexto` command;
#   6. installs the checked wheel file with `uv tool install` or `pipx install`.
#
# What it never does: install uv or pipx; use sudo; edit a shell profile; read or write session
# data (~/.contexto or $CONTEXTO_HOME). It installs no Python itself, but uv may download a Python
# it manages when the machine has none that is 3.12 or later. Until step 4 has passed, nothing is
# installed or removed.
#
# The whole script is one { } group, so a shell reads all of it before it runs any of it: a
# download cut short at any byte is a syntax error and runs nothing. Inside the group there are
# only function definitions and, last, one call of main, which also refuses to run without the
# marker that call passes.

set -eu

usage() {
    cat <<'EOF'
Install or upgrade Contexto from a GitHub release.

Usage: sh install.sh [options]
       curl -fsSL https://raw.githubusercontent.com/VentureForgre/contexto/main/install.sh | sh -s -- [options]

The script finds the latest release, downloads its wheel into a temporary
directory, checks the wheel's SHA-256 against the release's SHA256SUMS, and
installs the checked file with `uv tool install` or `pipx install`. Run it
again to upgrade.

It needs curl, sha256sum or shasum, and uv or pipx. It installs none of
those, asks for no password, and edits no shell startup file. Contexto needs
Python 3.12 or later. pipx must find one on the machine. With uv,
uv may download a Python it manages when the machine has none.

Options:
  --version X.Y.Z   Install that release, not the latest. vX.Y.Z works too.
                    Releases before v0.5.13 have no SHA256SUMS and are refused.
  --with uv|pipx    Install with that tool. Default: the tool that already has
                    Contexto, else uv if it is on PATH, else pipx.
  --attest          Also require `gh attestation verify` to pass for the wheel.
                    Needs the GitHub CLI and `gh auth login`.
  --force           Reinstall when that release is already installed, and let
                    uv or pipx overwrite a `contexto` command they do not own.
  --dry-run         Print what would be downloaded and run. Nothing is
                    installed or removed, and the wheel is not downloaded.
  -h, --help        Show this text.

Environment:
  CONTEXTO_VERSION            Same as --version. The option wins.
  CONTEXTO_INSTALL_BASE_URL   Where the releases are, for mirrors and tests.
                              Default: https://github.com/VentureForgre/contexto
                              https only; http only for 127.0.0.1 or localhost.
                              It must have GitHub's layout (see docs/install.md).

Exit status: 0 when Contexto is installed or already up to date, and after a
dry run. 1 when the install could not be done. 2 when an option, a value or
one of the two variables above is not valid.

Upgrading: releases up to 0.5.24 are the distribution `contexto`, later ones
`contexto-proxy`. Both provide the `contexto` command, so an install under the
other name, or one made by the other tool, is removed before the new one is
installed. That happens only after the new wheel has passed its checks and
has run once with the tool that will install it.

Session data in ~/.contexto (or $CONTEXTO_HOME) is never touched.

The checksum shows that the download is intact. It comes from the same release
as the wheel, so it does not show who built the wheel: --attest does.

More: https://github.com/VentureForgre/contexto/blob/main/docs/install.md
EOF
}

say() {
    printf '%s\n' "$*"
}

die() {
    printf 'install.sh: error: %s\n' "$*" >&2
    exit 1
}

# For a failure before the install step: nothing has been installed or removed yet.
stop() {
    die "$* Nothing was installed."
}

# For input that is not valid: an option, a value, or one of the two environment variables.
usage_error() {
    printf 'install.sh: %s\nRun it with --help for the options.\n' "$*" >&2
    exit 2
}

have() {
    command -v "$1" >/dev/null 2>&1
}

# quoted WORD...: the words as one line that a shell reads back as the same words, so a printed
# command can be pasted. A word with anything but letters, digits and _./:=@%+,- is put in single
# quotes: `--python '>=3.12'`, not a redirection.
quoted() {
    quoted_line=
    for word; do
        case $word in
            '' | *[!A-Za-z0-9_./:=@%+,-]*)
                rest=$word
                word=
                while :; do
                    case $rest in
                        *\'*)
                            word="$word${rest%%\'*}'\\''"
                            rest=${rest#*\'}
                            ;;
                        *)
                            word="$word$rest"
                            break
                            ;;
                    esac
                done
                word="'$word'"
                ;;
        esac
        quoted_line="$quoted_line${quoted_line:+ }$word"
    done
    printf '%s\n' "$quoted_line"
}

# has_force WORD...: one of the words is --force.
has_force() {
    for word; do
        if [ "$word" = --force ]; then
            return 0
        fi
    done
    return 1
}

# is_version X.Y.Z: three numbers of at most six digits, and nothing else.
is_version() {
    case $1 in
        '' | *[!0-9.]* | .* | *. | *..* | *.*.*.*) return 1 ;;
        *[0-9][0-9][0-9][0-9][0-9][0-9][0-9]*) return 1 ;;
        *.*.*) return 0 ;;
    esac
    return 1
}

# older_than A B: version A is older than version B. Both have passed is_version.
older_than() {
    left=$1
    right=$2
    for _ in 1 2 3; do
        if [ "${left%%.*}" -lt "${right%%.*}" ]; then
            return 0
        fi
        if [ "${left%%.*}" -gt "${right%%.*}" ]; then
            return 1
        fi
        left=${left#*.}
        right=${right#*.}
    done
    return 1
}

# download URL FILE MAX_BYTES [more curl options]: the one place that touches the network.
# https only, also across redirects (http only when the base URL is this machine, see main).
# Bounded in time and size: a server that does not answer, sends under 1024 bytes a second for
# $stall_seconds, or sends more than MAX_BYTES ends the transfer with curl's own message.
download() {
    url=$1
    file=$2
    limit=$3
    shift 3
    curl --proto "$proto" --proto-redir "$proto" --tlsv1.2 -fsSL \
        --connect-timeout "$connect_timeout" --speed-limit 1024 --speed-time "$stall_seconds" \
        --max-time "$max_seconds" --max-filesize "$limit" -o "$file" "$@" "$url" </dev/null
}

# Sets tag to the latest release's tag, without the GitHub API: /releases/latest redirects to
# /releases/tag/vX.Y.Z, and the tag is the end of the final URL. Only headers are read: a HEAD
# request, so a mirror must answer HEAD with that redirect.
find_latest() {
    final=$(download "$base/releases/latest" /dev/null 1048576 -I -w '%{url_effective}') ||
        stop "Could not read $base/releases/latest (curl's message is above). --version X.Y.Z skips" \
            "this lookup."
    case $final in
        "$base/releases/tag/v"*) tag=${final#"$base/releases/tag/"} ;;
        *) stop "$base/releases/latest did not lead to a release of this repository." ;;
    esac
    is_version "${tag#v}" || stop "$base/releases/latest led to a tag that is not vX.Y.Z."
}

bad_sums() {
    stop "SHA256SUMS for v$1 is not in the expected form (line $2), so it was not used."
}

# read_sums FILE VERSION: sets wheel and wheel_sha from a release's SHA256SUMS.
#
# The file is untrusted. Every line must be 64 lowercase hex digits, two spaces, and one of the
# four file names a release of exactly this version can carry, compared as whole strings. So a
# name can hold no path separator and nothing else of the file's choosing. Any other line, a
# second wheel, or no wheel refuses the whole file. Its lines are never printed.
#
# A shell drops some bytes when it reads a line (a NUL, for one). So the lines that were read
# are written to a second file, and the two files must have the same SHA-256: what was checked
# is then, byte for byte, what the file holds.
read_sums() {
    wheel=
    wheel_sha=
    sdists=0
    number=0
    ended=0
    : >"$1.read"
    while [ "$ended" = 0 ]; do
        if IFS= read -r line; then
            printf '%s\n' "$line" >>"$1.read"
        else
            ended=1
            if [ -z "$line" ]; then
                break
            fi
            printf '%s' "$line" >>"$1.read"
        fi
        number=$((number + 1))
        digest=${line%%  *}
        name=${line#*  }
        if [ "$digest  $name" != "$line" ] || [ "${#digest}" -ne 64 ]; then
            bad_sums "$2" "$number"
        fi
        case $digest in
            *[!0-9a-f]*) bad_sums "$2" "$number" ;;
        esac
        case $name in
            "contexto-$2-py3-none-any.whl" | "contexto_proxy-$2-py3-none-any.whl")
                if [ -n "$wheel" ]; then
                    stop "SHA256SUMS for v$2 lists more than one wheel, so it was not used."
                fi
                wheel=$name
                wheel_sha=$digest
                ;;
            "contexto-$2.tar.gz" | "contexto_proxy-$2.tar.gz")
                sdists=$((sdists + 1))
                if [ "$sdists" -gt 1 ]; then
                    bad_sums "$2" "$number"
                fi
                ;;
            *) bad_sums "$2" "$number" ;;
        esac
    done <"$1"
    if [ -z "$wheel" ]; then
        stop "SHA256SUMS for v$2 lists no wheel for that version, so it was not used."
    fi
    as_served=$(sha256_of "$1") || as_served=
    as_read=$(sha256_of "$1.read") || as_read=
    if [ -z "$as_served" ] || [ "$as_served" != "$as_read" ]; then
        stop "SHA256SUMS for v$2 holds bytes that are not plain text, so it was not used."
    fi
}

sha256_of() {
    if have sha256sum; then
        sum=$(sha256sum <"$1") || return 1
    else
        sum=$(shasum -a 256 <"$1") || return 1
    fi
    printf '%s\n' "${sum%% *}"
}

# names_in uv|pipx: which of the two distribution names that tool has installed, one per line.
# Releases up to 0.5.24 are `contexto`, later ones `contexto-proxy`. A package counts only when
# it provides the `contexto` command, so one that merely shares the name is left alone. An older
# pipx cannot say where its environments are; there the name alone decides.
names_in() {
    have "$1" || return 0
    case $1 in
        uv)
            uv tool list 2>/dev/null </dev/null | awk '
                /^- / { if (name != "" && $2 == "contexto" && !seen[name]++) print name; next }
                { name = ($1 == "contexto" || $1 == "contexto-proxy") ? $1 : "" }'
            ;;
        pipx)
            in_pipx=$(pipx list --short 2>/dev/null </dev/null | awk '
                ($1 == "contexto" || $1 == "contexto-proxy") && !seen[$1]++ { print $1 }')
            if [ -z "$in_pipx" ]; then
                return 0
            fi
            venvs=$(pipx environment --value PIPX_LOCAL_VENVS 2>/dev/null </dev/null) || venvs=
            for name in $in_pipx; do
                if [ -z "$venvs" ] || [ -e "$venvs/$name/bin/contexto" ]; then
                    printf '%s\n' "$name"
                fi
            done
            ;;
    esac
}

# listed NAME LIST: NAME is one of the words of LIST.
listed() {
    for item in $2; do
        if [ "$item" = "$1" ]; then
            return 0
        fi
    done
    return 1
}

# Sets installer. An existing install is upgraded with the tool that made it.
choose_installer() {
    if [ -n "$with" ]; then
        have "$with" || stop "--with $with: $with is not on PATH."
        installer=$with
    elif [ -n "$uv_names" ]; then
        installer=uv
    elif [ -n "$pipx_names" ]; then
        installer=pipx
    elif have uv; then
        installer=uv
    elif have pipx; then
        installer=pipx
    else
        printf '%s\n' \
            "install.sh: error: neither uv nor pipx is on PATH. Nothing was installed." \
            "Install one of them, then run this script again:" \
            "  uv:   https://docs.astral.sh/uv/getting-started/installation/" \
            "  pipx: https://pipx.pypa.io/stable/installation/" >&2
        exit 1
    fi
}

python_is_new_enough() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1 </dev/null
}

# Sets pipx_python: empty when pipx's default Python is 3.12 or later, else the path of a newer
# one on PATH for --python. Returns 1, with pipx_python empty, when there is neither. uv needs no
# such check: it is given the requirement, and downloads a Python it manages when the machine has
# none that meets it.
find_pipx_python() {
    pipx_python=
    default=$(pipx environment --value PIPX_DEFAULT_PYTHON 2>/dev/null </dev/null) || default=
    if [ -n "$default" ] && python_is_new_enough "$default"; then
        return 0
    fi
    for candidate in python3.15 python3.14 python3.13 python3.12 python3; do
        if found=$(command -v "$candidate" 2>/dev/null) && python_is_new_enough "$found"; then
            pipx_python=$found
            return 0
        fi
    done
    return 1
}

# find_pipx_python, for an install with pipx: with no Python new enough, the script stops.
choose_pipx_python() {
    find_pipx_python && return 0
    stop "Contexto needs Python 3.12 or later. pipx's default Python${default:+ ($default)} is older or" \
        "unknown, and no python3.15, python3.14, python3.13, python3.12 or python3 on PATH is new enough." \
        "Install one, or install uv, which can download one."
}

# Prints the directory the installer puts commands in.
tool_bin_dir() {
    case $installer in
        uv)
            uv tool dir --bin 2>/dev/null </dev/null ||
                printf '%s\n' "${UV_TOOL_BIN_DIR:-${XDG_BIN_HOME:-${HOME-}/.local/bin}}"
            ;;
        pipx)
            pipx environment --value PIPX_BIN_DIR 2>/dev/null </dev/null ||
                printf '%s\n' "${PIPX_BIN_DIR:-${HOME-}/.local/bin}"
            ;;
    esac
}

# Sets contexto_cmd to the contexto that PATH finds, else the one in the installer's bin
# directory, else empty; on_path says which; reported is its --version line.
find_contexto() {
    bin_dir=$(tool_bin_dir)
    on_path=1
    if ! contexto_cmd=$(command -v contexto 2>/dev/null) || [ -z "$contexto_cmd" ]; then
        on_path=0
        contexto_cmd=
        if [ -x "$bin_dir/contexto" ]; then
            contexto_cmd=$bin_dir/contexto
        fi
    fi
    reported=
    if [ -n "$contexto_cmd" ]; then
        reported=$("$contexto_cmd" --version 2>/dev/null </dev/null) || reported=
    fi
}

# Runs the checked wheel once, with the tool that will install it, without installing it. An
# earlier install is removed only after this has printed the release's version, so a wheel or a
# Python that does not work is found while the earlier install is still there. Measured with
# uv 0.13.5 and pipx 1.18.4: neither leaves anything in its tool or bin directory.
try_wheel() {
    case $installer in
        uv) set -- uv tool run --python '>=3.12' --from "$tmpdir/$wheel" contexto --version ;;
        *)
            set -- pipx run
            if [ -n "$pipx_python" ]; then
                set -- "$@" --python "$pipx_python"
            fi
            set -- "$@" --no-cache --spec "$tmpdir/$wheel" contexto --version
            ;;
    esac
    if [ "$dry_run" = 1 ]; then
        say "Would run: $(quoted "$@")"
        return 0
    fi
    if ! tried=$("$@" 2>"$tmpdir/tool.log" </dev/null); then
        cat "$tmpdir/tool.log" >&2
        stop "The checked wheel did not run with $installer: \`$(quoted "$@")\` should print" \
            "\"contexto $version\" ($installer's message is above). The earlier install was left as it was."
    fi
    if [ "$tried" != "contexto $version" ]; then
        stop "The checked wheel ran with $installer, but \`$(quoted "$@")\` should print" \
            "\"contexto $version\". It printed \"$tried\". The earlier install was left as it was."
    fi
}

# Removes every Contexto install except the one the new wheel will replace in place (same tool,
# same distribution name). Measured with uv 0.13.5 and pipx 1.18.4: two installs that provide
# `contexto` do not coexist. uv refuses the second ("Executable already exists"); pipx installs
# it, leaves the command pointing at the first, and exits 0; and `uv tool uninstall` of the
# first later deletes the command even when the second owns it. So the earlier one goes first.
remove_other_installs() {
    for tool in uv pipx; do
        case $tool in
            uv) names=$uv_names ;;
            *) names=$pipx_names ;;
        esac
        for name in $names; do
            if [ "$tool" = "$installer" ] && [ "$name" = "$dist" ]; then
                continue
            fi
            case $tool in
                uv) set -- uv tool uninstall "$name" ;;
                *) set -- pipx uninstall "$name" ;;
            esac
            if [ "$dry_run" = 1 ]; then
                say "Would run: $(quoted "$@")"
                continue
            fi
            say "Removing the earlier install, which provides the same contexto command: $(quoted "$@")"
            if ! "$@" >"$tmpdir/tool.log" 2>&1 </dev/null; then
                cat "$tmpdir/tool.log" >&2
                die "\`$(quoted "$@")\` failed ($tool's message is above). The new release was not installed."
            fi
            removed=1
            case " $removed_by " in
                *" $tool "*) ;;
                *) removed_by="$removed_by${removed_by:+ }$tool" ;;
            esac
        done
    done
}

# Prints, after a failed install that followed a removal, how to get the earlier release back: with
# the tool whose install was removed (the one this run used, when both tools had one), and in a
# form that works for a piped script. A release older than v0.5.13 has no SHA256SUMS, so this
# script refuses it; for one of those it prints the install of the release's wheel by URL.
restore_hint() {
    case $removed_by in
        uv | pipx) restore_with=$removed_by ;;
        *) restore_with=$installer ;;
    esac
    if older_than "$previous" "$oldest_with_sums"; then
        old_url=$base/releases/download/v$previous/contexto-$previous-py3-none-any.whl
        case $restore_with in
            uv) set -- uv tool install --python '>=3.12' "$old_url" ;;
            *)
                # pipx's Python was looked at only if pipx was this run's installer. With none new
                # enough, the hint goes without --python rather than stopping before it is printed.
                if [ "$installer" != pipx ]; then
                    find_pipx_python || :
                fi
                set -- pipx install
                if [ -n "$pipx_python" ]; then
                    set -- "$@" --python "$pipx_python"
                fi
                set -- "$@" "$old_url"
                ;;
        esac
        say "To get $previous back: $(quoted "$@")"
        say "v$previous has no SHA256SUMS, so this script cannot install it, and that download is not" \
            "checksum-verified."
    elif [ "$base" = "$default_base" ]; then
        say "To get $previous back: curl -fsSL $script_url | sh -s --" \
            "--version $previous --with $restore_with"
    else
        # The script cannot know where this copy of it came from, so it names the options and the
        # base URL, not a command.
        say "To get $previous back, run this script again the same way, with --version $previous" \
            "--with $restore_with and CONTEXTO_INSTALL_BASE_URL=$base."
    fi
}

install_wheel() {
    case $installer in
        uv)
            # Plain `uv tool install` replaces an install of the same distribution in place.
            set -- uv tool install --python '>=3.12'
            if [ "$force" = 1 ]; then
                set -- "$@" --force
            fi
            ;;
        *)
            # pipx leaves an install of the same distribution untouched unless given --force.
            set -- pipx install
            if [ -n "$pipx_python" ]; then
                set -- "$@" --python "$pipx_python"
            fi
            if [ "$force" = 1 ] || listed "$dist" "$pipx_names"; then
                set -- "$@" --force
            fi
            ;;
    esac
    set -- "$@" "$tmpdir/$wheel"
    if [ "$dry_run" = 1 ]; then
        say "Would run: $(quoted "$@")"
        return 0
    fi
    say "Installing with $installer: $(quoted "$@")"
    if ! "$@" >"$tmpdir/tool.log" 2>&1 </dev/null; then
        cat "$tmpdir/tool.log" >&2
        if [ "$removed" = 1 ]; then
            printf 'install.sh: error: %s %s\n' "\`$(quoted "$@")\` failed ($installer's message is above)." \
                "No contexto is installed now: the earlier install was removed first." >&2
            if [ -n "$previous" ]; then
                restore_hint >&2
            fi
            exit 1
        fi
        if has_force "$@"; then
            die "\`$(quoted "$@")\` failed ($installer's message is above)."
        fi
        die "\`$(quoted "$@")\` failed ($installer's message is above). If it says that a contexto" \
            "executable already exists, --force lets $installer overwrite it."
    fi
}

# Removes the temporary directory, and only that: tmpdir is empty until mktemp has made the
# directory, and the name must be one mktemp was asked for.
cleanup() {
    case ${tmpdir-} in
        */contexto-install.*) rm -rf "$tmpdir" ;;
    esac
}

main() {
    # The last line of this script passes --end-of-script after the caller's arguments. A copy
    # that was cut short cannot pass it, whatever the cut left of that line.
    count=$#
    index=0
    complete=0
    for arg; do
        index=$((index + 1))
        if [ "$index" -lt "$count" ]; then
            set -- "$@" "$arg"
        elif [ "$arg" = --end-of-script ]; then
            complete=1
        fi
    done
    if [ "$complete" != 1 ]; then
        die "this copy of install.sh is incomplete (a download cut short?). Nothing was done."
    fi
    shift "$count"

    repo=VentureForgre/contexto
    default_base=https://github.com/$repo
    script_url=https://raw.githubusercontent.com/$repo/main/install.sh
    oldest_with_sums=0.5.13
    # Seconds: to connect, of a stalled transfer, and for one whole download.
    connect_timeout=15
    stall_seconds=30
    max_seconds=600
    want=${CONTEXTO_VERSION-}
    want_from=CONTEXTO_VERSION
    with=
    attest=0
    force=0
    dry_run=0
    removed=0
    removed_by=
    tmpdir=

    while [ $# -gt 0 ]; do
        case $1 in
            --version | --with)
                if [ $# -lt 2 ] || [ -z "$2" ]; then
                    usage_error "$1 needs a value."
                fi
                if [ "$1" = --version ]; then
                    want=$2
                    want_from=--version
                else
                    with=$2
                fi
                shift
                ;;
            --version=?*)
                want=${1#--version=}
                want_from=--version
                ;;
            --with=?*) with=${1#--with=} ;;
            --attest) attest=1 ;;
            --force) force=1 ;;
            --dry-run) dry_run=1 ;;
            -h | --help)
                usage
                exit 0
                ;;
            *) usage_error "unknown option: $1" ;;
        esac
        shift
    done
    case $with in
        '' | uv | pipx) ;;
        *) usage_error "--with takes uv or pipx." ;;
    esac
    version=
    if [ -n "$want" ]; then
        version=${want#v}
        is_version "$version" || usage_error "$want_from takes a release version such as 0.5.24 or v0.5.24."
    fi

    base=${CONTEXTO_INSTALL_BASE_URL:-$default_base}
    base=${base%/}
    case $base in
        *[@?#\\\ ]*) usage_error "CONTEXTO_INSTALL_BASE_URL must be a plain https URL, with no user name or query." ;;
        https://?*) proto='=https' ;;
        http://127.0.0.1 | http://127.0.0.1[:/]* | http://localhost | http://localhost[:/]*) proto='=http,https' ;;
        *) usage_error "CONTEXTO_INSTALL_BASE_URL must start with https:// (http:// only for 127.0.0.1 or localhost)." ;;
    esac

    # Everything that can be checked without the network comes first.
    have curl || stop "curl is needed to download the release, and it is not on PATH."
    if ! have sha256sum && ! have shasum; then
        stop "sha256sum or shasum is needed to check the download, and neither is on PATH."
    fi
    if [ "$attest" = 1 ] && ! have gh; then
        stop "--attest needs gh, the GitHub CLI (https://cli.github.com), and it is not on PATH."
    fi
    uv_names=$(names_in uv)
    pipx_names=$(names_in pipx)
    choose_installer
    pipx_python=
    if [ "$installer" = pipx ]; then
        choose_pipx_python
    fi
    if [ -n "$version" ] && older_than "$version" "$oldest_with_sums"; then
        stop "v$version has no SHA256SUMS, so its download cannot be checked. v$oldest_with_sums is the" \
            "oldest release that has one."
    fi

    if [ "$base" != "$default_base" ]; then
        say "Using CONTEXTO_INSTALL_BASE_URL=$base instead of $default_base."
    fi
    if [ -n "$version" ]; then
        tag=v$version
        say "Release: $tag"
    else
        find_latest
        version=${tag#v}
        say "Latest release: $tag"
    fi

    # Up to date: the command reports this release, and there is no second install to clear away.
    find_contexto
    case $installer in
        uv)
            mine=$uv_names
            others=$pipx_names
            ;;
        *)
            mine=$pipx_names
            others=$uv_names
            ;;
    esac
    extra=0
    for name in $mine; do
        extra=$((extra + 1))
    done
    if [ "$force" = 0 ] && [ "$reported" = "contexto $version" ] && [ -z "$others" ] && [ "$extra" -le 1 ]; then
        say "contexto $version is already installed ($contexto_cmd). Nothing to do; --force reinstalls it."
        exit 0
    fi
    # The release that is installed now, when the command names one.
    previous=${reported#contexto }
    if [ "$previous" = "$reported" ] || ! is_version "$previous"; then
        previous=
    fi
    if [ -n "$previous" ] && older_than "$version" "$previous"; then
        say "Replacing contexto $previous with the older $version."
    fi

    trap cleanup EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    parent=${TMPDIR:-/tmp}
    tmpdir=$(mktemp -d "${parent%/}/contexto-install.XXXXXX") || stop "Could not create a temporary directory."

    release=$base/releases/download/$tag
    download "$release/SHA256SUMS" "$tmpdir/SHA256SUMS" 65536 ||
        stop "Could not download $release/SHA256SUMS (curl's message is above). If the answer was 404," \
            "$tag does not exist or has no SHA256SUMS: v$oldest_with_sums is the oldest release that has one."
    read_sums "$tmpdir/SHA256SUMS" "$version"
    case $wheel in
        contexto_proxy-*) dist=contexto-proxy ;;
        *) dist=contexto ;;
    esac
    # shellcheck disable=SC2088  # shown to the user, not expanded
    data_home=${CONTEXTO_HOME:-'~/.contexto'}

    if [ "$dry_run" = 1 ]; then
        say "Dry run. Nothing is installed or removed, and the wheel is not downloaded."
        say "Would download: $release/$wheel"
        say "Would check: its SHA-256 is $wheel_sha, from $release/SHA256SUMS"
        if [ "$attest" = 1 ]; then
            say "Would run: $(quoted gh attestation verify "$tmpdir/$wheel" --repo "$repo")"
        fi
        if [ -n "$uv_names$pipx_names" ]; then
            try_wheel
        fi
        remove_other_installs
        install_wheel
        say "Session data in $data_home would not be touched."
        exit 0
    fi

    download "$release/$wheel" "$tmpdir/$wheel" 104857600 ||
        stop "Could not download $release/$wheel (curl's message is above)."
    actual=$(sha256_of "$tmpdir/$wheel") || stop "Could not compute the SHA-256 of the download."
    if [ "$actual" != "$wheel_sha" ]; then
        stop "SHA-256 mismatch for $wheel: SHA256SUMS says $wheel_sha, the download has $actual."
    fi
    say "Verified $wheel: SHA-256 $actual, as in the release's SHA256SUMS."
    if [ "$attest" = 1 ]; then
        if ! gh attestation verify "$tmpdir/$wheel" --repo "$repo" >"$tmpdir/gh.log" 2>&1 </dev/null; then
            cat "$tmpdir/gh.log" >&2
            stop "The attestation check failed (gh's message is above). If gh is not logged in, run" \
                "gh auth login."
        fi
        say "Attestation verified: GitHub attests that a workflow in $repo built this wheel."
    elif [ "$base" = "$default_base" ]; then
        say "Not checked: who built it. To check the build attestation (needs gh and gh auth login):"
        say "  gh release download $tag --repo $repo --pattern '$wheel' &&" \
            "gh attestation verify $wheel --repo $repo"
    else
        # A copy downloaded from GitHub would not be the file that came from this base URL.
        say "Not checked: who built it. To check the build attestation of this file, run this script" \
            "again with --attest (needs gh and gh auth login)."
    fi

    if [ -n "$uv_names$pipx_names" ]; then
        try_wheel
    fi
    remove_other_installs
    install_wheel

    find_contexto
    if [ -z "$contexto_cmd" ]; then
        die "$installer reported success, but there is no contexto command on PATH or in $bin_dir."
    fi
    if [ "$reported" != "contexto $version" ]; then
        die "$installer installed contexto $version, but $contexto_cmd reports \"$reported\". If another" \
            "install comes first on PATH, remove it. If $installer did not replace that command, run this" \
            "script again with --force. See https://github.com/$repo/blob/main/docs/install.md#troubleshooting"
    fi
    say "Installed with $installer. Session data in $data_home was not touched."
    if [ "$on_path" = 0 ]; then
        say "$bin_dir is not on PATH, so your shell will not find contexto yet. Add it:"
        say "  export PATH=\"$bin_dir:\$PATH\""
    fi
    say "$reported"
}

main "$@" --end-of-script
}
