# OTel Collector Configuration Templates for LLMOps Platforms (#941)

Routes Weave traces/metrics to Langfuse or Arize Phoenix via OTLP.

## Quick Start

### 1. Langfuse (Recommended)

```bash
# Start Langfuse (self-hosted)
docker run -d --name langfuse \
  -p 3000:3000 \
  -e DATABASE_URL=postgresql://postgres:postgres@postgres:5432/langfuse \
  langfuse/langfuse:latest

# Point Weave at Langfuse's OTLP endpoint
export WEAVE_OTLP_ENDPOINT=http://localhost:3000/api/public/otel
export LANGFUSE_PUBLIC_KEY=pk-xxx
export LANGFUSE_SECRET_KEY=sk-xxx
```

### 2. Arize Phoenix

```bash
# Start Phoenix (single container)
docker run -d --name phoenix -p 6006:6006 arizephoenix/phoenix:latest

# Point Weave at Phoenix
export WEAVE_OTLP_ENDPOINT=http://localhost:6006/v1/traces
```

## OTel Collector Config (Optional Middleware)

If you need buffering, sampling, or multi-backend fanout, place an
OpenTelemetry Collector between Weave and the LLMOps platform.

Save as `otel-collector-config.yaml`:

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    timeout: 5s
    send_batch_size: 1024
  # Redact sensitive content before export
  attributes:
    actions:
      - key: gen_ai.input.messages
        action: delete
      - key: gen_ai.output.messages
        action: delete

exporters:
  # Route to Langfuse
  otlp/langfuse:
    endpoint: http://langfuse:3000/api/public/otel
    headers:
      authorization: "Basic ${LANGFUSE_AUTH_HEADER}"
  # Route to Phoenix
  otlp/phoenix:
    endpoint: http://phoenix:6006/v1/traces

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlp/langfuse]
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlp/langfuse]
```

```bash
# Run collector
docker run -d --name otel-collector \
  -p 4317:4317 -p 4318:4318 \
  -v $(pwd)/otel-collector-config.yaml:/etc/otelcol/config.yaml \
  otel/opentelemetry-collector-contrib:latest
```

## Weave Configuration

```bash
# Required: OTLP endpoint
export WEAVE_OTLP_ENDPOINT=http://localhost:4317

# Optional: Content capture (default: false)
export WEAVE_OTEL_CAPTURE_CONTENT=true

# Optional: Structured logging (default: text)
export WEAVE_LOG_FORMAT=json

# Optional: Log level (default: INFO)
export WEAVE_LOG_LEVEL=INFO
```

## What Gets Exported

After issues #936, #938, #939, #940 are resolved:

- **Traces**: Run → Node → LLM Turn / Tool Call hierarchy
- **Metrics**: Token usage, LLM latency, tool call success rate
- **Content**: Prompt/completion (opt-in via WEAVE_OTEL_CAPTURE_CONTENT)
- **Logs**: JSON structured logs with trace_id correlation
