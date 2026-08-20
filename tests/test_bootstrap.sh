#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOOTSTRAP="$REPO_DIR/bootstrap.sh"
TMP_ROOT="$(mktemp -d /tmp/fleet-bootstrap-test.XXXXXX)"

cleanup() {
  case "$TMP_ROOT" in
    /tmp/fleet-bootstrap-test.*) rm -rf -- "$TMP_ROOT" ;;
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

prepare_case() {
  local name="$1"
  local case_dir="$TMP_ROOT/$name"
  mkdir -p "$case_dir/bin" "$case_dir/home"
  : > "$case_dir/git.log"
  : > "$case_dir/gh.log"
  printf '%s\n' "$case_dir"
}

write_gh_https() {
  local bin_dir="$1"
  cat > "$bin_dir/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[ "${GH_PROMPT_DISABLED:-}" = "1" ] || exit 90
[ "${GIT_TERMINAL_PROMPT:-}" = "0" ] || exit 91
case "${1:-}" in
  config)
    printf 'https\n'
    ;;
  auth)
    case "${2:-}" in
      status) exit 0 ;;
      setup-git)
        printf 'setup-git:%s\n' "$*" >> "${GH_TEST_LOG:?}"
        exit 0
        ;;
      *) exit 92 ;;
    esac
    ;;
  *) exit 93 ;;
esac
EOF
  chmod +x "$bin_dir/gh"
}

write_git_success() {
  local bin_dir="$1"
  cat > "$bin_dir/git" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[ "${GH_PROMPT_DISABLED:-}" = "1" ] || exit 80
[ "${GIT_TERMINAL_PROMPT:-}" = "0" ] || exit 81
printf 'git:%s\n' "$*" >> "${GIT_TEST_LOG:?}"
case "${1:-}" in
  ls-remote)
    exit 0
    ;;
  clone)
    mkdir -p "${3:?}/.git"
    exit 0
    ;;
  -C)
    exit 0
    ;;
  *)
    exit 82
    ;;
esac
EOF
  chmod +x "$bin_dir/git"
}

write_git_private_preflight_failure() {
  local bin_dir="$1"
  cat > "$bin_dir/git" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[ "${GH_PROMPT_DISABLED:-}" = "1" ] || exit 80
[ "${GIT_TERMINAL_PROMPT:-}" = "0" ] || exit 81
printf 'git:%s\n' "$*" >> "${GIT_TEST_LOG:?}"
case "${1:-}" in
  ls-remote)
    case "${2:-}" in
      *system-operator-agent.git) exit 1 ;;
      *) exit 0 ;;
    esac
    ;;
  clone|-C)
    printf 'MUTATION_MUST_NOT_RUN:%s\n' "$*" >> "${GIT_TEST_LOG:?}"
    exit 99
    ;;
  *)
    exit 82
    ;;
esac
EOF
  chmod +x "$bin_dir/git"
}

run_bootstrap() {
  local case_dir="$1"
  env \
    HOME="$case_dir/home" \
    PROJECTS_DIR="$case_dir/projects" \
    SSH_KEY_PATH="$case_dir/home/.ssh/id_ed25519" \
    GIT_TEST_LOG="$case_dir/git.log" \
    GH_TEST_LOG="$case_dir/gh.log" \
    PATH="$case_dir/bin:/usr/bin:/bin" \
    bash "$BOOTSTRAP" 2>&1
}

active_repo_count="$(grep -cvE '^[[:space:]]*(#|$)' "$REPO_DIR/repos.txt")"

https_case="$(prepare_case https-success)"
write_gh_https "$https_case/bin"
write_git_success "$https_case/bin"
https_output="$(run_bootstrap "$https_case")"
assert_contains "$https_output" "Configuring Git to use authenticated GitHub CLI credentials for HTTPS" "HTTPS setup"
assert_contains "$https_output" "Preflighting noninteractive access to $active_repo_count configured repositories" "HTTPS preflight"
[ ! -e "$https_case/home/.ssh/id_ed25519" ] || fail "HTTPS bootstrap created an unnecessary SSH private key"
[ ! -e "$https_case/home/.ssh/id_ed25519.pub" ] || fail "HTTPS bootstrap created an unnecessary SSH public key"
grep -q '^setup-git:auth setup-git --hostname github.com$' "$https_case/gh.log" || fail "gh auth setup-git was not invoked"
preflight_count="$(grep -c '^git:ls-remote ' "$https_case/git.log")"
clone_count="$(grep -c '^git:clone ' "$https_case/git.log")"
[ "$preflight_count" -eq "$active_repo_count" ] || fail "expected $active_repo_count preflights, got $preflight_count"
[ "$clone_count" -eq "$active_repo_count" ] || fail "expected $active_repo_count clones, got $clone_count"
grep -q 'system-operator-agent.git' "$https_case/git.log" || fail "private operator repo was not preflighted/cloned"

failure_case="$(prepare_case private-failure)"
write_gh_https "$failure_case/bin"
write_git_private_preflight_failure "$failure_case/bin"
if failure_output="$(run_bootstrap "$failure_case")"; then
  fail "bootstrap unexpectedly passed with inaccessible required private repo"
else
  failure_rc=$?
fi
[ "$failure_rc" -ne 0 ] || fail "bootstrap private-access failure returned zero"
assert_contains "$failure_output" "Access FAILED: https://github.com/jarrettdustinqq/system-operator-agent.git" "private preflight"
assert_contains "$failure_output" "No clone or pull operations were started" "private preflight"
if grep -q 'MUTATION_MUST_NOT_RUN' "$failure_case/git.log"; then
  fail "bootstrap mutated repositories after failed access preflight"
fi
if grep -q '^git:clone ' "$failure_case/git.log"; then
  fail "bootstrap cloned a repository after failed access preflight"
fi
[ ! -d "$failure_case/projects" ] || fail "bootstrap created projects directory after failed access preflight"

printf 'BOOTSTRAP_TESTS=PASS\n'
