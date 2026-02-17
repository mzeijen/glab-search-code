# Group-Scoped Search

## Overview

Add the ability to limit code search to a specific GitLab group using the `--group` option.

## Usage

```bash
# Current (global search)
glab-search-code 'query' --hostname gitlab.example.com

# New (group-scoped search)
glab-search-code 'query' --hostname gitlab.example.com --group my-org/my-team
```

## Implementation

### CLI Changes

Add optional `--group` argument:
- Accepts group path (e.g., `my-org/my-team`)
- Human-readable, matches GitLab URLs

### API Endpoint

- **Global search:** `search?scope=blobs&search=...`
- **Group-scoped:** `groups/{url_encoded_path}/search?scope=blobs&search=...`

The group path is URL-encoded (e.g., `my-org/my-team` → `my-org%2Fmy-team`).

### Code Changes

1. **`__init__.py` argparse:** Add `--group` optional argument
2. **`GitLabSearcher.__init__`:** Store `group` parameter
3. **`search_all()`:** Construct group-scoped endpoint when `--group` provided
4. **Docstring/README:** Update examples and help text

### Files to Modify

- `glab_search_code/__init__.py`
- `README.md`
