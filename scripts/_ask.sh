#!/usr/bin/env bash
# Shared interactive-prompt helpers — install.sh and reinstall.sh both need to
# ask for the same kinds of values (required strings, optional strings,
# secrets, paste-friendly keys), so this is the one place that logic lives.
# Requires $BOLD/$NC (color) vars from the sourcing script's own setup.

ask() {  # ask <var> <question> [default]  — empty answer re-prompts if no default
    local _var="$1" _q="$2" _default="${3:-}" _ans
    if [ -n "$_default" ]; then
        read -rp "$(echo -e "  ${BOLD}${_q}${NC} [${_default}]: ")" _ans
        printf -v "$_var" '%s' "${_ans:-$_default}"
    else
        while true; do
            read -rp "$(echo -e "  ${BOLD}${_q}${NC}: ")" _ans
            [ -n "$_ans" ] && break
            echo "    (required)"
        done
        printf -v "$_var" '%s' "$_ans"
    fi
}

ask_opt() {  # ask_opt <var> <question> [default]  — empty answer allowed
    local _var="$1" _q="$2" _default="${3:-}" _ans
    if [ -n "$_default" ]; then
        read -rp "$(echo -e "  ${BOLD}${_q}${NC} [${_default}]: ")" _ans
        printf -v "$_var" '%s' "${_ans:-$_default}"
    else
        read -rp "$(echo -e "  ${BOLD}${_q}${NC} (leave blank to skip): ")" _ans
        printf -v "$_var" '%s' "${_ans:-}"
    fi
}

ask_secret() {  # ask_secret <var> <question>  — input hidden, no default
    local _var="$1" _q="$2" _ans
    read -rsp "$(echo -e "  ${BOLD}${_q}${NC}: ")" _ans
    echo
    printf -v "$_var" '%s' "$_ans"
}

# Plaintext, not masked — lets you visually compare against the value shown
# on the issuing dashboard (e.g. app.netbird.io) while typing/pasting, same
# as TAK's install.sh does for its NetBird setup key prompt.
ask_key() {
    local _var="$1" _q="$2" _ans
    read -rp "$(echo -e "  ${BOLD}${_q}${NC}: ")" _ans
    printf -v "$_var" '%s' "$_ans"
}

ask_yes_no() {  # ask_yes_no <var> <question> [default: y|n, default n]  — sets <var> to 0 or 1
    local _var="$1" _q="$2" _default="${3:-n}" _ans _prompt="y/N"
    [ "$_default" = "y" ] && _prompt="Y/n"
    read -rp "$(echo -e "  ${BOLD}${_q}${NC} [${_prompt}]: ")" _ans
    case "${_ans:-$_default}" in
        [Yy]*) printf -v "$_var" '1' ;;
        *)     printf -v "$_var" '0' ;;
    esac
}

env_value() {
    local key="$1"
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^${key}=//p" "$ENV_FILE" | head -1
}

gen_uuid() { python3 -c 'import uuid; print(uuid.uuid4().hex)'; }
