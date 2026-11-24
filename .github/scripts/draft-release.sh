#!/usr/bin/env bash
# Create a draft GitHub release for the manifest version when none exists yet.
# The tag is not created here: publishing the draft creates it.
#
# Environment:
#   GH_REPO      owner/name of the repository
#   TARGET_SHA   the commit that main was pushed to (the release target)
#   GH_TOKEN     token for gh (contents: write, actions: read)
#   REQUIRED_WORKFLOWS  workflow files that must have passed for TARGET_SHA
set -euo pipefail

: "${GH_REPO:?GH_REPO is required}"
: "${TARGET_SHA:?TARGET_SHA is required}"
REQUIRED_WORKFLOWS="${REQUIRED_WORKFLOWS:-test.yml hassfest.yml hacs.yml}"
MANIFEST="custom_components/climate_adapt/manifest.json"
CHANGELOG="CHANGELOG.md"

notice() { echo "::notice::$*"; }
fail() { echo "::error::$*"; exit 1; }

# 1. Release only a commit whose checks have all passed. Each finished check
#    triggers this workflow again, so the last one to finish releases.
for wf in $REQUIRED_WORKFLOWS; do
  state="$(gh run list --workflow "$wf" --commit "$TARGET_SHA" --event push --branch main \
    --limit 1 --json status,conclusion --jq '.[0] // empty | "\(.status) \(.conclusion)"')"
  case "$state" in
    "completed success") ;;
    "completed "*)
      notice "$wf did not pass for $TARGET_SHA ($state); no release."
      exit 0 ;;
    *)
      notice "$wf has not finished for $TARGET_SHA (${state:-no run}); a later run decides."
      exit 0 ;;
  esac
done

# 2. The version being released.
version="$(jq -er '.version' "$MANIFEST")" || fail "cannot read version from $MANIFEST"
[[ "$version" =~ ^[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}\.[0-9]+$ ]] \
  || fail "manifest version '$version' is not YYYY.M.D.N"
tag="v$version"

# 3. Nothing to do when this version already has a tag or a release (draft or published).
#    gh release view does not find drafts, so list the releases instead.
refs="$(gh api "repos/$GH_REPO/git/matching-refs/tags/$tag" --jq '.[].ref')"
if grep -Fxq "refs/tags/$tag" <<< "$refs"; then
  notice "Tag $tag already exists; no release."
  exit 0
fi
releases="$(gh api --paginate "repos/$GH_REPO/releases" --jq '.[] | .tag_name, .name')"
if grep -Fxq "$tag" <<< "$releases"; then
  notice "A release for $tag already exists (draft or published); no release."
  exit 0
fi

# 4. Release notes: the CHANGELOG section headed "## <version>" (or "## [<version>]").
notes="$(mktemp)"
trap 'rm -f "$notes"' EXIT
awk -v want="$version" '
  /^## / {
    if (found) exit
    head = substr($0, 4)
    sub(/^\[/, "", head)
    sub(/[] ].*$/, "", head)
    if (head == want) { found = 1; next }
    next
  }
  found {
    if ($0 ~ /[^[:space:]]/) { printf "%s", pending; pending = ""; print; started = 1 }
    else if (started) pending = pending "\n"
  }
' "$CHANGELOG" > "$notes"
grep -q '[^[:space:]]' "$notes" || fail "$CHANGELOG has no section (or an empty one) for $version"

# 5. Draft release. A draft carries no tag until the owner publishes it.
gh api --method POST "repos/$GH_REPO/releases" \
  -f tag_name="$tag" \
  -f name="$tag" \
  -f target_commitish="$TARGET_SHA" \
  -F body=@"$notes" \
  -F draft=true \
  -F prerelease=false \
  --jq '"Created draft release \(.name) (draft=\(.draft)) at \(.target_commitish)"'
