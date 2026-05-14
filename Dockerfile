# Long-running issue-agent-worker process. Pairs with docker-compose.yml.
#
# Build  : docker build -t agent-worker .
# Run    : docker compose up -d worker
#
# The image is intentionally minimal: Python 3.12 + git + the GitHub CLI + the
# package itself. Project configs, runs/, checkpoints/, worktrees/, and the
# host's gh auth are all bind-mounted at runtime — nothing project-specific is
# baked into the image, in line with the "zero hardcoded repo names" rule.

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENT_WORKER_PROJECT_ROOT=/workspace

# Install gh CLI (used for every GitHub call) + git (used by worktree mode).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git \
    && install -dm 0755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | dd of=/etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Install the package itself. Copy pyproject.toml first so dep changes don't
# bust the layer cache on every source edit.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir -e .

# Default command is overridden by docker-compose.yml; this is a sensible
# baseline if someone runs the image directly.
CMD ["agent-worker", "worker", "--with-dispatcher"]
