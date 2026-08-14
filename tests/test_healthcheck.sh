#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HEALTHCHECK="$REPO_DIR/healthcheck.sh"
TMP_ROOT="$(mktemp -d /tmp/fleet-health-test.XXXXXX)"

cleanup() {
  case "$TMP_ROOT" in
    /tmp/fleet-health-test.*) rm -rf -- "$TMP_ROOT" ;;
    *) printf 'Refusing unsafe cleanup path: %s\n' "$TMP_ROOT" >&2 ;;
  esac
}
trap cleanup EXIT

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_contains() {
  local output="$1"
  local expected="$2"
  local label="$3"
  [[ "$output" == *"$expected"* ]] || fail "$label: missing output: $expected"
}

assert_not_contains() {
  local output="$1"
  local unexpected="$2"
  local label="$3"
  [[ "$output" != *"$unexpected"* ]] || fail "$label: leaked output: $unexpected"
}

prepare_case() {
  local name="$1"
  local case_dir="$TMP_ROOT/$name"

  mkdir -p "$case_dir/bin" "$case_dir/home" "$case_dir/projects"
  cat > "$case_dir/bin/nix" <<'EOF'
#!/usr/bin/env bash
printf 'nix (Fleet health test)\n'
EOF
  chmod +x "$case_dir/bin/nix"
  printf '%s\n' "$case_dir"
}

write_gh_success() {
  local bin_dir="$1"

  cat > "$bin_dir/gh" <<'EOF'
#!/usr/bin/env bash
[ "${GH_PROMPT_DISABLED:-}" = "1" ] || exit 90
[ "${GIT_TERMINAL_PROMPT:-}" = "0" ] || exit 91
case "${1:-}" in
  config)
    printf 'https\n'
    ;;
  auth)
    printf 'AUTH_SECRET_MUST_NOT_LEAK\n'
    printf 'AUTH_SECRET_MUST_NOT_LEAK\n' >&2
    ;;
  *)
    exit 92
    ;;
esac
EOF
  chmod +x "$bin_dir/gh"
}

write_gh_failure() {
  local bin_dir="$1"

  cat > "$bin_dir/gh" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
  config)
    printf 'https\n'
    ;;
  auth)
    printf 'AUTH_SECRET_MUST_NOT_LEAK\n' >&2
    exit 1
    ;;
  *)
    exit 1
    ;;
esac
EOF
  chmod +x "$bin_dir/gh"
}

write_git_success() {
  local bin_dir="$1"

  cat > "$bin_dir/git" <<'EOF'
#!/usr/bin/env bash
[ "${GH_PROMPT_DISABLED:-}" = "1" ] || exit 80
[ "${GIT_TERMINAL_PROMPT:-}" = "0" ] || exit 81
case "${1:-}" in
  ls-remote) exit 0 ;;
  *) exit 82 ;;
esac
EOF
  chmod +x "$bin_dir/git"
}

write_git_private_failure() {
  local bin_dir="$1"

  cat > "$bin_dir/git" <<'EOF'
#!/usr/bin/env bash
[ "${GH_PROMPT_DISABLED:-}" = "1" ] || exit 80
[ "${GIT_TERMINAL_PROMPT:-}" = "0" ] || exit 81
case "${1:-}" in
  ls-remote)
    case "${2:-}" in
      *system-operator-agent.git) exit 1 ;;
      *) exit 0 ;;
    esac
    ;;
  *) exit 82 ;;
esac
EOF
  chmod +x "$bin_dir/git"
}

run_health() {
  local case_dir="$1"

  env \
    HOME="$case_dir/home" \
    PROJECTS_DIR="$case_dir/projects" \
    SSH_KEY_PATH="$case_dir/home/.ssh/id_ed25519" \
    PATH="$case_dir/bin:/usr/bin:/bin" \
    bash "$HEALTHCHECK" 2>&1
}

# A local SSH keypair is not proof that GitHub knows the key or that the
# required private repository is readable. The required-repo preflight must
# fail the health check when actual access fails.
ssh_false_positive_case="$(prepare_case ssh-false-positive)"
mkdir -p "$ssh_false_positive_case/home/.ssh"
: > "$ssh_false_positive_case/home/.ssh/id_ed25519"
: > "$ssh_false_positive_case/home/.ssh/id_ed25519.pub"
write_gh_failure "$ssh_false_positive_case/bin"
write_git_private_failure "$ssh_false_positive_case/bin"
if ssh_output="$(run_health "$ssh_false_positive_case")"; then
  fail "SSH key presence unexpectedly passed without required private-repo access"
else
  ssh_rc=$?
fi
[ "$ssh_rc" -eq 1 ] || fail "SSH false-positive case returned $ssh_rc instead of 1"
assert_contains "$ssh_output" "SSH keypair present" "SSH diagnostic"
assert_contains "$ssh_output" "key presence alone is not treated as proof" "SSH diagnostic"
assert_contains "$ssh_output" "repo access unavailable noninteractively: https://github.com/jarrettdustinqq/system-operator-agent.git" "SSH access failure"
assert_not_contains "$ssh_output" "AUTH_SECRET_MUST_NOT_LEAK" "SSH diagnostic"

# Authenticated gh-over-HTTPS plus successful noninteractive Git access is healthy.
https_case="$(prepare_case https)"
write_gh_success "$https_case/bin"
write_git_success "$https_case/bin"
https_output="$(run_health "$https_case")"
assert_contains "$https_output" "GitHub CLI authenticated over HTTPS" "HTTPS auth"
assert_contains "$https_output" "repo access: https://github.com/jarrettdustinqq/system-operator-agent.git" "HTTPS private access"
assert_not_contains "$https_output" "AUTH_SECRET_MUST_NOT_LEAK" "HTTPS auth"

# Existing Git credentials may be valid even without gh or an SSH key. Actual
# noninteractive repository access is the decisive bootstrap-readiness signal.
git_credential_case="$(prepare_case git-credential)"
write_gh_failure "$git_credential_case/bin"
write_git_success "$git_credential_case/bin"
git_credential_output="$(run_health "$git_credential_case")"
assert_contains "$git_credential_output" "required-repository access preflight will decide health" "Git credential fallback"
assert_contains "$git_credential_output" "repo access: https://github.com/jarrettdustinqq/system-operator-agent.git" "Git credential fallback"
assert_not_contains "$git_credential_output" "AUTH_SECRET_MUST_NOT_LEAK" "Git credential fallback"

printf 'HEALTHCHECK_TESTS=PASS\n'
