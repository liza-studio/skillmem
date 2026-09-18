# The MCP Registry lists a package; Glama builds a container to score it, and a
# server without a working build is kept out of its search results — which in turn
# blocks the awesome-mcp-servers listing. So the build is ours, not inferred.
#
# Base install only: the semantic extra pulls a 220MB ONNX model, and retrieval
# degrades to BM25 without it rather than breaking. Build with
# `--build-arg EXTRAS=[semantic]` when you want the vector path in the image.
FROM python:3.12-slim

ARG EXTRAS=""

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY skillmem ./skillmem

RUN pip install --no-cache-dir ".${EXTRAS}"

# The database lives outside the image so a container restart keeps the memory.
ENV SKILLMEM_HOME=/data
VOLUME ["/data"]

# stdio transport: an MCP client talks to this process over stdin/stdout.
#
# Through a shim, because a scanner that writes its requests and closes stdin at
# once races the SDK's own 0.4s import: the reader does not exist yet when EOF
# lands, and the catalogue then lists zero tools for a server that has nine. The
# shim drains stdin first and holds the pipe open while the server answers. See
# docker/mcp-stdio-shim.py for the measurements. Ordinary clients never take
# this path — they keep the connection open and talk to `skillmem mcp` as before.
COPY docker/mcp-stdio-shim.py /usr/local/bin/mcp-stdio-shim.py
ENTRYPOINT ["python", "/usr/local/bin/mcp-stdio-shim.py"]
CMD []
