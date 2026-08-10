# WHO ICTRP MCP Service

The service exposes database_metadata, execute_search_plan, get_trial and the other read-only search tools over stdio or authenticated Streamable HTTP.

## Requirements

Use Python 3.10 or newer:

    python -m pip install -r requirements.txt

Set WHO_ICTRP_DB to the optimized SQLite database path.

## Local stdio

    export WHO_ICTRP_DB=/absolute/path/to/who_ictrp_cancer_trials.db
    export WHO_MCP_TRANSPORT=stdio
    python server.py

The dependency-free server_compat.py remains available for legacy Python 3.9 stdio deployments.

## Authenticated Streamable HTTP

Generate a key outside the repository:

    python -c "import secrets; print(secrets.token_urlsafe(32))"

Configure and start:

    export WHO_ICTRP_DB=/absolute/path/to/who_ictrp_cancer_trials.db
    export WHO_MCP_TRANSPORT=streamable-http
    export WHO_MCP_API_KEY=the-generated-secret
    export WHO_MCP_HOST=127.0.0.1
    export WHO_MCP_PORT=8000
    python server.py

The endpoint is http://127.0.0.1:8000/mcp. Every HTTP request must use Authorization: Bearer <key>. Missing or invalid keys return HTTP 401.

For remote access, keep the service bound to 127.0.0.1 and publish it through an HTTPS reverse proxy. Do not expose the API key over public plaintext HTTP. Binding WHO_MCP_HOST=0.0.0.0 should be limited to a protected private network.

## Client configuration

    WHO_MCP_TRANSPORT=streamable-http
    WHO_MCP_URL=https://mcp.example.org/mcp
    WHO_MCP_API_KEY=the-generated-secret

Do not commit a populated .env. Rotate the API key if it is exposed.

## Database preparation

    python ../scripts/matching_db.py --db ../data/who_ictrp_cancer_trials.db --strategy ../config/who_search_strategy.yaml
