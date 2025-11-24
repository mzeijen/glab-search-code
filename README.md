# glab-search-code

A powerful async tool for searching and downloading files from GitLab repositories using the `glab` CLI.

## Features

- **Async Parallel Downloads**: Configure the number of concurrent workers to optimize download speed
- **Smart Rate Limiting**: Automatic retry with exponential backoff for rate-limited requests
- **Project Caching**: Minimizes API calls by caching project information
- **Resume Support**: Skips already downloaded files when re-running searches
- **Progress Tracking**: Real-time progress bar with success/skip/fail counters
- **Detailed Logging**: Complete download log with timestamps and error details
- **Metadata Export**: JSON file mapping downloaded files to their source locations
- **Cross-Platform**: Works on Windows, macOS, and Linux

## Prerequisites

- Python 3.12 or higher
- [glab CLI](https://gitlab.com/gitlab-org/cli) installed and configured
- Active GitLab authentication via `glab auth login`

## Installation

### Using pipx (recommended)

[pipx](https://pipx.pypa.io/) installs the tool in an isolated environment and makes it globally available:

```bash
# Clone the repository
git clone <repository-url>
cd glab-search-code

# Install globally with pipx
pipx install .

# Now you can use it from anywhere
glab-search-code 'your-search-query' --hostname gitlab.example.com
```

To upgrade after making changes:
```bash
pipx upgrade glab-search-code
```

To uninstall:
```bash
pipx uninstall glab-search-code
```

### Using uv (for development)

```bash
# Clone the repository
git clone <repository-url>
cd glab-search-code

# Install with uv
uv sync

# Run directly
uv run glab-search-code 'your-search-query' --hostname gitlab.example.com
```

### Using pip

```bash
# Clone the repository
git clone <repository-url>
cd glab-search-code

# Install the package
pip install .

# Run the tool
glab-search-code 'your-search-query' --hostname gitlab.example.com
```

## Usage

### Basic Search

```bash
glab-search-code 'GeneratedValue' --hostname gitlab.example.com
```

### Custom Worker Count

Increase parallel downloads for faster processing:

```bash
glab-search-code 'class MyService' --hostname gitlab.example.com --workers 20
```

### Custom Retry Settings

Adjust retry behavior for rate-limited requests:

```bash
glab-search-code 'import requests' --hostname gitlab.example.com --max-retries 5
```

## Command Line Options

- `search_query` (required): The search term to find in GitLab repositories
- `--hostname` (required): GitLab hostname to search (must be configured in glab)
- `--workers` (optional): Number of parallel downloads (default: 10, range: 1-50)
- `--max-retries` (optional): Maximum retries for rate-limited requests (default: 3)

## Output

All downloads are stored in your system's temp directory (e.g., `/tmp/glab-code-search-<timestamp>/` on Unix-like systems, `%TEMP%\glab-code-search-<timestamp>\` on Windows):

- Downloaded files with sanitized names (`project__path__to__file.ext`)
- `metadata.json`: Maps downloaded files to their GitLab source locations
- `download.log`: Detailed log of all operations with timestamps

### Useful Commands

After the download completes, the tool suggests useful commands:

```bash
# View complete download log
less /tmp/glab-code-search-<timestamp>/download.log

# View only failed downloads
grep FAIL /tmp/glab-code-search-<timestamp>/download.log

# Search downloaded files
grep -r 'pattern' /tmp/glab-code-search-<timestamp>

# List unique projects
jq -r '.[] | .project_path' /tmp/glab-code-search-<timestamp>/metadata.json | sort -u
```

## Development

### Setup Development Environment

```bash
# Install dependencies including dev tools
uv sync

# Run linter
uv run ruff check glab_search_code

# Format code
uv run ruff format glab_search_code
```

### Project Structure

```
glab-search-code/
├── glab_search_code/       # Main package
│   └── __init__.py         # Main script
├── pyproject.toml          # Project metadata and dependencies
├── README.md               # This file
└── .gitignore
```

## How It Works

1. **Search Phase**: Uses `glab api` to search for files matching your query across all GitLab repositories
2. **Cache Phase**: Pre-fetches project information in parallel to minimize API calls during downloads
3. **Download Phase**: Downloads files in parallel with configurable workers, handling rate limits automatically

## Configuration

The tool automatically detects configured GitLab hostnames from your glab configuration file (`~/.config/glab-cli/config.yml`).

To add a new GitLab instance:

```bash
glab auth login --hostname gitlab.example.com
```

## License

MIT

## Contributing

Contributions are welcome! Please ensure code passes ruff linting before submitting PRs.
