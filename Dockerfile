# Remote/shared Strobes Agents MCP server (streamable HTTP).
#   docker build -t strobes-agents-mcp .
#   docker run -p 8765:8765 strobes-agents-mcp
# Clients send their own key per request (Authorization: Bearer <MasterKey>,
# X-Strobes-Url, X-Strobes-Organization-Id). For a single-tenant deployment,
# drop --require-header-auth and pass STROBES_URL / STROBES_API_KEY /
# STROBES_ORGANIZATION_ID as env instead.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system --no-cache . && useradd -r -u 10001 mcp
USER mcp
ENV STROBES_AI_HOME=/tmp/strobes-ai
EXPOSE 8765
ENTRYPOINT ["strobes-agents-mcp"]
CMD ["serve", "--http", "--host", "0.0.0.0", "--port", "8765", "--require-header-auth"]
