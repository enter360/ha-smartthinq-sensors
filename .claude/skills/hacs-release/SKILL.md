---
name: hacs-release
description: Cut a new HACS-compatible release for ha-smartthinq-sensors. Bumps version in const.py and manifest.json, opens a PR to master, merges it, creates a GitHub release, builds and uploads smartthinq_sensors.zip as a release asset.
user-invocable: true
argument-hint: <new-version>  (e.g. 1.2.0)
allowed-tools: Bash Read Edit Glob Grep
---

# hacs-release

Cut a new HACS-compatible release. Invoke as `/hacs-release <version>` (e.g. `/hacs-release 1.2.0`).

## Steps

Follow every step in order. Do not skip any. Confirm success before moving to the next.

### 1 — Validate input

- The argument must be a semver string (`MAJOR.MINOR.PATCH`). If it is missing or malformed, stop and ask the user.
- Run `git status` and confirm the working tree is clean and we are on `master` (or pull first if behind).

### 2 — Bump version

Edit exactly two files:

- `custom_components/smartthinq_sensors/const.py` — update `__version__ = "..."` to the new version.
- `custom_components/smartthinq_sensors/manifest.json` — update `"version": "..."` to the new version.

### 3 — Commit, push, open PR

```
git checkout -b chore/bump-version-{version}
git add custom_components/smartthinq_sensors/const.py custom_components/smartthinq_sensors/manifest.json
git commit -m "chore: bump version to {version}"
git push -u origin chore/bump-version-{version}
```

Open a PR targeting `master` via the GitHub REST API:

```
gh api repos/enter360/ha-smartthinq-sensors/pulls \
  --method POST \
  --field title="chore: bump version to {version}" \
  --field base="master" \
  --field head="chore/bump-version-{version}" \
  --field body="Version bump to {version} in preparation for HACS release." \
  --jq '.number'
```

Print the PR URL for the user.

### 4 — Merge the PR into master

Wait for the user to confirm the PR looks correct, then merge:

```
gh api repos/enter360/ha-smartthinq-sensors/pulls/{pr_number}/merge \
  --method PUT \
  --field merge_method="squash" \
  --field commit_title="chore: bump version to {version}"
```

Switch back to master and pull:

```
git checkout master && git pull
```

### 5 — Create GitHub release

```
gh api repos/enter360/ha-smartthinq-sensors/releases \
  --method POST \
  --field tag_name="v{version}" \
  --field name="v{version}" \
  --field target_commitish="master" \
  --field body="## v{version}\n\n<!-- Add release notes here -->" \
  --jq '.id'
```

Save the release ID — it is needed in step 6.

### 6 — Build and upload the zip asset

Build a clean zip (no `__pycache__`, no `.pyc`, no `.DS_Store`):

```
rm -rf /tmp/smartthinq_build /tmp/smartthinq_sensors.zip
rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' \
  custom_components/smartthinq_sensors /tmp/smartthinq_build/
cd /tmp/smartthinq_build && zip -r /tmp/smartthinq_sensors.zip smartthinq_sensors
```

Verify zero pycache entries:

```
zip -sf /tmp/smartthinq_sensors.zip | grep -c pycache || echo "0"
```

Upload to the release (using `uploads.github.com`, not `api.github.com`):

```
TOKEN=$(gh auth token)
curl -s -X POST \
  -H "Authorization: token $TOKEN" \
  -H "Content-Type: application/zip" \
  --data-binary @/tmp/smartthinq_sensors.zip \
  "https://uploads.github.com/repos/enter360/ha-smartthinq-sensors/releases/{release_id}/assets?name=smartthinq_sensors.zip" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('browser_download_url', d))"
```

Print the asset download URL for the user.

### 7 — Done

Print a summary:
- PR URL
- Release URL: `https://github.com/enter360/ha-smartthinq-sensors/releases/tag/v{version}`
- Asset URL: `https://github.com/enter360/ha-smartthinq-sensors/releases/download/v{version}/smartthinq_sensors.zip`

The HACS badge in the README will update automatically once the release tag is live.

## Notes

- **Always merge the PR before creating the release.** Tag against `master`, not the bump branch.
- The zip must contain `smartthinq_sensors/` at the root (not `custom_components/smartthinq_sensors/`). HACS installs it directly into `custom_components/`.
- The asset upload endpoint is `uploads.github.com`, not `api.github.com`. Using `gh api` for the upload will return 404.
- `gh release create` may fail with a spurious "workflow scope" error even after re-auth. Use `gh api repos/.../releases` (POST) instead.
