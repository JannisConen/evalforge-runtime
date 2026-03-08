# evalforge-runtime

Runtime engine for [EvalForge](https://github.com/JannisConen/evalforge-runtime) generated applications. Provides a FastAPI server that executes LLM-powered processes with cost tracking, execution logging, secret management, and file handling.

> Completely vibe-coded in 1 hour.

## Installation

```bash
pip install evalforge-runtime
```

With optional extras:

```bash
# Langfuse observability
pip install evalforge-runtime[langfuse]

# Azure Key Vault secrets
pip install evalforge-runtime[azure]

# AWS Secrets Manager
pip install evalforge-runtime[aws]

# Everything
pip install evalforge-runtime[all]
```

## Quick start

### 1. Create a config file

Create `evalforge.config.yaml` in your project root:

```yaml
project:
  id: my-project

llm:
  model: anthropic/claude-sonnet-4-6

processes:
  classify:
    process_id: classify
    trigger:
      type: webhook
    instructions: |
      Classify the incoming support ticket into one of: billing, technical, general.
```

### 2. Set your API keys

```bash
export EVALFORGE_API_KEYS=my-secret-key
export ANTHROPIC_API_KEY=sk-ant-...
```

`EVALFORGE_API_KEYS` is a comma-separated list of keys that authenticate requests to the runtime.

### 3. Start the server

```bash
evalforge-runtime start --config evalforge.config.yaml
```

The server starts on `http://localhost:8000` by default.

### 4. Call a process

```bash
curl -X POST http://localhost:8000/process/classify \
  -H "Authorization: Bearer my-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"ticket_text": "I cannot log in to my account"}'
```

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check with version and uptime |
| `POST` | `/process/<name>` | Execute a process (authenticated) |
| `GET` | `/executions` | Query execution history |
| `GET` | `/executions/:id` | Single execution detail |
| `GET` | `/executions/stats` | Aggregated cost, latency, and volume stats |

## Configuration

The runtime is configured via `evalforge.config.yaml`. Environment variables can be referenced with `${VAR_NAME}` syntax.

### Top-level sections

```yaml
project:
  id: my-project              # Required: project identifier
  evalforge_url: https://...   # Optional: EvalForge server URL
  version: 1.0.0

llm:
  model: anthropic/claude-sonnet-4-6  # Default LLM model (LiteLLM format)

database:
  url: sqlite+aiosqlite:///./data/app.db  # Execution log database

storage:
  type: local          # local or s3
  path: ./data/files   # Local file storage path

auth:
  methods:
    - type: api_key    # api_key or oauth2

secrets:
  provider: env        # env, evalforge, azure_keyvault, aws_secrets_manager, sap_credential_store

observability:
  langfuse:
    enabled: false
    host: https://cloud.langfuse.com
```

### Process configuration

Each process defines an LLM-powered task:

```yaml
processes:
  my-process:
    process_id: my-process
    trigger:
      type: webhook           # webhook, schedule, or process
      cron: "0 9 * * *"       # For schedule triggers
      after: other-process    # For process triggers (chaining)
    instructions: |
      Your LLM instructions here...
    llm_model: openai/gpt-4o  # Override default model
    connector: exchange        # Optional: built-in connector
    review:
      enabled: true
      timeout: 24h
```

### Custom process modules

For advanced logic, provide Python modules for the three-phase pipeline:

```yaml
processes:
  my-process:
    process_id: my-process
    trigger:
      type: webhook
    before_module: processes.my_process.before     # Pre-processing
    execution_module: processes.my_process.execution  # Custom LLM call
    after_module: processes.my_process.after        # Post-processing
```

Each module should define an async function matching the phase signature. See the [EvalForge docs](https://github.com/JannisConen/evalforge-runtime) for details.

## Secret providers

The runtime supports multiple secret backends:

| Provider | Config value | Description |
|----------|-------------|-------------|
| Environment | `env` | Read from environment variables (default) |
| EvalForge | `evalforge` | Fetch from EvalForge server |
| Azure Key Vault | `azure_keyvault` | Azure Key Vault (`pip install evalforge-runtime[azure]`) |
| AWS Secrets Manager | `aws_secrets_manager` | AWS Secrets Manager (`pip install evalforge-runtime[aws]`) |
| SAP Credential Store | `sap_credential_store` | SAP BTP Credential Store |

## CLI reference

```
evalforge-runtime start [OPTIONS]

Options:
  --config PATH   Path to evalforge.config.yaml (default: evalforge.config.yaml)
  --host TEXT      Bind host (default: 0.0.0.0)
  --port INT       Bind port (default: 8000)
```

## Development

```bash
# Clone and install
git clone https://github.com/JannisConen/evalforge-runtime.git
cd evalforge-runtime
pip install -e ".[dev]"

# Run tests
pytest -v

# Lint
ruff check .
```

## License

MIT
