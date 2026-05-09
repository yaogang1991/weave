# SPEC: core/llm_client.py

## Purpose

Unified LLM API wrapper providing a single `call(messages, tools)` interface for both Anthropic and OpenAI providers. Extracted as a shared dependency so that `AgentWorker` and `IntelligentOrchestrator` can call LLMs without coupling to provider-specific SDKs.

## Public Interfaces

### `LLMClient`

```python
class LLMClient:
    def __init__(self, config: LLMConfig) -> None
    def call(self, messages: list[dict], tools: list[dict] | None = None) -> dict
```

#### Constructor

```python
def __init__(self, config: LLMConfig) -> None
```
- Creates the underlying SDK client (`anthropic.Anthropic` or `openai.OpenAI`) based on `config.provider`.
- Stores `config` as `self.config`.

#### `call`

```python
def call(self, messages: list[dict], tools: list[dict] | None = None) -> dict
```

**Parameters:**
- `messages` -- Chat messages in OpenAI-style format (`[{"role": "...", "content": "..."}, ...]`).
- `tools` -- Tool schemas in OpenAI format. Pass `None` or `[]` to omit tools.

**Returns:** `dict` with keys:
- `"role"`: always `"assistant"`
- `"content"`: `str` -- text response
- `"tool_calls"`: `list[dict]` (optional, present when the model invokes tools). Each dict has keys `"id"`, `"name"`, `"arguments"` (parsed dict).

**Dispatch logic:**
- `config.provider == "anthropic"` -> `_call_anthropic(messages, tools)`
- Otherwise -> `_call_openai(messages, tools)`

#### Internal: `_call_anthropic`

```python
def _call_anthropic(self, messages: list[dict], tools: list[dict]) -> dict
```

Converts OpenAI-style messages to Anthropic Messages API format:
1. Extracts `role=system` messages into the `system` parameter.
2. Converts `role=assistant` messages with `tool_calls` into Anthropic content blocks (`tool_use` type).
3. Groups consecutive `role=tool` messages into a single `role=user` turn with `tool_result` content blocks.
4. Passes other messages through unchanged.
5. Calls `self._client.messages.create(model, max_tokens, temperature, messages, system?, tools?)`.
6. Parses response content blocks: `text` blocks appended to `content`, `tool_use` blocks collected into `tool_calls`.

#### Internal: `_call_openai`

```python
def _call_openai(self, messages: list[dict], tools: list[dict]) -> dict
```

- Calls `self._client.chat.completions.create(model, messages, tools, max_tokens, temperature)`.
- Parses `choices[0].message` into the unified response dict.
- `tool_calls[].function.arguments` is JSON-parsed from string to dict.

#### Internal: `_create_client`

```python
def _create_client(self) -> anthropic.Anthropic | openai.OpenAI
```
- Provider `"anthropic"`: returns `anthropic.Anthropic(api_key, base_url, timeout)`.
- Otherwise: returns `openai.OpenAI(api_key, base_url, timeout)`.

## Data Flow

```
Caller (AgentWorker / IntelligentOrchestrator)
  -> LLMClient.call(messages, tools)
  -> Dispatches to _call_anthropic() or _call_openai()
     - Anthropic path: message format conversion -> messages.create() -> content block parsing
     - OpenAI path: chat.completions.create() -> choice parsing
  -> Returns unified dict: {"role": "assistant", "content": str, "tool_calls"?: list[dict]}
```

### Message Format Conversion (Anthropic path)

```
OpenAI input:
  [{"role": "system", "content": "..."}, {"role": "user", "content": "..."},
   {"role": "assistant", "tool_calls": [...]}, {"role": "tool", "content": "..."}]

Anthropic output:
  system="...", messages=[
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": [{"type": "text", ...}, {"type": "tool_use", ...}]},
    {"role": "user", "content": [{"type": "tool_result", ...}]}
  ]
```

## Error Codes

No custom error codes. Exceptions propagate from the underlying SDKs:
- `anthropic.APIError` / `anthropic.AuthenticationError` / `anthropic.RateLimitError` -- Anthropic SDK errors.
- `openai.APIError` / `openai.AuthenticationError` / `openai.RateLimitError` -- OpenAI SDK errors.
- `json.JSONDecodeError` -- If OpenAI returns malformed tool call arguments.

## Dependencies

- `anthropic` -- Anthropic Python SDK.
- `openai` -- OpenAI Python SDK.
- `core.config.LLMConfig` -- Configuration model.

## Configuration

Configured via `LLMConfig` (see `core_config.md` spec). Key fields consumed:

| LLMConfig Field | Usage |
|-----------------|-------|
| `provider` | Selects `"anthropic"` vs `"openai"` code path |
| `model` | Passed to `model` parameter |
| `api_key` | Passed to SDK constructor |
| `base_url` | Passed to SDK constructor (or `None` if empty) |
| `max_tokens` | Passed to `max_tokens` parameter |
| `temperature` | Passed to `temperature` parameter |
| `timeout` | Passed to SDK constructor |

## Extension Points

1. **New providers**: Add a new `elif` branch in `_create_client()` and a corresponding `_call_<provider>()` method. Ensure the return dict matches the unified format.
2. **Response parsing hooks**: Override or extend `_call_anthropic` / `_call_openai` in a subclass to post-process responses.

## Invariants

1. `call()` always returns a dict with at least `"role"` and `"content"` keys.
2. `"tool_calls"` is present in the return dict only when the model generated tool calls.
3. `tool_calls[].arguments` is always a parsed `dict`, never a JSON string.
4. Empty `base_url` is passed as `None` to SDK constructors (not empty string).
5. System messages are extracted from the message list for Anthropic but passed inline for OpenAI.
6. Consecutive `role=tool` messages are grouped into a single Anthropic user turn (API requirement).
